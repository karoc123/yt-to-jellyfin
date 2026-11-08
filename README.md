# yt-to-jellyfin

A lightweight, zero-nonsense YouTube archiver built specifically for **Jellyfin**. It downloads videos, channels, and playlists, names files properly, generates standard Kodi/Jellyfin XML `.nfo` files, and manages automated retention rules — all in a single tiny container that consumes less than 50 MB of RAM.

---

## 🛑 Before You Use This: Credit Where Credit Is Due

Depending on what you actually need, you might not even want this tool:

* If you just want to paste a YouTube link in your browser and download a video file on demand, go use **[MeTube](https://github.com/alexta69/metube)**. It is fantastic, simple, and does that single job better than anyone else.
* If you want a full-blown YouTube replacement with comments archiving, Elasticsearch-powered subtitle search, channel analytics, and a multi-gigabyte stack, go use **[TubeArchivist](https://github.com/tubearchivist/tubearchivist)**. It's a massive, powerful suite.

**yt-to-jellyfin** lives right between them:
You have a family, you run Jellyfin, and you just want certain YouTube channels and videos to show up cleanly in Jellyfin as regular TV Shows — without installing proprietary Jellyfin plugins, without babysitting Elasticsearch, and without wasting hundreds of gigabytes on outdated news clips thanks to configurable auto-cleanup rules.

---

## ✨ Features

* **Native Jellyfin Integration (No Plugins Needed):** Stores videos in a standard TV Show hierarchy (`<Channel>/Season <Year>/<Date> - <Title> [<ID>].mp4`) with native `tvshow.nfo` and episode `.nfo` files. Jellyfin reads them out of the box.
* **Dead Simple Family UI:** Clean, responsive, mobile-first web interface. Paste a YouTube link, click download, done.
* **Subscriptions & Automation:** Monitor channels and playlists automatically via lightweight YouTube RSS feeds without hitting aggressive rate limits.
* **Sensible Retention Rules:** Delete news videos after 7 days, keep only the latest 5 videos of a channel, or pin favorite videos to keep them forever.
* **Tiny Footprint:** Pure Python 3.13, FastAPI, SQLite with WAL mode, and yt-dlp. Idle RAM consumption is under 50 MB.
* **Safe Cookie & Quality Handling:** Support for `cookies.txt` (for member/age-gated videos) and resolution caps (e.g. max 1080p).

---

## 🚀 Quickstart

Run it via `docker compose`:

```yaml
services:
  yt-to-jellyfin:
    image: ghcr.io/karoc123/yt-to-jellyfin:latest
    container_name: yt-to-jellyfin
    restart: unless-stopped
    ports:
      - "8099:8000"
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Berlin
    volumes:
      - ./data:/data
      - /path/to/your/jellyfin/media/youtube:/media
```

Start the container:
```bash
docker compose up -d
```
Open **`http://<your-server-ip>:8099`** in your browser.

---

## 📺 Jellyfin Setup (2 Minutes)

You do **not** need to install any plugins in Jellyfin:

1. Open Jellyfin -> **Dashboard** -> **Libraries** -> **Add Media Library**.
2. Content type: **Shows** (or "Serien / TV Shows").
3. Display name: **YouTube** (or whatever you prefer).
4. Folders: Select the folder where yt-to-jellyfin writes its `/media` output.
5. In **Metadata readers**: Ensure **Nfo** is enabled (checked) and placed at the top.
6. Click **Ok** and scan the library. Done! All channel posters, episode metadata, descriptions, and air dates will populate automatically.

---

## 🛠 Local Development & Verification

Requirements: Python 3.13+, `uv`

```bash
# Run code checks, linting, and unit tests
./run-check.sh

# Run full local verification
uv sync --locked
uv run ruff check .
uv run pytest -q
```

---

## 📜 License

[WTFPL / AI Disclaimer](LICENSE) — generated with planet-destroying AI technology; do whatever you want with it.
