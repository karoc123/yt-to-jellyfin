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
5. **Local Verification:**
   * Run `./run-check.sh` locally to execute `uv sync --locked`, `ruff check`, `ruff format --check`, and `pytest`. All tests are passing green.

---

## Next Tasks (Milestone 1)

1. **Issue #3:** Multi-stage Dockerfile (`uv` -> `python:3.13-slim` with `ffmpeg` and non-root user `1000:1000`) & `docker-compose.yml`.
