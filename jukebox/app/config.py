"""Configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # --- access ---
    admin_password: str = ""
    party_pin: str = ""  # optional PIN guests must enter to join
    secret_key: str = ""

    # --- storage / library ---
    db_path: str = "data/jukebox.db"
    music_dir: str = "music"
    # URL the WiiM / Home Assistant uses to fetch local files from this server,
    # e.g. http://192.168.1.50:8000 (must be reachable from the speaker, so not localhost)
    public_base_url: str = "http://127.0.0.1:8000"

    # --- fairness tuning ---
    # importance of a user's n-th track = 1 / (1 + IMPORTANCE_DECAY * (n - 1))
    importance_decay: float = 1.0
    # chance a user's k-th "play next" request is honoured = 1 / (1 + PLAY_NEXT_DECAY * (k - 1))
    play_next_decay: float = 1.0

    # --- playback for local files: wiim | homeassistant | dummy ---
    player: str = "dummy"
    wiim_host: str = ""
    ha_url: str = ""
    ha_token: str = ""
    ha_entity_id: str = ""

    # --- Spotify (optional) ---
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8000/spotify/callback"
    spotify_device_name: str = ""  # the WiiM's Spotify Connect name

    poll_seconds: float = 2.0

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)


def load_settings() -> Settings:
    _load_dotenv(Path(_env("JUKEBOX_ENV_FILE", ".env")))
    s = Settings(
        admin_password=_env("ADMIN_PASSWORD"),
        party_pin=_env("PARTY_PIN"),
        secret_key=_env("SECRET_KEY"),
        db_path=_env("DB_PATH", "data/jukebox.db"),
        music_dir=_env("MUSIC_DIR", "music"),
        public_base_url=_env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        importance_decay=_float("IMPORTANCE_DECAY", 1.0),
        play_next_decay=_float("PLAY_NEXT_DECAY", 1.0),
        player=_env("PLAYER", "dummy").lower(),
        wiim_host=_env("WIIM_HOST"),
        ha_url=_env("HA_URL").rstrip("/"),
        ha_token=_env("HA_TOKEN"),
        ha_entity_id=_env("HA_ENTITY_ID"),
        spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
        spotify_redirect_uri=_env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/spotify/callback"),
        spotify_device_name=_env("SPOTIFY_DEVICE_NAME"),
        poll_seconds=_float("POLL_SECONDS", 2.0),
    )
    if not s.admin_password:
        raise RuntimeError("ADMIN_PASSWORD must be set (see .env.example)")
    return s
