"""Output devices.

Every player exposes the same small async interface. Local files are played by
handing the WiiM (directly, or through Home Assistant) a URL served by this app.
Spotify tracks go through Spotify Connect (see SpotifyPlayer).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .config import Settings
from .spotify import Spotify

log = logging.getLogger("jukebox.players")


@dataclass
class Status:
    state: str = "unknown"          # playing | paused | stopped | unknown
    position_ms: int = 0
    duration_ms: int = 0
    ident: str | None = None        # what the device says it is playing, if it can tell us


class Player:
    name = "base"

    async def play(self, track: dict, url: str | None) -> None: ...
    async def status(self) -> Status: return Status()
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def stop(self) -> None: ...
    async def set_volume(self, volume: int) -> None: ...


class DummyPlayer(Player):
    """Pretends to play. Handy for trying the app without any hardware."""

    name = "dummy"

    def __init__(self):
        self.track: dict | None = None
        self.started = 0.0
        self.paused_at: float | None = None
        self.volume = 50

    async def play(self, track, url):
        log.info("[dummy] playing %s - %s (%s)", track.get("artist"), track.get("title"), url or track.get("source_id"))
        self.track, self.started, self.paused_at = track, time.time(), None

    def _pos(self) -> int:
        now = self.paused_at or time.time()
        return int((now - self.started) * 1000)

    async def status(self):
        if not self.track:
            return Status("stopped")
        dur = self.track.get("duration_ms") or 180_000
        pos = self._pos()
        if pos >= dur:
            return Status("stopped", dur, dur)
        return Status("paused" if self.paused_at else "playing", pos, dur)

    async def pause(self):
        if self.track and not self.paused_at:
            self.paused_at = time.time()

    async def resume(self):
        if self.paused_at:
            self.started += time.time() - self.paused_at
            self.paused_at = None

    async def stop(self):
        self.track = None

    async def set_volume(self, volume):
        self.volume = volume


class WiimPlayer(Player):
    """WiiM / LinkPlay HTTP API: https://<ip>/httpapi.asp?command=..."""

    name = "wiim"
    STATES = {"play": "playing", "pause": "paused", "stop": "stopped", "none": "stopped", "load": "playing"}

    def __init__(self, host: str):
        if not host:
            raise RuntimeError("PLAYER=wiim needs WIIM_HOST (the WiiM's IP address)")
        self.host = host
        # WiiM uses a self-signed certificate on the local network
        self.http = httpx.AsyncClient(timeout=6, verify=False)
        self.scheme = "https"

    async def _cmd(self, command: str) -> str:
        for scheme in (self.scheme, "http" if self.scheme == "https" else "https"):
            try:
                r = await self.http.get(f"{scheme}://{self.host}/httpapi.asp", params={"command": command})
                self.scheme = scheme
                return r.text
            except httpx.TransportError as e:
                last = e
        raise RuntimeError(f"WiiM at {self.host} unreachable: {last}")

    async def play(self, track, url):
        if not url:
            raise RuntimeError("WiiM player can only play URLs")
        await self._cmd(f"setPlayerCmd:play:{url}")

    async def status(self):
        import json

        try:
            data = json.loads(await self._cmd("getPlayerStatus"))
        except (ValueError, RuntimeError) as e:
            log.warning("WiiM status failed: %s", e)
            return Status()
        return Status(
            state=self.STATES.get(data.get("status", ""), "unknown"),
            position_ms=int(data.get("curpos") or 0),
            duration_ms=int(data.get("totlen") or 0),
            ident=f"mode:{data.get('mode')}",
        )

    async def pause(self):
        await self._cmd("setPlayerCmd:pause")

    async def resume(self):
        await self._cmd("setPlayerCmd:resume")

    async def stop(self):
        await self._cmd("setPlayerCmd:stop")

    async def set_volume(self, volume):
        await self._cmd(f"setPlayerCmd:vol:{max(0, min(100, volume))}")


class HomeAssistantPlayer(Player):
    """Plays URLs on a Home Assistant media_player (e.g. the WiiM / LinkPlay integration)."""

    name = "homeassistant"
    STATES = {"playing": "playing", "paused": "paused", "idle": "stopped", "off": "stopped",
              "on": "stopped", "standby": "stopped", "buffering": "playing"}

    def __init__(self, url: str, token: str, entity_id: str):
        if not (url and token and entity_id):
            raise RuntimeError("PLAYER=homeassistant needs HA_URL, HA_TOKEN and HA_ENTITY_ID")
        self.url, self.entity = url, entity_id
        self.http = httpx.AsyncClient(timeout=8, headers={"Authorization": f"Bearer {token}"})

    async def _service(self, domain: str, service: str, **data) -> None:
        r = await self.http.post(f"{self.url}/api/services/{domain}/{service}", json={"entity_id": self.entity, **data})
        if r.status_code >= 400:
            raise RuntimeError(f"Home Assistant {domain}.{service} failed: {r.status_code} {r.text[:200]}")

    async def play(self, track, url):
        if not url:
            raise RuntimeError("Home Assistant player can only play URLs")
        await self._service("media_player", "play_media", media_content_id=url, media_content_type="music")

    async def status(self):
        try:
            r = await self.http.get(f"{self.url}/api/states/{quote(self.entity)}")
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Home Assistant status failed: %s", e)
            return Status()
        attrs = data.get("attributes", {})
        pos = float(attrs.get("media_position") or 0)
        state = self.STATES.get(data.get("state", ""), "unknown")
        if state == "playing" and attrs.get("media_position_updated_at"):
            # HA only refreshes position occasionally; extrapolate from the timestamp
            from datetime import datetime

            try:
                updated = datetime.fromisoformat(attrs["media_position_updated_at"]).timestamp()
                pos += max(0.0, time.time() - updated)
            except ValueError:
                pass
        return Status(state, int(pos * 1000), int(float(attrs.get("media_duration") or 0) * 1000),
                      ident=attrs.get("media_content_id"))

    async def pause(self):
        await self._service("media_player", "media_pause")

    async def resume(self):
        await self._service("media_player", "media_play")

    async def stop(self):
        await self._service("media_player", "media_stop")

    async def set_volume(self, volume):
        await self._service("media_player", "volume_set", volume_level=max(0, min(100, volume)) / 100)


class SpotifyPlayer(Player):
    """Spotify Connect: tells Spotify to play the track on the configured device (the WiiM)."""

    name = "spotify"

    def __init__(self, spotify: Spotify):
        self.sp = spotify

    async def play(self, track, url):
        await self.sp.play(track["source_id"])

    async def status(self):
        data = await self.sp.status()
        if not data:
            return Status("stopped")
        item = data.get("item") or {}
        return Status(
            state="playing" if data.get("is_playing") else "paused",
            position_ms=data.get("progress_ms") or 0,
            duration_ms=item.get("duration_ms") or 0,
            ident=item.get("id"),
        )

    async def pause(self):
        await self.sp.pause()

    async def resume(self):
        await self.sp.resume()

    async def stop(self):
        try:
            await self.sp.pause()
        except Exception:
            pass

    async def set_volume(self, volume):
        await self.sp.set_volume(volume)


def make_local_player(s: Settings) -> Player:
    if s.player == "wiim":
        return WiimPlayer(s.wiim_host)
    if s.player in ("homeassistant", "ha"):
        return HomeAssistantPlayer(s.ha_url, s.ha_token, s.ha_entity_id)
    return DummyPlayer()
