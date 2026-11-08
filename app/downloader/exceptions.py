"""Custom exceptions for the yt-dlp execution service."""


class YtDlpError(Exception):
    """Base exception for all yt-dlp operations."""


class VideoUnavailableError(YtDlpError):
    """Raised when a video is private, removed, region-restricted, or does not exist."""


class DownloadFailedError(YtDlpError):
    """Raised when downloading video, audio, thumbnail, or subtitles fails."""


class CookieError(YtDlpError):
    """Raised when cookies.txt file is invalid, corrupted, or missing."""
