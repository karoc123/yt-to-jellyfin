"""yt-dlp wrapper and execution service package."""

from app.downloader.exceptions import (
    CookieError,
    DownloadFailedError,
    VideoUnavailableError,
    YtDlpError,
)
from app.downloader.options import (
    DownloadOptions,
    DownloadResult,
    POTokenConfig,
    ProgressUpdate,
    VideoMetadata,
)
from app.downloader.pot_provider import HttpPOTokenProvider
from app.downloader.service import YtDlpService, downloader, parse_metadata, wrap_progress_hook

__all__ = [
    "CookieError",
    "DownloadFailedError",
    "DownloadOptions",
    "DownloadResult",
    "HttpPOTokenProvider",
    "POTokenConfig",
    "ProgressUpdate",
    "VideoMetadata",
    "VideoUnavailableError",
    "YtDlpError",
    "YtDlpService",
    "downloader",
    "parse_metadata",
    "wrap_progress_hook",
]
