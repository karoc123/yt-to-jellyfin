"""Jellyfin integration package for layout and NFO metadata generation."""

from app.jellyfin.layout import (
    DEFAULT_LIBRARY_NAME,
    DEFAULT_MEDIA_PATH,
    JellyfinLayout,
    sanitize_filename,
)
from app.jellyfin.nfo import (
    NfoGenerator,
    generate_episodedetails_nfo,
    generate_tvshow_nfo,
)

jellyfin_layout = JellyfinLayout()
nfo_generator = NfoGenerator()

__all__ = [
    "DEFAULT_LIBRARY_NAME",
    "DEFAULT_MEDIA_PATH",
    "JellyfinLayout",
    "NfoGenerator",
    "generate_episodedetails_nfo",
    "generate_tvshow_nfo",
    "jellyfin_layout",
    "nfo_generator",
    "sanitize_filename",
]
