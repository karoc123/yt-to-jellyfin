"""Filesystem layout manager for Jellyfin-compliant TV Show libraries."""

import re
from pathlib import Path

from app.core.config import settings

DEFAULT_LIBRARY_NAME = "default"

# Filesystem name limit (ext4, btrfs, ...) and room for the longest companion/temp suffix
# appended to a video stem (e.g. ".f399.mp4.part", ".zh-Hans.vtt", "-thumb.jpg")
MAX_FILENAME_BYTES = 255
COMPANION_SUFFIX_RESERVE = 32


def truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut `text` to at most `max_bytes` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max(max_bytes, 0)].decode("utf-8", errors="ignore")


def sanitize_filename(name: str) -> str:
    r"""Sanitize string for cross-platform filesystem paths.

    Replaces invalid characters (: / \ * ? " < > |) with dashes or safe equivalents.
    Strips leading/trailing whitespace and trailing dots.
    """
    if not name:
        return "Unknown"
    # Replace invalid chars with hyphen
    cleaned = re.sub(r'[\\/*?:"<>|]', "-", name)
    # Collapse consecutive hyphens and whitespace
    cleaned = re.sub(r"[\s-]+", " ", cleaned)
    cleaned = cleaned.strip(". ")
    return cleaned or "Unknown"


class JellyfinLayout:
    """Manages directory and file paths conforming to Jellyfin's TV Show structure."""

    def __init__(self, media_root: Path | str | None = None) -> None:
        self._media_root = Path(media_root) if media_root is not None else None

    @property
    def media_root(self) -> Path:
        """Explicit root, or `settings.media_dir` read at call time."""
        return self._media_root if self._media_root is not None else settings.media_dir

    @media_root.setter
    def media_root(self, value: Path | str) -> None:
        self._media_root = Path(value)

    def get_library_dir(self, library: str = DEFAULT_LIBRARY_NAME) -> Path:
        """Return the root path for a given media library folder."""
        safe_library = sanitize_filename(library or DEFAULT_LIBRARY_NAME)
        return self.media_root / safe_library

    def get_channel_dir(
        self,
        channel_name: str,
        library: str = DEFAULT_LIBRARY_NAME,
    ) -> Path:
        """Return channel folder path: /media/<library>/<Channel>/."""
        safe_channel = sanitize_filename(channel_name)
        return self.get_library_dir(library) / safe_channel

    def get_season_dir(
        self,
        channel_name: str,
        year: int | str,
        library: str = DEFAULT_LIBRARY_NAME,
    ) -> Path:
        """Return season directory path: /media/<library>/<Channel>/Season <Year>/."""
        season_str = str(year).strip()
        if not season_str or not season_str.isdigit():
            season_folder = "Season Unknown"
        else:
            season_folder = f"Season {season_str}"
        return self.get_channel_dir(channel_name, library) / season_folder

    def get_video_filename(
        self,
        date_str: str,
        title: str,
        video_id: str,
        extension: str = "mp4",
    ) -> str:
        """Format video filename: <Date> - <Title> [<ID>].<ext>.

        Long titles are truncated UTF-8-safely so that the stem plus any companion
        suffix stays within MAX_FILENAME_BYTES.
        """
        safe_date = str(date_str).strip()
        # If date is YYYYMMDD, format as YYYY-MM-DD
        if len(safe_date) == 8 and safe_date.isdigit():
            safe_date = f"{safe_date[:4]}-{safe_date[4:6]}-{safe_date[6:8]}"

        safe_id = sanitize_filename(video_id)
        ext = extension.lstrip(".")
        prefix = f"{safe_date} - " if safe_date else ""
        tail = f" [{safe_id}]"

        # Keep stem + any companion suffix within the filesystem limit; date and ID stay intact
        budget = MAX_FILENAME_BYTES - COMPANION_SUFFIX_RESERVE - len((prefix + tail).encode())
        safe_title = sanitize_filename(title)
        truncated = truncate_utf8(safe_title, budget).rstrip(". ")
        if truncated != safe_title:
            safe_title = truncated or "Unknown"

        return f"{prefix}{safe_title}{tail}.{ext}"

    def get_video_path(
        self,
        channel_name: str,
        year: int | str,
        date_str: str,
        title: str,
        video_id: str,
        library: str = DEFAULT_LIBRARY_NAME,
        extension: str = "mp4",
    ) -> Path:
        """Return full path for video file."""
        season_dir = self.get_season_dir(channel_name, year, library)
        filename = self.get_video_filename(date_str, title, video_id, extension)
        return season_dir / filename

    def get_episode_nfo_path(self, video_file_path: Path | str) -> Path:
        """Return corresponding .nfo path for a video file (same stem + .nfo)."""
        video_path = Path(video_file_path)
        return video_path.with_suffix(".nfo")

    def get_channel_nfo_path(
        self,
        channel_name: str,
        library: str = DEFAULT_LIBRARY_NAME,
    ) -> Path:
        """Return tvshow.nfo path for a channel folder."""
        return self.get_channel_dir(channel_name, library) / "tvshow.nfo"

    def get_channel_poster_path(
        self,
        channel_name: str,
        library: str = DEFAULT_LIBRARY_NAME,
    ) -> Path:
        """Return poster.jpg path for a channel folder."""
        return self.get_channel_dir(channel_name, library) / "poster.jpg"

    def get_episode_thumb_path(self, video_file_path: Path | str) -> Path:
        """Return Jellyfin episode thumbnail path: <video_stem>-thumb.jpg."""
        video_path = Path(video_file_path)
        return video_path.parent / f"{video_path.stem}-thumb.jpg"

    def ensure_channel_structure(
        self,
        channel_name: str,
        library: str = DEFAULT_LIBRARY_NAME,
    ) -> Path:
        """Create library and channel directories if they do not exist."""
        channel_dir = self.get_channel_dir(channel_name, library)
        channel_dir.mkdir(parents=True, exist_ok=True)
        return channel_dir

    def ensure_season_structure(
        self,
        channel_name: str,
        year: int | str,
        library: str = DEFAULT_LIBRARY_NAME,
    ) -> Path:
        """Create library, channel, and season directories if they do not exist."""
        season_dir = self.get_season_dir(channel_name, year, library)
        season_dir.mkdir(parents=True, exist_ok=True)
        return season_dir
