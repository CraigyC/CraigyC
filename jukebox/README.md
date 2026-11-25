# Party Jukebox

A self-hosted party jukebox. Guests open a web page on their phones, pick a name and add songs from
**Spotify** or a **local music folder**. The music plays on a **WiiM** streamer, either directly or
through **Home Assistant**. Only the host can edit the queue.

## How the fairness works

| Rule | Default behaviour |
|---|---|
| **Importance.** Each track a guest adds counts for less than their last one. | 1st track 100%, 2nd 50%, 3rd 33%, 4th 25% … |
| **Shuffle by importance.** A new track drops into a random slot in the queue. Lower importance pushes it towards the back. | At 100% any slot is equally likely. At 25% it lands near the end. |
| **Play-next requests.** Guests can ask for a track to play next. Every request they make lowers the odds of the next one. | 1st request always wins, then 50%, 33%, 25% … Winners queue up in the order they won. |
| **Host powers.** Only the host can remove, reorder, skip, pause or clear tracks. The host can also reset a guest's counts or ban them. | |

Both curves can be tuned in `.env` (`IMPORTANCE_DECAY`, `PLAY_NEXT_DECAY`). Set either to `0` to turn that rule off.
The rules live in [`app/fairness.py`](app/fairness.py).

Each guest sees their own current importance and their play-next odds. Every queued track shows who added it and its importance.

## How playback works

```
 phones ──► Jukebox (this app) ──┬─ local files ─► WiiM HTTP API ─────────────► WiiM
                                 │                 (or Home Assistant play_media)
                                 └─ Spotify ─────► Spotify Web API ─► Spotify Connect ─► WiiM
```

* **Local files** are served by the jukebox at `PUBLIC_BASE_URL/media/...`. The WiiM is told to play that URL.
  `PLAYER=wiim` talks to the WiiM's own HTTP API, which is the simplest option and needs no Home Assistant.
  `PLAYER=homeassistant` sends the URL to a HA `media_player` entity instead.
* **Spotify** tracks are started on the WiiM through Spotify Connect, using the host's account.
  **Spotify Premium is required.**
* The app checks the speaker every 2 seconds. When a track ends it starts the next one.
  Spotify's repeat and shuffle are switched off so Spotify doesn't wander into autoplay.

## Run it

### Docker (recommended: a NAS, Raspberry Pi, or the Home Assistant box)

```bash
cd jukebox
cp .env.example .env        # then edit it
mkdir -p data music         # or change the music volume in docker-compose.yml
docker compose up -d --build
```

Then open `http://<server-ip>:8000` and log in as the host using **"I'm the host"** and your `ADMIN_PASSWORD`.
Guests just need the same URL. A QR code of it on the fridge works well.

### Without Docker

```bash
cd jukebox
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # edit it; set MUSIC_DIR
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

To try it with no speaker or Spotify, set `PLAYER=dummy`. Playback is then simulated.

## Setting up the speaker

**WiiM direct (`PLAYER=wiim`)**
1. Find the WiiM's IP address in the WiiM Home app (device settings → network) and put it in `WIIM_HOST`.
2. Set `PUBLIC_BASE_URL` to this server's LAN address, e.g. `http://192.168.1.50:8000`. The WiiM fetches files from there,
   so it can't be `localhost`.

**Via Home Assistant (`PLAYER=homeassistant`)**
1. Add the WiiM to HA using the built-in **LinkPlay** integration, or the WiiM integration.
2. Create a long-lived access token: your HA profile → Security.
3. Set `HA_URL`, `HA_TOKEN` and `HA_ENTITY_ID` (e.g. `media_player.wiim_pro`), plus `PUBLIC_BASE_URL` as above.

## Setting up Spotify (optional)

1. Create an app at <https://developer.spotify.com/dashboard>. Tick **Web API**.
   Add the redirect URI `http://127.0.0.1:8000/spotify/callback`.
2. Put the client ID and secret in `.env`.
3. Open the jukebox **on the server itself** at `http://127.0.0.1:8000` and log in as the host. Then click **Connect Spotify**.
   This step has to use 127.0.0.1 because Spotify only allows plain-`http` redirects to loopback addresses.
   If the server is headless, either use an SSH tunnel (`ssh -L 8000:localhost:8000 server`) and open 127.0.0.1:8000 on your laptop,
   or put the app behind HTTPS and use that redirect URI instead. You only need to do this once; the login is remembered.
4. Open Spotify on your phone and pick the WiiM as the output once, so Spotify knows about it.
   Then click **Show Spotify devices** on the host page and copy the WiiM's name into `SPOTIFY_DEVICE_NAME`.
5. Guests can now search Spotify. Searches go through the host's account.

## Notes and limits

* Guests sign in with just a name, plus the `PARTY_PIN` if you set one. The first phone to use a name keeps it.
  If someone switches phones, the host can use 🗑 to free their name.
  Someone could rejoin under a new name to get full importance again. The host can see and ban this.
* `/media/...` URLs work without login so that the speaker can fetch them. Run the app on your home network only.
  Don't expose it to the internet as-is.
* Queues, guests and the Spotify login are stored in SQLite (`data/jukebox.db`), so they survive restarts.
* Tests: `pip install pytest && python -m pytest`

## API sketch

| Method | Path | Who |
|---|---|---|
| POST | `/api/login`, `/api/logout` | anyone |
| GET | `/api/state` (now playing, queue, history, your stats) | guests |
| GET | `/api/search?source=spotify\|local&q=` | guests |
| POST | `/api/queue` `{source, source_id, play_next}` | guests |
| POST | `/api/queue/{id}/request-next` | the guest who added it |
| DELETE | `/api/queue/{id}` · POST `/api/queue/{id}/move` `{index}` · POST `/api/queue/clear` | host |
| POST | `/api/player/{pause\|resume\|skip}` · POST `/api/volume` | host |
| GET/PATCH/DELETE | `/api/admin/users[/{id}]` · POST `/api/admin/rescan` | host |
