import asyncio
import random
import statistics
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import fairness
from app.config import Settings
from app.main import create_app


# ---------------------------------------------------------------- fairness rules
def test_importance_decays_per_track():
    assert [round(fairness.importance(n), 3) for n in range(4)] == [1.0, 0.5, 0.333, 0.25]
    assert fairness.importance(3, decay=0) == 1.0  # decay 0 switches the rule off


def test_play_next_chance_decays_per_request():
    assert [round(fairness.play_next_chance(k), 3) for k in range(4)] == [1.0, 0.5, 0.333, 0.25]


def test_insertion_respects_play_next_block_and_bounds():
    rng = random.Random(1)
    for _ in range(500):
        i = fairness.insertion_index(10, rng.random(), protected_front=3, rng=rng)
        assert 3 <= i <= 10
    assert fairness.insertion_index(0, 1.0) == 0


def test_low_importance_lands_further_back():
    rng = random.Random(42)
    hi = statistics.mean(fairness.insertion_index(20, 1.0, rng=rng) for _ in range(4000))
    lo = statistics.mean(fairness.insertion_index(20, 0.2, rng=rng) for _ in range(4000))
    assert hi == pytest.approx(10, abs=0.6)  # uniform over 21 slots
    assert lo > 15


# ---------------------------------------------------------------- API end to end
def make_wav(path: Path, seconds: float = 0.2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))


@pytest.fixture
def setup(tmp_path):
    music = tmp_path / "music"
    for i in range(8):
        make_wav(music / f"Artist {i % 3}" / f"Song {i}.wav")
    s = Settings(admin_password="hostpw", party_pin="1234", db_path=str(tmp_path / "j.db"),
                 music_dir=str(music), public_base_url="http://jukebox.lan:8000", player="dummy")
    app = create_app(s, start_engine=False)
    with TestClient(app) as admin, TestClient(app) as alice, TestClient(app) as bob:
        assert admin.post("/api/login", json={"name": "Host", "admin_password": "hostpw"}).status_code == 200
        assert alice.post("/api/login", json={"name": "Alice", "pin": "1234"}).status_code == 200
        assert bob.post("/api/login", json={"name": "Bob", "pin": "1234"}).status_code == 200
        yield app, admin, alice, bob


def local_ids(client):
    return [r["source_id"] for r in client.get("/api/search", params={"source": "local"}).json()]


def test_login_rules(setup):
    app, *_ = setup
    with TestClient(app) as c:
        assert c.post("/api/login", json={"name": "Eve", "pin": "nope"}).status_code == 403
        assert c.post("/api/login", json={"name": "alice", "pin": "1234"}).status_code == 409  # name taken
        assert c.post("/api/login", json={"name": "Eve", "admin_password": "bad"}).status_code == 403
        assert c.get("/api/state").status_code == 401


def test_add_importance_and_guest_cannot_edit(setup):
    app, admin, alice, bob = setup
    ids = local_ids(alice)
    assert len(ids) == 8
    imps = [alice.post("/api/queue", json={"source": "local", "source_id": i}).json()["importance"] for i in ids[:3]]
    assert imps == [1.0, 0.5, 0.333]
    assert alice.post("/api/queue", json={"source": "local", "source_id": ids[0]}).status_code == 409  # duplicate

    q = alice.get("/api/state").json()["queue"]
    assert len(q) == 3
    tid = q[0]["id"]
    assert alice.delete(f"/api/queue/{tid}").status_code == 403
    assert bob.post(f"/api/queue/{tid}/move", json={"index": 2}).status_code == 403
    assert admin.post(f"/api/queue/{tid}/move", json={"index": 2}).status_code == 200
    assert admin.get("/api/state").json()["queue"][2]["id"] == tid
    assert admin.delete(f"/api/queue/{tid}").status_code == 200
    assert [t["index"] for t in admin.get("/api/state").json()["queue"]] == [0, 1]


def test_play_next_first_request_always_wins_then_odds_drop(setup):
    app, admin, alice, bob = setup
    ids = local_ids(alice)
    for i in ids[:4]:
        bob.post("/api/queue", json={"source": "local", "source_id": i})
    r = alice.post("/api/queue", json={"source": "local", "source_id": ids[5], "play_next": True}).json()
    assert r["granted"] and r["chance"] == 1.0 and r["position"] == 0
    me = alice.get("/api/me").json()
    assert me["next_requests"] == 1 and me["next_request_chance"] == 0.5

    # a second winner queues *behind* the first one
    r2 = bob.post("/api/queue", json={"source": "local", "source_id": ids[6], "play_next": True}).json()
    assert r2["granted"] and r2["position"] == 1

    # guests can only request their own tracks
    bobs = [t for t in bob.get("/api/state").json()["queue"] if t["added_by_name"] == "Bob" and not t["play_next"]]
    assert alice.post(f"/api/queue/{bobs[0]['id']}/request-next").status_code == 403


def test_playback_engine_with_dummy_player(setup):
    app, admin, alice, bob = setup
    ids = local_ids(alice)
    for i in ids[:2]:
        alice.post("/api/queue", json={"source": "local", "source_id": i})
    engine = app.state.engine

    async def run():
        await engine.tick()
        now = engine.now_playing()
        assert now and now["status"] == "playing"
        assert engine.media_url(now).startswith("http://jukebox.lan:8000/media/")
        await engine.tick()  # sees it playing
        engine.local.started -= 3600  # fast-forward past the end
        await engine.tick()
        assert engine.now_playing()["id"] != now["id"]
        await engine.skip()
        assert engine.now_playing() is None

    asyncio.run(run())
    hist = admin.get("/api/state").json()["history"]
    assert [h["status"] for h in hist] == ["skipped", "played"]


def test_media_is_served_for_the_speaker(setup):
    app, admin, alice, bob = setup
    tid = local_ids(alice)[0]
    with TestClient(app) as anon:  # the WiiM isn't logged in
        r = anon.get(f"/media/{tid}.wav")
        assert r.status_code == 200 and r.content[:4] == b"RIFF"
        assert anon.get("/media/../../etc/passwd").status_code == 404


def test_admin_can_ban_and_reset(setup):
    app, admin, alice, bob = setup
    ids = local_ids(alice)
    alice.post("/api/queue", json={"source": "local", "source_id": ids[0]})
    uid = alice.get("/api/me").json()["id"]
    assert admin.patch(f"/api/admin/users/{uid}", json={"reset_counts": True}).status_code == 200
    assert alice.get("/api/me").json()["next_track_importance"] == 1.0
    assert admin.patch(f"/api/admin/users/{uid}", json={"banned": True}).status_code == 200
    assert alice.get("/api/state").status_code == 403
