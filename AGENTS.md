# AGENTS.md

## Purpose & Ethos

`yt-to-jellyfin` is a focused, lightweight YouTube archiver and synchronizer targeting native Jellyfin media libraries.
It is built for families and self-hosters who want hassle-free downloads, clean directory structures, standard `.nfo` metadata, and flexible retention rules without the resource overhead of Elasticsearch or plugin dependencies.

---

## Non-Negotiable Core Rules

1. **Ask for Clarification First:**
   * Before you make broad assumptions or architectural leaps on a given task: **ASK THE USER FOR CLARIFICATION**.
2. **Keep It Lean (Zero Bloat):**
   * Do NOT introduce heavy ORMs (like SQLAlchemy / Django ORM) or external databases (like Redis, Postgres, Elasticsearch).
   * SQLite with WAL mode (`PRAGMA journal_mode=WAL`), foreign keys (`PRAGMA foreign_keys=ON`), and explicit SQL migration files (`PRAGMA user_version`) is the law.
3. **Jellyfin Standards Are Sacred:**
   * Never store videos as cryptic IDs (e.g. `UCxxxx/id.mp4`).
   * Output structure must always remain Jellyfin-compliant: `<Channel>/Season <Year>/<Date> - <Title> [<ID>].mp4` accompanied by valid XML `tvshow.nfo` and episode `.nfo` files.
4. **Local Verification Before Push:**
   * Never rely solely on remote GitHub CI.
   * Run `./run-check.sh` locally (`ruff check`, `ruff format --check`, `pytest`) before committing and pushing.
5. **Minimal Documentation & Pragmatism (Less is More):**
   * Write Markdown only where strictly necessary — less is often more.
   * Avoid creating unnecessary `.md` files or sprawling prose; keep documentation lean, actionable, user-centric, and grounded without corporate marketing fluff.
6. **English UI Text:**
   * All user-facing UI text, buttons, labels, notifications, and tooltips must always be in English.

---

## Technical Stack

* **Language & Runtime:** Python 3.13+ managed with `uv`
* **Web Framework:** FastAPI + Uvicorn
* **Templates & UI:** Jinja2 + HTMX + clean, mobile-first responsive CSS (no heavy Node build-pipeline required for core app)
* **Downloader Core:** `yt-dlp` + `ffmpeg`
* **Testing:** Pytest (unit/integration) + local verification scripts
