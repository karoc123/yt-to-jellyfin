"""Jellyfin and Kodi compliant XML NFO generator."""

import logging
from datetime import datetime
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


def _prettify_xml(elem: ET.Element) -> str:
    """Return a pretty-printed, indented XML string with utf-8 encoding."""
    rough_string = ET.tostring(elem, encoding="utf-8")
    reparsed = minidom.parseString(rough_string)
    pretty = reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    # Clean up blank lines that minidom sometimes inserts
    lines = [line for line in pretty.splitlines() if line.strip()]
    return "\n".join(lines) + "\n"


def _add_element(parent: ET.Element, tag: str, text: str | int | float | None) -> ET.Element:
    """Helper to append a child element with text content if not None."""
    child = ET.SubElement(parent, tag)
    if text is not None:
        child.text = str(text)
    return child


def generate_tvshow_nfo(
    title: str,
    plot: str = "",
    channel_id: str = "",
    channel_url: str = "",
    poster_url: str = "",
    studio: str = "YouTube",
    premiered: str = "",
    genre: str = "YouTube",
) -> str:
    """Generate Kodi/Jellyfin compliant tvshow.nfo XML."""
    root = ET.Element("tvshow")

    _add_element(root, "title", title)
    _add_element(root, "originaltitle", title)
    _add_element(root, "plot", plot)
    _add_element(root, "outline", plot[:300] if plot else "")
    _add_element(root, "studio", studio)
    _add_element(root, "genre", genre)

    if premiered:
        _add_element(root, "premiered", premiered)
        try:
            year = premiered.split("-")[0]
            _add_element(root, "year", year)
        except Exception:
            pass

    if channel_id:
        unique_id = _add_element(root, "uniqueid", channel_id)
        unique_id.set("type", "youtube")
        unique_id.set("default", "true")

    if channel_url:
        _add_element(root, "website", channel_url)

    if poster_url:
        thumb = _add_element(root, "thumb", poster_url)
        thumb.set("aspect", "poster")

    # Lock metadata so Jellyfin won't overwrite with false online TVDB matches
    _add_element(root, "lockdata", "true")

    return _prettify_xml(root)


def generate_episodedetails_nfo(
    title: str,
    plot: str = "",
    aired: str = "",
    season: int = 1,
    episode: int = 1,
    duration_seconds: int = 0,
    video_id: str = "",
    channel_name: str = "",
    webpage_url: str = "",
    thumbnail_url: str = "",
    tags: list[str] | None = None,
) -> str:
    """Generate Kodi/Jellyfin compliant episodedetails XML."""
    root = ET.Element("episodedetails")

    _add_element(root, "title", title)
    _add_element(root, "showtitle", channel_name or "YouTube")
    _add_element(root, "season", season)
    _add_element(root, "episode", episode)
    _add_element(root, "plot", plot)
    _add_element(root, "outline", plot[:300] if plot else "")

    if aired:
        _add_element(root, "aired", aired)
        try:
            year = aired.split("-")[0]
            _add_element(root, "year", year)
        except Exception:
            pass

    if duration_seconds > 0:
        minutes = max(1, round(duration_seconds / 60))
        _add_element(root, "runtime", minutes)

    if video_id:
        unique_id = _add_element(root, "uniqueid", video_id)
        unique_id.set("type", "youtube")
        unique_id.set("default", "true")

    if webpage_url:
        _add_element(root, "website", webpage_url)

    if thumbnail_url:
        _add_element(root, "thumb", thumbnail_url)

    if tags:
        for tag in tags:
            if tag and tag.strip():
                _add_element(root, "tag", tag.strip())

    # Lock metadata to prevent Jellyfin scraper overwrites
    _add_element(root, "lockdata", "true")

    return _prettify_xml(root)


class NfoGenerator:
    """Service to create and save tvshow.nfo and episode .nfo files."""

    @staticmethod
    def write_tvshow_nfo(
        dest_path: Path | str,
        title: str,
        plot: str = "",
        channel_id: str = "",
        channel_url: str = "",
        poster_url: str = "",
        studio: str = "YouTube",
        premiered: str = "",
        genre: str = "YouTube",
    ) -> Path:
        """Write tvshow.nfo to disk."""
        target = Path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        xml_content = generate_tvshow_nfo(
            title=title,
            plot=plot,
            channel_id=channel_id,
            channel_url=channel_url,
            poster_url=poster_url,
            studio=studio,
            premiered=premiered,
            genre=genre,
        )
        target.write_text(xml_content, encoding="utf-8")
        return target

    @staticmethod
    def write_episode_nfo(
        dest_path: Path | str,
        title: str,
        plot: str = "",
        aired: str = "",
        season: int | None = None,
        episode: int = 1,
        duration_seconds: int = 0,
        video_id: str = "",
        channel_name: str = "",
        webpage_url: str = "",
        thumbnail_url: str = "",
        tags: list[str] | None = None,
    ) -> Path:
        """Write episodedetails .nfo to disk.

        If season is not provided, extracts season number from year of aired date
        or defaults to current year.
        """
        target = Path(dest_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Derive season from aired year if not given
        resolved_season = season
        if resolved_season is None:
            if aired and len(aired) >= 4 and aired[:4].isdigit():
                resolved_season = int(aired[:4])
            else:
                resolved_season = datetime.now().year

        xml_content = generate_episodedetails_nfo(
            title=title,
            plot=plot,
            aired=aired,
            season=resolved_season,
            episode=episode,
            duration_seconds=duration_seconds,
            video_id=video_id,
            channel_name=channel_name,
            webpage_url=webpage_url,
            thumbnail_url=thumbnail_url,
            tags=tags,
        )
        target.write_text(xml_content, encoding="utf-8")
        return target
