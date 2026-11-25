"""Spotify Web API: OAuth, search, and Spotify Connect playback on the WiiM.

The admin connects their (Premium) Spotify account once; the refresh token is
kept in the database. Guests search through that account, and tracks are played
by telling Spotify to start them on the WiiM's Spotify Connect device.
"""
from __future__ import annotations

import base64
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import Settings
from .db import DB

log = logging.getLogger("jukebox.spotify")

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"


class SpotifyError(RuntimeError):
    pass


class Spotify:
    def __init__(self, settings: Settings, db: DB):
        self.s = settings
        self.db = db
        self.http = httpx.AsyncClient(timeout=10)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._device_id: str | None = None

    # -- auth --------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self.s.spotify_configured

    @property
    def connected(self) -> bool:
        return self.configured and bool(self.db.get_kv("spotify_refresh_token"))

    def authorize_url(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(16)
        q = urlencode({
            "client_id": self.s.spotify_client_id,
            "response_type": "code",
            "redirect_uri": self.s.spotify_redirect_uri,
            "scope": SCOPES,
            "state": state,
        })
        return f"{ACCOUNTS}/authorize?{q}", state

    def _basic(self) -> str:
        raw = f"{self.s.spotify_client_id}:{self.s.spotify_client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    async def _token_request(self, data: dict) -> dict:
        r = await self.http.post(f"{ACCOUNTS}/api/token", data=data, headers={"Authorization": self._basic()})
        if r.status_code != 200:
            raise SpotifyError(f"token request failed: {r.status_code} {r.text[:200]}")
        tok = r.json()
        self._access_token = tok["access_token"]
        self._expires_at = time.time() + tok.get("expires_in", 3600) - 60
        if tok.get("refresh_token"):
            self.db.set_kv("spotify_refresh_token", tok["refresh_token"])
        return tok

    async def exchange_code(self, code: str) -> None:
        await self._token_request({
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.s.spotify_redirect_uri,
        })

    def disconnect(self) -> None:
        self.db.set_kv("spotify_refresh_token", None)
        self._access_token = None
        self._device_id = None

    async def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        refresh = self.db.get_kv("spotify_refresh_token")
        if not refresh:
            raise SpotifyError("Spotify is not connected (admin: use 'Connect Spotify')")
        await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        return self._access_token  # type: ignore[return-value]

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}"}
        r = await self.http.request(method, f"{API}{path}", headers=headers, **kw)
        if r.status_code == 401:  # token revoked/expired early: retry once
            self._access_token = None
            headers = {"Authorization": f"Bearer {await self._token()}"}
            r = await self.http.request(method, f"{API}{path}", headers=headers, **kw)
        if r.status_code >= 400:
            raise SpotifyError(f"{method} {path} -> {r.status_code} {r.text[:200]}")
        return r

    # -- catalogue -----------------------------------------------------------
    @staticmethod
    def _track(item: dict) -> dict:
        images = (item.get("album") or {}).get("images") or []
        return {
            "source": "spotify",
            "source_id": item["id"],
            "title": item.get("name", ""),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "album": (item.get("album") or {}).get("name", ""),
            "duration_ms": item.get("duration_ms", 0),
            "art_url": images[-1]["url"] if images else "",
        }

    async def search(self, query: str) -> list[dict]:
        r = await self._call("GET", "/search", params={"q": query, "type": "track", "limit": 10})
        return [self._track(i) for i in r.json().get("tracks", {}).get("items", []) if i]

    async def get_track(self, track_id: str) -> dict:
        r = await self._call("GET", f"/tracks/{track_id}")
        return self._track(r.json())

    # -- playback ------------------------------------------------------------
    async def devices(self) -> list[dict]:
        r = await self._call("GET", "/me/player/devices")
        return r.json().get("devices", [])

    async def _device(self) -> str | None:
        if self._device_id:
            return self._device_id
        if not self.s.spotify_device_name:
            return None  # play on whatever device is active
        want = self.s.spotify_device_name.lower()
        for d in await self.devices():
            if d.get("name", "").lower() == want:
                self._device_id = d["id"]
                return self._device_id
        raise SpotifyError(
            f"Spotify device '{self.s.spotify_device_name}' not found. Open the Spotify app and select the WiiM "
            "once so it shows up as a Connect device."
        )

    async def play(self, track_id: str) -> None:
        params = {}
        device = await self._device()
        if device:
            params["device_id"] = device
        try:
            await self._call("PUT", "/me/player/play", params=params, json={"uris": [f"spotify:track:{track_id}"]})
        except SpotifyError:
            self._device_id = None  # device ids can change after a reboot; look it up again next time
            raise
        # don't let Spotify wander off into autoplay/shuffle after our single track
        for path, value in (("/me/player/repeat", "off"), ("/me/player/shuffle", "false")):
            try:
                await self._call("PUT", path, params={"state": value, **params})
            except SpotifyError:
                pass

    async def status(self) -> dict | None:
        r = await self._call("GET", "/me/player")
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    async def pause(self) -> None:
        await self._call("PUT", "/me/player/pause")

    async def resume(self) -> None:
        await self._call("PUT", "/me/player/play")

    async def set_volume(self, volume: int) -> None:
        await self._call("PUT", "/me/player/volume", params={"volume_percent": volume})
