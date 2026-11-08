"""Application configuration settings loaded from environment or defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """Central configuration for yt-to-jellyfin."""

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "/data")))
    media_dir: Path = field(default_factory=lambda: Path(os.environ.get("MEDIA_DIR", "/media")))
    database_path: Path = field(
        default_factory=lambda: Path(os.environ.get("DATABASE_PATH", "/data/app.db"))
    )
    cookies_path: Path = field(
        default_factory=lambda: Path(os.environ.get("COOKIES_PATH", "/data/cookies.txt"))
    )

    # TubeArchivist & yt-dlp defaults
    default_format: str = os.environ.get("DEFAULT_FORMAT", "bestvideo*+bestaudio/best")
    default_format_sort: str | None = os.environ.get("DEFAULT_FORMAT_SORT")
    default_max_height: int | None = (
        int(os.environ["DEFAULT_MAX_HEIGHT"]) if "DEFAULT_MAX_HEIGHT" in os.environ else None
    )
    default_throttled_rate: int | None = (
        int(os.environ["DEFAULT_THROTTLED_RATE"])
        if "DEFAULT_THROTTLED_RATE" in os.environ
        else None
    )

    # RSS polling interval (in seconds)
    rss_poll_interval_seconds: int = int(os.environ.get("RSS_POLL_INTERVAL", "900"))


settings = Settings()
