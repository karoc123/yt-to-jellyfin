"""Tests for Jellyfin filesystem layout and Kodi/Jellyfin XML NFO generator."""

from pathlib import Path

import defusedxml.ElementTree as DefusedET
import pytest

from app.jellyfin import (
    DEFAULT_LIBRARY_NAME,
    JellyfinLayout,
    NfoGenerator,
    generate_episodedetails_nfo,
    generate_tvshow_nfo,
    sanitize_filename,
)


@pytest.fixture
def layout(tmp_path: Path) -> JellyfinLayout:
    return JellyfinLayout(media_root=tmp_path)


def test_sanitize_filename() -> None:
    """Test character sanitization for path safety."""
    assert sanitize_filename("Normal Title") == "Normal Title"
    assert sanitize_filename('Dangerous: / * ? " < > | Name') == "Dangerous Name"
    assert sanitize_filename("   Trailing spaces and dots...   ") == "Trailing spaces and dots"
    assert sanitize_filename("") == "Unknown"


def test_jellyfin_layout_paths(layout: JellyfinLayout, tmp_path: Path) -> None:
    """Test generating library, channel, season, and video file paths."""
    # Default library
    assert layout.get_library_dir() == tmp_path / "default"
    assert layout.get_channel_dir("Kurzgesagt") == tmp_path / "default" / "Kurzgesagt"
    assert (
        layout.get_season_dir("Kurzgesagt", 2024)
        == tmp_path / "default" / "Kurzgesagt" / "Season 2024"
    )

    # Custom library (e.g. kids, dokus)
    kids_channel = layout.get_channel_dir("Sesamstrasse", library="kids")
    assert kids_channel == tmp_path / "kids" / "Sesamstrasse"

    kids_season = layout.get_season_dir("Sesamstrasse", 2023, library="kids")
    assert kids_season == tmp_path / "kids" / "Sesamstrasse" / "Season 2023"

    # Video filename and path
    video_filename = layout.get_video_filename(
        date_str="2024-05-12",
        title="What if the Moon hit Earth?",
        video_id="abc123xyz",
        extension="mp4",
    )
    assert video_filename == "2024-05-12 - What if the Moon hit Earth [abc123xyz].mp4"

    video_path = layout.get_video_path(
        channel_name="Kurzgesagt",
        year=2024,
        date_str="20240512",
        title="What if the Moon hit Earth?",
        video_id="abc123xyz",
        library=DEFAULT_LIBRARY_NAME,
        extension="mp4",
    )
    expected_path = (
        tmp_path
        / "default"
        / "Kurzgesagt"
        / "Season 2024"
        / "2024-05-12 - What if the Moon hit Earth [abc123xyz].mp4"
    )
    assert video_path == expected_path

    # Companion paths
    assert layout.get_episode_nfo_path(video_path) == video_path.with_suffix(".nfo")
    assert (
        layout.get_episode_thumb_path(video_path)
        == video_path.parent / f"{video_path.stem}-thumb.jpg"
    )
    assert (
        layout.get_channel_nfo_path("Kurzgesagt")
        == tmp_path / "default" / "Kurzgesagt" / "tvshow.nfo"
    )
    assert (
        layout.get_channel_poster_path("Kurzgesagt")
        == tmp_path / "default" / "Kurzgesagt" / "poster.jpg"
    )


def test_jellyfin_ensure_structure(layout: JellyfinLayout, tmp_path: Path) -> None:
    """Test directory creation helper methods."""
    ch_dir = layout.ensure_channel_structure("MyChannel", library="tech")
    assert ch_dir.is_dir()
    assert ch_dir == tmp_path / "tech" / "MyChannel"

    season_dir = layout.ensure_season_structure("MyChannel", 2026, library="tech")
    assert season_dir.is_dir()
    assert season_dir == tmp_path / "tech" / "MyChannel" / "Season 2026"


def test_generate_tvshow_nfo() -> None:
    """Test generating valid tvshow.nfo XML."""
    xml_str = generate_tvshow_nfo(
        title="Veritasium",
        plot="An element of truth - videos about science, education, and anything interesting.",
        channel_id="UCHnyfMqiRRG1u-2MsSQLbXA",
        channel_url="https://www.youtube.com/@veritasium",
        poster_url="https://yt3.googleusercontent.com/poster.jpg",
        premiered="2010-07-21",
    )

    root = DefusedET.fromstring(xml_str)
    assert root.tag == "tvshow"
    assert root.find("title").text == "Veritasium"
    assert "science" in root.find("plot").text
    assert root.find("premiered").text == "2010-07-21"
    assert root.find("year").text == "2010"
    assert root.find("studio").text == "YouTube"
    assert root.find("lockdata").text == "true"

    uniqueid = root.find("uniqueid")
    assert uniqueid.text == "UCHnyfMqiRRG1u-2MsSQLbXA"
    assert uniqueid.attrib.get("type") == "youtube"
    assert uniqueid.attrib.get("default") == "true"

    thumb = root.find("thumb")
    assert thumb.text == "https://yt3.googleusercontent.com/poster.jpg"
    assert thumb.attrib.get("aspect") == "poster"


def test_generate_episodedetails_nfo() -> None:
    """Test generating valid episodedetails XML."""
    xml_str = generate_episodedetails_nfo(
        title="Why the Speed of Light Cannot Be Measured",
        plot="The one-way speed of light has never been measured.",
        aired="2020-10-31",
        season=2020,
        episode=1,
        duration_seconds=1054,
        video_id="pTn6Ewhb27k",
        channel_name="Veritasium",
        webpage_url="https://www.youtube.com/watch?v=pTn6Ewhb27k",
        thumbnail_url="https://i.ytimg.com/vi/pTn6Ewhb27k/maxresdefault.jpg",
        tags=["physics", "relativity", "light"],
    )

    root = DefusedET.fromstring(xml_str)
    assert root.tag == "episodedetails"
    assert root.find("title").text == "Why the Speed of Light Cannot Be Measured"
    assert root.find("showtitle").text == "Veritasium"
    assert root.find("aired").text == "2020-10-31"
    assert root.find("season").text == "2020"
    assert root.find("episode").text == "1"
    assert root.find("runtime").text == "18"  # 1054 / 60 rounded
    assert root.find("lockdata").text == "true"

    uniqueid = root.find("uniqueid")
    assert uniqueid.text == "pTn6Ewhb27k"
    assert uniqueid.attrib.get("type") == "youtube"
    assert uniqueid.attrib.get("default") == "true"

    tags = [elem.text for elem in root.findall("tag")]
    assert "physics" in tags
    assert "relativity" in tags
    assert "light" in tags


def test_nfo_generator_write_files(tmp_path: Path) -> None:
    """Test writing NFO files to disk via NfoGenerator."""
    tvshow_file = tmp_path / "tvshow.nfo"
    written_show = NfoGenerator.write_tvshow_nfo(
        dest_path=tvshow_file,
        title="Technology Connections",
        plot="A show about things you might take for granted.",
        channel_id="UCy0tKL1T7wFoYcxCe0xjN6Q",
    )
    assert written_show.is_file()
    assert written_show.read_text(encoding="utf-8").startswith("<?xml")

    # Episode NFO with automatic season derivation from aired year
    ep_file = tmp_path / "ep.nfo"
    written_ep = NfoGenerator.write_episode_nfo(
        dest_path=ep_file,
        title="Heat Pumps: The Future of Heating",
        aired="2022-01-15",
        duration_seconds=1800,
        video_id="xyz999",
        channel_name="Technology Connections",
    )
    assert written_ep.is_file()
    ep_root = DefusedET.fromstring(written_ep.read_text(encoding="utf-8"))
    assert ep_root.find("season").text == "2022"
    assert ep_root.find("runtime").text == "30"


def test_assign_episode_is_stable(tmp_path: Path) -> None:
    """Episode numbers: MMDD + daily counter, never reused or renumbered."""
    from app.db import Database
    from app.jellyfin.episodes import assign_episode, episode_number

    assert episode_number("2024-03-15", 1) == 31501
    assert episode_number("20241231T10:00:00Z", 2) == 123102

    database = Database(db_path=tmp_path / "episodes.db")
    database.run_migrations()
    with database.connection() as conn:
        conn.execute(
            "INSERT INTO channels (id, name, url, folder_name) VALUES ('UCep', 'Ep', 'u', 'Ep')"
        )
        for vid in ("vidEp1", "vidEp2"):
            ep = assign_episode(conn, vid, "UCep", "2024-03-15")
            conn.execute(
                "INSERT INTO videos (id, channel_id, title, published_at, episode)"
                " VALUES (?, 'UCep', 't', '2024-03-15', ?)",
                (vid, ep),
            )
        assert assign_episode(conn, "vidEp1", "UCep", "2024-03-15") == 31501
        assert assign_episode(conn, "vidEp2", "UCep", "2024-03-15") == 31502
        # Tombstoned sibling keeps its number; a new video does not reuse it
        conn.execute("UPDATE videos SET status = 'deleted' WHERE id = 'vidEp1'")
        assert assign_episode(conn, "vidEp3", "UCep", "2024-03-15") == 31503
        assert assign_episode(conn, "vidEp3", "UCep", "2024-03-16") == 31601
