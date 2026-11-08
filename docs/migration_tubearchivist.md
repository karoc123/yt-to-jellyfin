# TubeArchivist Migration Guide

`yt-to-jellyfin` provides a dedicated migration CLI utility (`migrate-tubearchivist`) to transition your existing **TubeArchivist** archive into a Jellyfin-compliant TV Show library and populate the SQLite catalog.

---

## 1. What TubeArchivist Stores

TubeArchivist archives videos in a flat folder structure under:
```text
/youtube/<channel_id>/<video_id>.mp4
/youtube/<channel_id>/<video_id>.info.json
/youtube/<channel_id>/<video_id>.jpg
/youtube/<channel_id>/<video_id>.description
```

The `migrate-tubearchivist` tool automatically:
1. Scans the source directory and identifies all video files (`.mp4`, `.mkv`, `.webm`, `.avi`, `.mov`).
2. Extracts metadata from companion `.info.json` (or `.description` and channel-level JSON) with robust fallbacks.
3. Restructures media into Jellyfin TV Show seasons (`/media/<library>/<Channel>/Season <YYYY>/<Date> - <Title> [<ID>].mp4`).
4. Generates standard `tvshow.nfo` and episode `.nfo` files with locked metadata (`<lockdata>true</lockdata>`).
5. Converts or copies video thumbnails to `<video_name>-thumb.jpg`.
6. Populates the SQLite database (`channels` and `videos` tables).

---

## 2. CLI Options

Run `migrate-tubearchivist --help` or `python -m app.migration.tubearchivist --help`:

| Argument | Flag | Default | Description |
|---|---|---|---|
| `--source` | `-s` | *Required* | Path to TubeArchivist media directory (e.g. `/youtube` or `/path/to/archive`) |
| `--dest` | `-d` | `MEDIA_PATH` or `/media` | Target Jellyfin media root directory |
| `--library` | `-l` | `default` | Target library name (e.g. `default`, `kids`, `dokus`, `tech`) |
| `--mode` | `-m` | `copy` | File operation mode: `copy`, `move`, `hardlink`, or `symlink` |
| `--dry-run` | | `False` | Preview scanned items and migration actions without writing files or modifying the database |
| `--db` | | `data/app.db` | Custom path to SQLite database file |

---

## 3. Migration Examples

### A. Preview / Dry-Run (Recommended First)
Preview the migration without altering any files or database records:
```bash
uv run migrate-tubearchivist --source /mnt/tubearchivist/youtube --dest /media --library default --dry-run
```

### B. Instant Hardlink Migration (Zero Disk Space Overhead)
If your TubeArchivist archive and your Jellyfin media share the same filesystem or volume, use `hardlink` for near-instant migration without duplicating disk space:
```bash
uv run migrate-tubearchivist --source /mnt/tubearchivist/youtube --dest /media --library default --mode hardlink
```

### C. Safe Copy Migration
Safely copy video files while preserving original TubeArchivist files:
```bash
uv run migrate-tubearchivist --source /mnt/tubearchivist/youtube --dest /media --library dokus --mode copy
```

### D. Running Migration Inside Docker Container
If running `yt-to-jellyfin` inside Docker, you can mount the TubeArchivist archive folder into the container and execute:
```bash
docker compose exec yt-to-jellyfin migrate-tubearchivist \
  --source /tubearchivist_archive \
  --dest /media \
  --library default \
  --mode hardlink
```
