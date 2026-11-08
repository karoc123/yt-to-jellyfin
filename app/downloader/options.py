"""Data structures and configuration models for yt-dlp operations."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class POTokenConfig:
    """Proof of Origin (PO-token) configuration for YouTube client emulation."""

    token: str | None = None
    visitor_data: str | None = None
    provider: Callable[[], tuple[str, str | None] | str | None] | None = None


@dataclass
class DownloadOptions:
    """Configurable options for yt-dlp download execution."""

    output_dir: Path | str = "/media"
    output_template: str = "%(title)s [%(id)s].%(ext)s"

    # Subtitles
    download_subtitles: bool = True
    subtitles_langs: list[str] = field(default_factory=lambda: ["en", "de"])
    write_auto_sub: bool = True
    subtitles_format: str = "vtt"

    # Thumbnails
    write_thumbnail: bool = True
    convert_thumbnail: str = "jpg"
    thumbnail_suffix: str = "-thumb"

    # Rate limiting & sleep intervals
    rate_limit_bytes: int | None = None
    sleep_interval: float | None = None
    max_sleep_interval: float | None = None
    sleep_requests: float | None = None

    # Cookies
    cookies_path: Path | str | None = None

    # PO-Token
    po_token_config: POTokenConfig | None = None

    # Quality & Formats
    format_selector: str = "bestvideo*+bestaudio/best"
    max_height: int | None = None
    merge_output_format: str = "mp4"

    # SponsorBlock
    sponsorblock_remove: list[str] = field(default_factory=list)
    sponsorblock_mark: list[str] = field(default_factory=list)

    # Execution overrides
    ffmpeg_path: str | None = None
    extra_params: dict = field(default_factory=dict)


@dataclass
class VideoMetadata:
    """Structured video metadata extracted by yt-dlp."""

    id: str
    title: str
    channel: str
    channel_id: str
    channel_url: str
    upload_date: str
    duration: int
    description: str = ""
    thumbnail_url: str = ""
    webpage_url: str = ""
    filesize_approx: int | None = None
    tags: list[str] = field(default_factory=list)
    raw_info: dict = field(default_factory=dict)


@dataclass
class ProgressUpdate:
    """Standardized progress update emitted during downloads."""

    status: str
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    percentage: float = 0.0
    speed_bytes_per_sec: float | None = None
    eta_seconds: int | None = None
    filename: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class DownloadResult:
    """Artifacts produced by a completed download run."""

    id: str
    metadata: VideoMetadata
    video_path: Path
    thumbnail_path: Path | None = None
    subtitle_paths: list[Path] = field(default_factory=list)
    file_size: int = 0
