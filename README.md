# yt-to-jellyfin

A lightweight, zero-nonsense YouTube archiver built specifically for **Jellyfin**. It downloads videos, channels, and playlists, organizes files into native TV Show season folders, generates standard Kodi/Jellyfin XML `.nfo` files with locked metadata, and manages automated retention rules — all in a single tiny container that consumes less than 50 MB of RAM.

---

## 🛑 Before You Use This: Credit Where Credit Is Due

Depending on what you actually need, you might prefer an alternative tool:

* If you just want to paste a YouTube link in your browser and download a single video file on demand, check out **[MeTube](https://github.com/alexta69/metube)**. It is simple, fast, and does that single job exceptionally well.
* If you want a full-blown YouTube replacement with comments archiving, Elasticsearch-powered subtitle search, channel analytics, and a multi-gigabyte stack, check out **[TubeArchivist](https://github.com/tubearchivist/tubearchivist)**.

**yt-to-jellyfin** lives right between them:
You run Jellyfin for your family, and you want selected YouTube channels and videos to show up cleanly as regular TV Shows — without installing proprietary Jellyfin plugins, without managing Elasticsearch, and without filling disks with outdated daily news thanks to automatic cleanup policies.

---

## ✨ Features

* **Native Jellyfin Integration (Zero Plugins Required):**
  * Automatically stores media in standard TV Show hierarchy: `<Library>/<Channel>/Season <Year>/<Date> - <Title> [<ID>].mp4`.
  * Generates `tvshow.nfo` in channel folders and episode `.nfo` files with `<lockdata>true</lockdata>` to prevent online scraper mismatches.
  * Writes companion `-thumb.jpg` and channel `poster.jpg` images recognized out-of-the-box.
* **Mobile-First Family UI:**
  * Zero-login, responsive web frontend designed for phones, tablets, and desktop browsers.
  * Global library switcher pills (`All`, `Default`, `Kids`, `Dokus`, `Tech`) persisted via browser `localStorage` and `?lib=` query parameter.
* **Quick Download Interface:**
  * Paste a video or playlist URL, pick maximum resolution (e.g. 1080p, 720p, 4K), optionally toggle SponsorBlock segment removal, and view inline queue progress.
* **Two-Tier Mediathek Browser:**
  * Channel cards showing video counts and library tags.
  * Channel drilldown grid with video thumbnails, durations, publish dates, and pin statuses.
  * Floating action bar for multi-select bulk operations (bulk delete & bulk pin/unpin).
* **Automated Retention Rules & Pinning:**
  * Set per-channel or global retention policies (e.g., delete episodes older than 14 days, or keep only the latest 10 videos).
  * Pin videos (`is_pinned`) to permanently protect favorites from automatic cleanup.
  * Safe atomic filesystem deletion removing video files, NFOs, and companion thumbnails together.
* **TubeArchivist Migration Utility:**
  * Built-in CLI command (`migrate-tubearchivist`) to import existing TubeArchivist archives (`/youtube/<channel_id>/<video_id>.mp4`) with metadata preservation and zero-overhead hardlink support.
* **Lightweight Architecture:**
  * Pure Python 3.13, FastAPI, SQLite with WAL mode, and `yt-dlp`.
  * Background queue with atomic transitions and automatic crash recovery.
  * Background RSS feed poller for periodic channel synchronization.

---

## 🚀 Quickstart with Docker Compose

Run `yt-to-jellyfin` with Docker:

```yaml
services:
  yt-to-jellyfin:
    image: yt-to-jellyfin:latest
    container_name: yt-to-jellyfin
    restart: unless-stopped
    ports:
      - "8099:8000"
    environment:
      - DATABASE_PATH=/data/app.db
      - MEDIA_PATH=/media
      - TZ=Europe/Berlin
      # Optional: Netscape cookies.txt path for age/member-restricted videos
      # - COOKIES_PATH=/cookies/cookies.txt
      # Optional: Format sort parameter (e.g. res:1080,fps)
      # - FORMAT_SORT=res:1080,fps
      # Optional: re-extract downloads slower than N bytes/sec
      # - THROTTLED_RATE_BYTES=5242880
    volumes:
      - ./data:/data
      - ./media:/media
      # Optional: Mount cookies file as read-only
      # - ./cookies.txt:/cookies/cookies.txt:ro
```

Start the service:
```bash
docker compose up -d
```
Open **`http://<your-server-ip>:8099`** in your browser.

---

## ⚙️ Configuration Reference

| Environment Variable | Default | Description |
|---|---|---|
| `MEDIA_PATH` | `/media` | Root folder where Jellyfin library folders (`default`, `kids`, etc.) are created |
| `DATABASE_PATH` | `/data/app.db` | Path to SQLite database (set e.g. `data/app.db` for local dev) |
| `COOKIES_PATH` | `/data/cookies.txt` | Netscape `cookies.txt` for member or age-restricted videos (used only if the file exists) |
| `FORMAT_SORT` | *None* | Custom `yt-dlp` format sorting criteria (e.g. `res:1080,fps,codec:h264`) |
| `THROTTLED_RATE_BYTES` | *None* | Re-extract a download that drops below N bytes/second (yt-dlp `--throttled-rate`) |
| `DEFAULT_FORMAT` | *None* | `yt-dlp` format selector (default `bestvideo*+bestaudio/best`) |
| `DEFAULT_MAX_HEIGHT` | *None* | Maximum video height (e.g. `1080`) when the job does not set one |
| `RSS_POLL_INTERVAL` | `900` | Seconds between RSS polls / retention runs |
| `RETENTION_DAYS` | *None* | Global default: delete unpinned videos older than N days (channel rule wins) |
| `RETENTION_COUNT` | *None* | Global default: keep only the latest N videos per channel (channel rule wins) |
| `TZ` | `Europe/Berlin` | Container timezone |

---

## 📺 Jellyfin Setup (2 Minutes)

Jellyfin natively reads NFO files without needing any custom plugins:

1. In Jellyfin, navigate to **Dashboard** -> **Libraries** -> **Add Media Library**.
2. Set **Content Type** to **Shows** (or "Serien / TV Shows").
3. Set **Display Name** (e.g., `YouTube` or `Kinder YouTube`).
4. Under **Folders**, add the path pointing to `/media` (or `/media/kids` for a dedicated kids library).
5. In **Metadata Readers**, enable **Nfo** and move it to the top of the list.
6. Under **Metadata Downloaders** and **Image Fetchers**, disable online scrapers (TheTVDB, TheMovieDb) to prevent false matches, as all metadata is supplied locally.
7. Click **Ok** and scan the library.

For detailed screenshots and multi-library parental control setups, see [docs/jellyfin_setup.md](docs/jellyfin_setup.md).

---

## 📦 TubeArchivist Migration

Migrate existing TubeArchivist video archives and subscriptions directly into `yt-to-jellyfin`:

```bash
# Preview video archive migration (dry run)
uv run migrate-tubearchivist --source /path/to/tubearchivist/youtube --dest /media --dry-run

# Migrate media and import subscriptions file (JSON or OPML)
uv run migrate-tubearchivist --source /path/to/archive --dest /media --subscriptions /path/to/subscriptions.json --mode hardlink

# Or import subscriptions directly from running TubeArchivist instance:
uv run migrate-tubearchivist --ta-url http://tubearchivist:8000 --ta-token YOUR_API_TOKEN

# Or upload subscriptions.json / OPML in the Web UI under /subscriptions
```

For full CLI documentation and options, see [docs/migration_tubearchivist.md](docs/migration_tubearchivist.md).

---

## 🍪 Authentication & Cookies

To download age-restricted or member-only videos, mount a Netscape `cookies.txt` file exported from your browser. See [docs/cookies.md](docs/cookies.md) for step-by-step export and security guidelines.

---

## 🛠️ Local Development & Testing

This project uses Python 3.13, `uv`, `ruff`, and `pytest`:

```bash
# Install dependencies
uv sync --locked

# Run code checks, linting, and tests
./run-check.sh

# Run test suite directly
uv run pytest
```

---

## 📜 License

[WTFPL / AI Disclaimer](LICENSE) — generated with planet-destroying AI technology; do whatever you want with it.
