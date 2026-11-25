"""Local music library: scans MUSIC_DIR and indexes tags into SQLite."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .db import DB

log = logging.getLogger("jukebox.library")

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape"}


def _tag(tags, key: str) -> str:
    if not tags:
        return ""
    val = tags.get(key)
    if isinstance(val, list):
        val = val[0] if val else ""
    return str(val or "").strip()


def scan(db: DB, music_dir: str) -> int:
    """(Re)index every audio file under music_dir. Returns the number of tracks found."""
    try:
        import mutagen
    except ImportError:  # pragma: no cover
        mutagen = None

    root = Path(music_dir)
    if not root.is_dir():
        log.warning("MUSIC_DIR %s does not exist; local library is empty", root)
        db.run("DELETE FROM library")
        return 0

    rows = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        # sensible fallbacks for untagged files laid out as Artist/Album/Track
        parents = path.relative_to(root).parent.parts
        title, duration = path.stem, 0
        artist = parents[0] if parents else ""
        album = parents[1] if len(parents) > 1 else ""
        if mutagen:
            try:
                f = mutagen.File(path, easy=True)
                if f is not None:
                    title = _tag(f.tags, "title") or title
                    artist = _tag(f.tags, "artist") or _tag(f.tags, "albumartist") or artist
                    album = _tag(f.tags, "album") or album
                    duration = int((getattr(f.info, "length", 0) or 0) * 1000)
            except Exception as e:  # unreadable tags shouldn't stop the scan
                log.debug("could not read tags from %s: %s", path, e)
        tid = hashlib.sha1(rel.encode()).hexdigest()[:16]
        rows.append((tid, rel, title, artist, album, duration))

    with db.transaction():
        db.conn.execute("DELETE FROM library")
        db.conn.executemany(
            "INSERT OR REPLACE INTO library(id,path,title,artist,album,duration_ms) VALUES(?,?,?,?,?,?)", rows
        )
    log.info("indexed %d local tracks from %s", len(rows), root)
    return len(rows)


def search(db: DB, query: str, limit: int = 30) -> list[dict]:
    terms = [t for t in query.split() if t]
    if not terms:
        return db.all("SELECT * FROM library ORDER BY artist, album, title LIMIT ?", limit)
    where = " AND ".join("(title LIKE ? OR artist LIKE ? OR album LIKE ?)" for _ in terms)
    args: list = []
    for t in terms:
        like = f"%{t}%"
        args += [like, like, like]
    return db.all(f"SELECT * FROM library WHERE {where} ORDER BY artist, album, title LIMIT ?", *args, limit)


def resolve_path(db: DB, music_dir: str, track_id: str) -> Path | None:
    row = db.one("SELECT path FROM library WHERE id=?", track_id)
    if not row:
        return None
    root = Path(music_dir).resolve()
    path = (root / row["path"]).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path
