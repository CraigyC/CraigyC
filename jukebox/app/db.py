"""SQLite storage. All access goes through one connection guarded by a lock."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    tracks_added INTEGER NOT NULL DEFAULT 0,
    next_requests INTEGER NOT NULL DEFAULT 0,
    next_granted INTEGER NOT NULL DEFAULT 0,
    banned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,            -- 'spotify' | 'local'
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    art_url TEXT NOT NULL DEFAULT '',
    added_by INTEGER REFERENCES users(id),
    importance REAL NOT NULL DEFAULT 1.0,
    position INTEGER,                -- order in the upcoming queue (only while queued)
    play_next INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|playing|played|skipped|removed|error
    added_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS tracks_status ON tracks(status, position);
CREATE TABLE IF NOT EXISTS library (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    # -- helpers ---------------------------------------------------------
    def all(self, sql: str, *args: Any) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, *args: Any) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def run(self, sql: str, *args: Any) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def transaction(self):
        return _Tx(self)

    # -- key/value -------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key=?", key)
        return row["value"] if row else default

    def set_kv(self, key: str, value: str | None) -> None:
        if value is None:
            self.run("DELETE FROM kv WHERE key=?", key)
        else:
            self.run("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)

    # -- users -----------------------------------------------------------
    def user(self, uid: int) -> dict | None:
        return self.one("SELECT * FROM users WHERE id=?", uid)

    def user_by_name(self, name: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE name=?", name)

    def create_user(self, name: str, is_admin: bool) -> dict:
        uid = self.run("INSERT INTO users(name,is_admin,created_at) VALUES(?,?,?)", name, int(is_admin), time.time())
        return self.user(uid)

    # -- queue -----------------------------------------------------------
    def queue(self) -> list[dict]:
        return self.all(
            "SELECT t.*, u.name AS added_by_name FROM tracks t LEFT JOIN users u ON u.id=t.added_by "
            "WHERE t.status='queued' ORDER BY t.position"
        )

    def current(self) -> dict | None:
        return self.one(
            "SELECT t.*, u.name AS added_by_name FROM tracks t LEFT JOIN users u ON u.id=t.added_by "
            "WHERE t.status='playing' ORDER BY t.started_at DESC LIMIT 1"
        )

    def history(self, limit: int = 10) -> list[dict]:
        return self.all(
            "SELECT t.*, u.name AS added_by_name FROM tracks t LEFT JOIN users u ON u.id=t.added_by "
            "WHERE t.status IN ('played','skipped','error') ORDER BY t.finished_at DESC LIMIT ?",
            limit,
        )

    def track(self, tid: int) -> dict | None:
        return self.one("SELECT * FROM tracks WHERE id=?", tid)

    def is_queued_or_playing(self, source: str, source_id: str) -> bool:
        return bool(self.one(
            "SELECT 1 FROM tracks WHERE source=? AND source_id=? AND status IN ('queued','playing')", source, source_id
        ))

    def play_next_count(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM tracks WHERE status='queued' AND play_next=1")["n"]

    def place(self, tid: int, index: int) -> None:
        """Move a queued track to a 0-based index, renumbering the rest."""
        with self.transaction():
            ids = [r["id"] for r in self.all("SELECT id FROM tracks WHERE status='queued' AND id<>? ORDER BY position", tid)]
            index = max(0, min(index, len(ids)))
            ids.insert(index, tid)
            for pos, i in enumerate(ids):
                self.conn.execute("UPDATE tracks SET position=? WHERE id=?", (pos, i))

    def pop_next(self) -> dict | None:
        with self.transaction():
            nxt = self.one("SELECT * FROM tracks WHERE status='queued' ORDER BY position LIMIT 1")
            if nxt:
                self.conn.execute(
                    "UPDATE tracks SET status='playing', position=NULL, play_next=0, started_at=? WHERE id=?",
                    (time.time(), nxt["id"]),
                )
            return nxt

    def finish(self, tid: int, status: str = "played") -> None:
        self.run("UPDATE tracks SET status=?, finished_at=? WHERE id=?", status, time.time(), tid)


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.nested = self.db.conn.in_transaction
        if not self.nested:
            self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            if not self.nested:
                self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
