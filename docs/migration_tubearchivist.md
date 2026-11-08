# TubeArchivist Migration Guide

`yt-to-jellyfin` provides migration tools both via the CLI utility (`migrate-tubearchivist`) and directly in the web UI under **Abonnements** to transition your existing **TubeArchivist** archive and actual channel subscriptions into a Jellyfin-compliant TV Show library and SQLite catalog.

---

## 1. Important: Media vs. Subscriptions

In TubeArchivist:
* **Media files** live in `/youtube/<channel_id>/<video_id>.mp4`.
* **Channel subscriptions** (`channel_subscribed: true`) live in TubeArchivist's internal database (API or Elasticsearch).
* **Video metadata** (title, channel, date) also lives only in TubeArchivist's index, not next to the files.

> [!NOTE]
> For `--source`, metadata comes from a companion `<id>.info.json` if present, otherwise from the TubeArchivist API (`--ta-url` + `--ta-token`).
> Videos without metadata from either source are **skipped and listed** in the summary — the source file stays untouched. Re-run with API access to migrate them.

> [!IMPORTANT]
> `yt-to-jellyfin` deliberately **does NOT automatically subscribe** to channels simply because a video exists in the archive (`auto_download = 0`). This ensures that one-off video downloads don't trigger unwanted recurring background downloads.
>
> Your actual TubeArchivist channel subscriptions are migrated explicitly — either from a JSON/OPML export, directly via the TubeArchivist REST API, or through the web UI.

---

## 2. CLI Options

Run `migrate-tubearchivist --help` or `python -m app.migration.tubearchivist --help`:

| Argument | Flag | Default | Description |
|---|---|---|---|
| `--source` | `-s` | *None* | Path to TubeArchivist media folder (e.g. `/youtube` or `/path/to/archive`) |
| `--subscriptions` | `-sub` | *None* | Path to TubeArchivist subscriptions JSON or standard OPML file |
| `--ta-url` | | *None* | TubeArchivist instance URL (e.g. `http://tubearchivist:8000`) |
| `--ta-token` | | *None* | TubeArchivist API token (from *Settings -> Application -> API Token*) |
| `--auto-subscribe-discovered` | | `False` | Also auto-subscribe (`auto_download=1`) to all channels found in video archive |
| `--dest` | `-d` | `MEDIA_PATH` or `/media` | Target Jellyfin media root directory |
| `--library` | `-l` | `default` | Target library name (e.g. `default`, `kids`, `dokus`, `tech`) |
| `--mode` | `-m` | `copy` | File operation mode: `copy`, `move`, `hardlink`, or `symlink` |
| `--dry-run` | | `False` | Preview scanned items and migration actions without modifying files or database |
| `--db` | | `DATABASE_PATH` or `/data/app.db` | Custom path to SQLite database file |

---

## 3. How to Export Subscriptions from TubeArchivist

### Option A: Direct REST API (No manual export needed)
You can let `migrate-tubearchivist` pull your subscriptions directly:
1. In TubeArchivist: Go to **Settings** -> **Application** -> copy your **API Token**.
2. Run:
```bash
uv run migrate-tubearchivist --ta-url http://tubearchivist:8000 --ta-token YOUR_TOKEN --library default
```

### Option B: Export via curl to JSON
Dump your subscribed channels via TubeArchivist's API:
```bash
curl -s "http://<ta-ip>:8000/api/channel/" \
  -H "Authorization: Token YOUR_TOKEN" \
  -H "Content-Type: application/json" > subscriptions.json
```
Or directly from Elasticsearch (`ta_channel` index):
```bash
curl -s -u elastic:PASSWORD -X GET "http://<elasticsearch-host>:9200/ta_channel/_search?size=10000" \
  -H 'Content-Type: application/json' \
  -d '{"query": {"term": {"channel_subscribed": true}}}' > subscriptions.json
```

### Option C: OPML File
Standard OPML files (e.g. exported from YouTube Takeout, NewPipe, or RSS aggregators) are fully supported.

---

## 4. Migration Examples

### A. Migrate Media and Subscriptions Together
Migrates videos into Jellyfin Season folders via hardlinks (metadata from the TubeArchivist API) AND imports all subscriptions via the API:
```bash
uv run migrate-tubearchivist \
  --source /mnt/tubearchivist/youtube \
  --dest /media \
  --ta-url http://tubearchivist:8000 \
  --ta-token YOUR_TOKEN \
  --mode hardlink \
  --library default
```

### B. Migrate Only Subscriptions Directly from Running TubeArchivist
```bash
uv run migrate-tubearchivist \
  --ta-url http://192.168.1.50:8000 \
  --ta-token 9f8a7b6c5d4e... \
  --library default
```

### C. Import via Web UI
Open **`http://<your-ip>:8099/subscriptions`**:
* Expand the **Abonnements importieren (TubeArchivist / OPML / JSON)** section.
* Either upload your `subscriptions.json` / `subscriptions.opml` file, or enter your TubeArchivist URL and API Token.
* Click **Abos importieren**. All active subscriptions will be imported immediately.
