# 🤝 Handoff & Next Steps

Welcome to **yt-to-jellyfin**!

## Current State

The repository bootstrap is complete and verified:
1. **License & Ethos:** [LICENSE](file:///home/karoc/dev/yt-to-jellyfin/LICENSE) and [README.md](file:///home/karoc/dev/yt-to-jellyfin/README.md) set the candid, practical tone, giving credit to MeTube and TubeArchivist and focusing purely on user workflows.
2. **Agent Guardrails:** [AGENTS.md](file:///home/karoc/dev/yt-to-jellyfin/AGENTS.md) specifies non-negotiable rules (clarify first, keep lean, maintain Jellyfin NFO standards, verify locally).
3. **Automation & CI:**
   * Dependabot configured in [.github/dependabot.yml](file:///home/karoc/dev/yt-to-jellyfin/.github/dependabot.yml) with a dedicated group for `yt-dlp`.
   * GitHub Actions workflow in [.github/workflows/ci.yml](file:///home/karoc/dev/yt-to-jellyfin/.github/workflows/ci.yml).
4. **Database & Schema (Issue #2):**
   * SQLite database manager with WAL mode, foreign keys, and SQL migration versioning (`PRAGMA user_version`).
   * Schema migration `001_initial_schema.sql` defining `channels`, `videos`, `jobs`, and `settings` tables.
   * Auto-migration integrated into FastAPI lifespan startup.
5. **Containerization (Issue #3):**
   * Production multi-stage `Dockerfile` (`ghcr.io/astral-sh/uv:latest` builder -> `python:3.13-slim` runner with `ffmpeg`, `curl`, non-root user `1000:1000`, volume mounts for `/data` and `/media`, and container healthcheck).
   * `docker-compose.yml` preconfigured for standard deployment mapping `./data:/data` and `./media:/media`.
   * `.dockerignore` ignoring virtualenvs, cache, and git metadata.
   * GitHub Actions workflow updated to verify Docker image builds and container health in CI.
6. **yt-dlp Wrapper Service & Pipeline (Issue #4):**
   * Configurable download engine in `app/downloader/` (`YtDlpService`, `DownloadOptions`, `POTokenConfig`) supporting format selection, rate-limiting, and sleep intervals.
   * Subtitle retrieval (`.vtt`) and Jellyfin-standard thumbnail convention (`-thumb.jpg`).
   * Netscape `cookies.txt` import and validation helpers.
   * Support for static, dynamic, and HTTP-based PO-token providers (`HttpPOTokenProvider`).
   * Sync and async execution methods (`async_extract_info`, `async_download`) with structured exception mapping and progress hook wrappers.
7. **Local Verification:**
   * Run `./run-check.sh` locally to execute `uv sync --locked`, `ruff check`, `ruff format --check`, and `pytest`. All 19 tests are passing green.

---

## Next Tasks (Milestone 1)

1. **Issue #5:** Jellyfin library layout & NFO generator (standard TV show structure `<Channel>/Season <Year>/<Date> - <Title> [<ID>].mp4`, XML generator for `tvshow.nfo` and episode `.nfo`).
