"""HTTP API + web UI."""
from __future__ import annotations

import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import fairness, library
from .config import Settings, load_settings
from .db import DB
from .engine import Engine
from .spotify import Spotify, SpotifyError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jukebox")
STATIC = Path(__file__).parent / "static"


# ---- request bodies ----
class LoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    pin: str = ""
    admin_password: str = ""


class AddIn(BaseModel):
    source: str
    source_id: str
    play_next: bool = False


class MoveIn(BaseModel):
    index: int


class VolumeIn(BaseModel):
    volume: int = Field(ge=0, le=100)


class UserPatch(BaseModel):
    banned: bool | None = None
    is_admin: bool | None = None
    reset_counts: bool = False



def create_app(settings: Settings | None = None, start_engine: bool = True) -> FastAPI:
    s = settings or load_settings()
    db = DB(s.db_path)
    if not s.secret_key:  # generate once and keep it, so logins survive restarts
        s.secret_key = db.get_kv("secret_key") or secrets.token_hex(32)
        db.set_kv("secret_key", s.secret_key)
    spotify = Spotify(s, db)
    engine = Engine(s, db, spotify)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        library.scan(db, s.music_dir)
        if start_engine:
            engine.start()
        yield
        await engine.stop()

    app = FastAPI(title="Party Jukebox", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=s.secret_key, max_age=60 * 60 * 24 * 30, same_site="lax")
    app.state.db, app.state.engine, app.state.spotify, app.state.settings = db, engine, spotify, s

    # -- auth helpers ------------------------------------------------------
    def current_user(request: Request) -> dict:
        uid = request.session.get("uid")
        user = db.user(uid) if uid else None
        if not user:
            raise HTTPException(401, "Please log in")
        if user["banned"]:
            raise HTTPException(403, "You've been removed from this party")
        return user

    def admin_user(user: dict = Depends(current_user)) -> dict:
        if not user["is_admin"]:
            raise HTTPException(403, "Admins only")
        return user

    def user_stats(user: dict) -> dict:
        return {
            "id": user["id"],
            "name": user["name"],
            "is_admin": bool(user["is_admin"]),
            "tracks_added": user["tracks_added"],
            "next_requests": user["next_requests"],
            "next_granted": user["next_granted"],
            "next_track_importance": round(fairness.importance(user["tracks_added"], s.importance_decay), 3),
            "next_request_chance": round(fairness.play_next_chance(user["next_requests"], s.play_next_decay), 3),
        }

    # -- pages -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html")

    # -- login -------------------------------------------------------------

    @app.post("/api/login")
    def login(body: LoginIn, request: Request):
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(400, "Pick a name")
        wants_admin = bool(body.admin_password)
        if wants_admin and not hmac.compare_digest(body.admin_password, s.admin_password):
            raise HTTPException(403, "Wrong admin password")
        if not wants_admin and s.party_pin and not hmac.compare_digest(body.pin.strip(), s.party_pin):
            raise HTTPException(403, "Wrong party PIN")

        user = db.user_by_name(name)
        if user is None:
            user = db.create_user(name, wants_admin)
        elif wants_admin:
            db.run("UPDATE users SET is_admin=1 WHERE id=?", user["id"])
        elif request.session.get("uid") != user["id"]:
            # names are claimed by the first phone that uses them
            raise HTTPException(409, f"'{name}' is already taken - pick another name")
        if user["banned"]:
            raise HTTPException(403, "You've been removed from this party")
        request.session["uid"] = user["id"]
        return user_stats(db.user(user["id"]))

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return user_stats(user)

    # -- state -------------------------------------------------------------
    def public_track(t: dict, index: int | None = None) -> dict:
        keys = ("id", "source", "title", "artist", "album", "duration_ms", "art_url", "added_by",
                "added_by_name", "importance", "play_next", "status", "position_ms")
        out = {k: t.get(k) for k in keys if k in t}
        if index is not None:
            out["index"] = index
        return out

    @app.get("/api/state")
    def state(user: dict = Depends(current_user)):
        now = engine.now_playing()
        return {
            "me": user_stats(user),
            "now_playing": public_track(now) if now else None,
            "queue": [public_track(t, i) for i, t in enumerate(db.queue())],
            "history": [public_track(t) for t in db.history(10)],
            "paused": engine.paused,
            "error": engine.last_error if user["is_admin"] else "",
            "sources": {"spotify": spotify.connected, "local": bool(db.one("SELECT 1 FROM library LIMIT 1"))},
            "spotify_configured": spotify.configured,
        }

    # -- search ------------------------------------------------------------
    @app.get("/api/search")
    async def search(q: str = "", source: str = "spotify", user: dict = Depends(current_user)):
        if source == "local":
            rows = library.search(db, q)
            return [{"source": "local", "source_id": r["id"], "title": r["title"], "artist": r["artist"],
                     "album": r["album"], "duration_ms": r["duration_ms"], "art_url": ""} for r in rows]
        if not q.strip():
            return []
        try:
            return await spotify.search(q)
        except SpotifyError as e:
            raise HTTPException(502, str(e))

    # -- adding / requesting -------------------------------------------------

    def request_next(user: dict, track_id: int) -> dict:
        """Roll the dice on a play-next request. Every attempt lowers the user's future odds."""
        granted, chance = fairness.roll_play_next(user["next_requests"], s.play_next_decay)
        with db.transaction():
            db.run("UPDATE users SET next_requests=next_requests+1, next_granted=next_granted+? WHERE id=?",
                   int(granted), user["id"])
            if granted:
                slot = db.play_next_count()  # behind any earlier winners
                db.run("UPDATE tracks SET play_next=1 WHERE id=?", track_id)
                db.place(track_id, slot)
        return {"requested": True, "granted": granted, "chance": round(chance, 3)}

    @app.post("/api/queue")
    async def add_track(body: AddIn, user: dict = Depends(current_user)):
        if body.source == "local":
            row = db.one("SELECT * FROM library WHERE id=?", body.source_id)
            if not row:
                raise HTTPException(404, "Track not found in the local library")
            meta = {"title": row["title"], "artist": row["artist"], "album": row["album"],
                    "duration_ms": row["duration_ms"], "art_url": ""}
        elif body.source == "spotify":
            try:
                meta = await spotify.get_track(body.source_id)
            except SpotifyError as e:
                raise HTTPException(502, str(e))
        else:
            raise HTTPException(400, "Unknown source")

        if db.is_queued_or_playing(body.source, body.source_id):
            raise HTTPException(409, "That track is already in the queue")

        with db.transaction():
            fresh = db.user(user["id"])
            weight = fairness.importance(fresh["tracks_added"], s.importance_decay)
            queue_len = len(db.queue())
            index = fairness.insertion_index(queue_len, weight, protected_front=db.play_next_count())
            tid = db.run(
                "INSERT INTO tracks(source,source_id,title,artist,album,duration_ms,art_url,added_by,importance,added_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                body.source, body.source_id, meta["title"], meta["artist"], meta["album"],
                meta["duration_ms"], meta["art_url"], user["id"], weight, time.time(),
            )
            db.place(tid, index)
            db.run("UPDATE users SET tracks_added=tracks_added+1 WHERE id=?", user["id"])

        result = {"id": tid, "importance": round(weight, 3), "requested": False, "granted": False}
        if body.play_next:
            result.update(request_next(db.user(user["id"]), tid))
        result["position"] = next((i for i, t in enumerate(db.queue()) if t["id"] == tid), None)
        return result

    @app.post("/api/queue/{track_id}/request-next")
    def request_next_route(track_id: int, user: dict = Depends(current_user)):
        t = db.track(track_id)
        if not t or t["status"] != "queued":
            raise HTTPException(404, "Track isn't in the queue")
        if t["added_by"] != user["id"] and not user["is_admin"]:
            raise HTTPException(403, "You can only request your own tracks")
        if t["play_next"]:
            raise HTTPException(409, "That track is already up next")
        result = request_next(user, track_id)
        result["position"] = next((i for i, q in enumerate(db.queue()) if q["id"] == track_id), None)
        return result

    # -- admin: queue editing ------------------------------------------------

    @app.delete("/api/queue/{track_id}")
    def remove_track(track_id: int, _: dict = Depends(admin_user)):
        t = db.track(track_id)
        if not t or t["status"] != "queued":
            raise HTTPException(404, "Track isn't in the queue")
        with db.transaction():
            db.run("UPDATE tracks SET status='removed', position=NULL, finished_at=? WHERE id=?", time.time(), track_id)
            for pos, row in enumerate(db.queue()):
                db.run("UPDATE tracks SET position=? WHERE id=?", pos, row["id"])
        return {"ok": True}

    @app.post("/api/queue/{track_id}/move")
    def move_track(track_id: int, body: MoveIn, _: dict = Depends(admin_user)):
        t = db.track(track_id)
        if not t or t["status"] != "queued":
            raise HTTPException(404, "Track isn't in the queue")
        db.place(track_id, body.index)
        return {"ok": True}

    @app.post("/api/queue/clear")
    def clear_queue(_: dict = Depends(admin_user)):
        db.run("UPDATE tracks SET status='removed', position=NULL, finished_at=? WHERE status='queued'", time.time())
        return {"ok": True}

    # -- admin: playback -----------------------------------------------------

    @app.post("/api/player/{action}")
    async def player_action(action: str, _: dict = Depends(admin_user)):
        actions = {"skip": engine.skip, "pause": engine.pause, "resume": engine.resume}
        if action not in actions:
            raise HTTPException(404, "Unknown action")
        try:
            await actions[action]()
        except Exception as e:
            raise HTTPException(502, f"Speaker error: {e}")
        return {"ok": True, "paused": engine.paused}

    @app.post("/api/volume")
    async def volume(body: VolumeIn, _: dict = Depends(admin_user)):
        try:
            await engine.set_volume(body.volume)
        except Exception as e:
            raise HTTPException(502, f"Speaker error: {e}")
        return {"ok": True}

    # -- admin: users & library -----------------------------------------------

    @app.get("/api/admin/users")
    def list_users(_: dict = Depends(admin_user)):
        return [user_stats(u) | {"banned": bool(u["banned"])} for u in db.all("SELECT * FROM users ORDER BY name")]

    @app.patch("/api/admin/users/{user_id}")
    def patch_user(user_id: int, body: UserPatch, admin: dict = Depends(admin_user)):
        if not db.user(user_id):
            raise HTTPException(404, "No such user")
        if body.banned is not None:
            if user_id == admin["id"]:
                raise HTTPException(400, "You can't ban yourself")
            db.run("UPDATE users SET banned=? WHERE id=?", int(body.banned), user_id)
        if body.is_admin is not None:
            db.run("UPDATE users SET is_admin=? WHERE id=?", int(body.is_admin), user_id)
        if body.reset_counts:
            db.run("UPDATE users SET tracks_added=0, next_requests=0, next_granted=0 WHERE id=?", user_id)
        return {"ok": True}

    @app.delete("/api/admin/users/{user_id}")
    def delete_user(user_id: int, admin: dict = Depends(admin_user)):
        """Frees up the name (e.g. someone changed phone). Their queued tracks stay."""
        if user_id == admin["id"]:
            raise HTTPException(400, "You can't remove yourself")
        db.run("UPDATE tracks SET added_by=NULL WHERE added_by=?", user_id)
        db.run("DELETE FROM users WHERE id=?", user_id)
        return {"ok": True}

    @app.post("/api/admin/rescan")
    def rescan(_: dict = Depends(admin_user)):
        return {"tracks": library.scan(db, s.music_dir)}

    # -- media for the speaker -------------------------------------------------
    @app.get("/media/{name}")
    def media(name: str):
        # served without login: the WiiM / Home Assistant fetches it directly
        path = library.resolve_path(db, s.music_dir, name.split(".")[0])
        if not path:
            raise HTTPException(404)
        return FileResponse(path)

    # -- Spotify connection (admin) -----------------------------------------------
    @app.get("/spotify/login")
    def spotify_login(request: Request, _: dict = Depends(admin_user)):
        if not spotify.configured:
            raise HTTPException(400, "Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first")
        url, state_token = spotify.authorize_url()
        # kept in the DB (not the session) because the callback may arrive on a different
        # host name, e.g. 127.0.0.1, which Spotify requires for non-https redirect URIs
        db.set_kv("spotify_state", state_token)
        return RedirectResponse(url)

    @app.get("/spotify/callback")
    async def spotify_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(400, f"Spotify said: {error}")
        expected = db.get_kv("spotify_state")
        db.set_kv("spotify_state", None)
        if not state or state != expected:
            raise HTTPException(400, "Spotify login state mismatch - start again from the admin page")
        try:
            await spotify.exchange_code(code)
        except SpotifyError as e:
            raise HTTPException(502, str(e))
        return RedirectResponse("/?spotify=connected")

    @app.post("/spotify/disconnect")
    def spotify_disconnect(_: dict = Depends(admin_user)):
        spotify.disconnect()
        return {"ok": True}

    @app.get("/api/admin/spotify-devices")
    async def spotify_devices(_: dict = Depends(admin_user)):
        try:
            return [{"name": d.get("name"), "type": d.get("type"), "active": d.get("is_active")}
                    for d in await spotify.devices()]
        except SpotifyError as e:
            raise HTTPException(502, str(e))

    return app

