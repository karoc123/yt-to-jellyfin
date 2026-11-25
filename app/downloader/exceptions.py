"""Custom exceptions for the yt-dlp execution service."""


class YtDlpError(Exception):
    """Base exception for all yt-dlp operations."""


class VideoUnavailableError(YtDlpError):
    """Raised when a video is private, removed, region-restricted, or does not exist."""


class VideoUpcomingError(YtDlpError):
    """Raised when YouTube has announced a video that has yet to start."""

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("Waiting for release")
        self.retry_after_seconds = retry_after_seconds


class DownloadFailedError(YtDlpError):
    """Raised when downloading video, audio, thumbnail, or subtitles fails."""


class CookieError(YtDlpError):
    """Raised when cookies.txt file is invalid, corrupted, or missing."""
