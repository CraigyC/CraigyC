"""Playback engine: keeps the speaker fed from the queue."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .config import Settings
from .db import DB
from .players import DummyPlayer, Player, SpotifyPlayer, Status, make_local_player
from .spotify import Spotify

log = logging.getLogger("jukebox.engine")

START_TIMEOUT = 25  # seconds a track may take to start before we give up on it


class Engine:
    def __init__(self, settings: Settings, db: DB, spotify: Spotify):
        self.s, self.db = settings, db
        self.local = make_local_player(settings)
        self.spotify = DummyPlayer() if isinstance(self.local, DummyPlayer) else SpotifyPlayer(spotify)
        self.lock = asyncio.Lock()
        self.paused = db.get_kv("paused") == "1"
        self.last_status = Status()
        self.last_error = ""
        self._seen_playing = False
        self._task: asyncio.Task | None = None

    # -- helpers -----------------------------------------------------------
    def player_for(self, track: dict) -> Player:
        return self.spotify if track["source"] == "spotify" else self.local

    def media_url(self, track: dict) -> str | None:
        if track["source"] != "local":
            return None
        row = self.db.one("SELECT path FROM library WHERE id=?", track["source_id"])
        ext = Path(row["path"]).suffix.lower() if row else ""
        return f"{self.s.public_base_url}/media/{track['source_id']}{ext}"

    def _set_paused(self, value: bool) -> None:
        self.paused = value
        self.db.set_kv("paused", "1" if value else "0")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        # a track left "playing" from a previous run can't be resumed reliably; put it back on top
        cur = self.db.current()
        if cur:
            self.db.run("UPDATE tracks SET status='queued', started_at=NULL WHERE id=?", cur["id"])
            self.db.place(cur["id"], 0)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the loop die
                log.exception("engine tick failed")
                self.last_error = str(e)
            await asyncio.sleep(self.s.poll_seconds)

    # -- core --------------------------------------------------------------
    async def tick(self) -> None:
        async with self.lock:
            cur = self.db.current()
            if cur is None:
                if not self.paused:
                    await self._start_next()
                return
            if await self._is_finished(cur):
                self.db.finish(cur["id"], "played")
                await self._start_next()

    async def _is_finished(self, cur: dict) -> bool:
        try:
            st = await self.player_for(cur).status()
        except Exception as e:
            log.warning("status failed: %s", e)
            self.last_error = str(e)
            return False
        self.last_status = st
        elapsed = time.time() - (cur["started_at"] or time.time())
        expected_ident = cur["source_id"] if cur["source"] == "spotify" else None

        if st.state == "playing" and (expected_ident is None or st.ident == expected_ident):
            self._seen_playing = True
            return False
        if self.paused:
            return False
        if not self._seen_playing:
            if elapsed > START_TIMEOUT:
                log.warning("track %s never started; skipping", cur["title"])
                self.last_error = f"'{cur['title']}' didn't start on the speaker"
                return True
            return False
        if expected_ident and st.ident and st.ident != expected_ident:
            return True  # Spotify moved on to something else
        if st.state == "stopped":
            return True
        # Spotify reports a finished single track as paused at 0 or at the very end
        dur = st.duration_ms or cur["duration_ms"]
        return st.state == "paused" and (st.position_ms < 1000 or (dur and st.position_ms >= dur - 3000))

    async def _start_next(self) -> None:
        for _ in range(5):  # skip over tracks that fail to start
            nxt = self.db.pop_next()
            if not nxt:
                return
            self._seen_playing = False
            try:
                await self.player_for(nxt).play(nxt, self.media_url(nxt))
                self.last_error = ""
                log.info("now playing: %s - %s", nxt["artist"], nxt["title"])
                return
            except Exception as e:
                log.warning("could not play %s: %s", nxt["title"], e)
                self.last_error = f"Couldn't play '{nxt['title']}': {e}"
                self.db.finish(nxt["id"], "error")

    # -- admin controls ----------------------------------------------------
    async def skip(self) -> None:
        async with self.lock:
            cur = self.db.current()
            if cur:
                self.db.finish(cur["id"], "skipped")
            self._set_paused(False)
            if not self.db.queue() and cur:
                await self.player_for(cur).stop()
            await self._start_next()

    async def pause(self) -> None:
        async with self.lock:
            self._set_paused(True)
            cur = self.db.current()
            if cur:
                await self.player_for(cur).pause()

    async def resume(self) -> None:
        async with self.lock:
            self._set_paused(False)
            cur = self.db.current()
            if cur:
                await self.player_for(cur).resume()
            else:
                await self._start_next()

    async def set_volume(self, volume: int) -> None:
        cur = self.db.current()
        await (self.player_for(cur) if cur else self.local).set_volume(volume)

    def now_playing(self) -> dict | None:
        cur = self.db.current()
        if not cur:
            return None
        st = self.last_status
        pos = st.position_ms if self._seen_playing else 0
        return {**cur, "position_ms": pos, "duration_ms": st.duration_ms or cur["duration_ms"]}
