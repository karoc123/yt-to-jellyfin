"""Application configuration loaded from environment variables (single source of truth).

Every field reads the environment when `Settings()` is instantiated. Application code reads
the module-level `settings` object at call time instead of copying values at import.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIBRARIES = ("default", "kids", "dokus", "tech")


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int | None = None) -> int | None:
    value = _env_str(name)
    return int(value) if value is not None else default


def _env_libraries() -> list[str]:
    raw = _env_str("LIBRARIES")
    libs = [lib.strip() for lib in raw.split(",") if lib.strip()] if raw else []
    return list(dict.fromkeys(libs)) or list(DEFAULT_LIBRARIES)


@dataclass
class Settings:
    """Central configuration for yt-to-jellyfin (env names as documented in README)."""

    media_dir: Path = field(default_factory=lambda: Path(_env_str("MEDIA_PATH", "/media")))
    database_path: Path = field(
        default_factory=lambda: Path(_env_str("DATABASE_PATH", "/data/app.db"))
    )
    cookies_path: Path = field(
        default_factory=lambda: Path(_env_str("COOKIES_PATH", "/data/cookies.txt"))
    )

    # yt-dlp download defaults (a job payload may override them)
    default_format: str | None = field(default_factory=lambda: _env_str("DEFAULT_FORMAT"))
    default_max_height: int | None = field(default_factory=lambda: _env_int("DEFAULT_MAX_HEIGHT"))
    format_sort: str | None = field(default_factory=lambda: _env_str("FORMAT_SORT"))
    throttled_rate_bytes: int | None = field(
        default_factory=lambda: _env_int("THROTTLED_RATE_BYTES")
    )

    # Global retention defaults for channels without own rules (unset = unlimited)
    retention_days: int | None = field(default_factory=lambda: _env_int("RETENTION_DAYS"))
    retention_count: int | None = field(default_factory=lambda: _env_int("RETENTION_COUNT"))

    # Jellyfin library folders offered in the UI
    libraries: list[str] = field(default_factory=_env_libraries)

    # RSS polling / scheduler interval (in seconds)
    rss_poll_interval_seconds: int = field(
        default_factory=lambda: _env_int("RSS_POLL_INTERVAL", 900)
    )


settings = Settings()
