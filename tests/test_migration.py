"""Tests for the TubeArchivist migration utility."""

import json
from pathlib import Path

import pytest

from app.db import Database
from app.migration.tubearchivist import TubeArchivistMigrator


@pytest.fixture
def test_db(tmp_path: Path) -> Database:
    """Fixture providing initialized SQLite database."""
    db_instance = Database(tmp_path / "test_migration.db")
    db_instance.run_migrations()
    return db_instance


@pytest.fixture
def tubearchivist_source(tmp_path: Path) -> Path:
    """Fixture creating mock TubeArchivist directory layout with sample files."""
    source_root = tmp_path / "tubearchivist" / "youtube"
    channel_dir = source_root / "UC12345channel"
    channel_dir.mkdir(parents=True, exist_ok=True)

    # Video 1: with full info.json and jpg thumbnail
    v1_file = channel_dir / "vid12345678.mp4"
    v1_file.write_bytes(b"dummy video data 1")

    v1_info = {
        "id": "vid12345678",
        "title": "Introduction to Astronomy",
        "channel": "Science Today",
        "channel_id": "UC12345channel",
        "upload_date": "20230514",
        "duration": 720,
        "description": "An introductory overview of modern astronomy.",
        "tags": ["astronomy", "science"],
    }
    (channel_dir / "vid12345678.info.json").write_text(json.dumps(v1_info), encoding="utf-8")
    (channel_dir / "vid12345678.jpg").write_bytes(b"dummy thumbnail 1")

    # Video 2: fallback without info.json
    v2_file = channel_dir / "vid87654321.mkv"
    v2_file.write_bytes(b"dummy video data 2")

    return source_root


def test_tubearchivist_scan(tubearchivist_source: Path, tmp_path: Path, test_db: Database) -> None:
    """Test discovering and extracting metadata from TubeArchivist source tree."""
    dest_dir = tmp_path / "jellyfin_media"
    migrator = TubeArchivistMigrator(
        source_dir=tubearchivist_source,
        dest_media_root=dest_dir,
        database=test_db,
    )

    candidates = migrator.scan_source()
    assert len(candidates) == 2

    # Verify candidate 1
    c1 = next(c for c in candidates if c.video_id == "vid12345678")
    assert c1.title == "Introduction to Astronomy"
    assert c1.channel_name == "Science Today"
    assert c1.published_at == "2023-05-14"
    assert c1.duration == 720
    assert c1.thumbnail_path is not None

    # Verify candidate 2 (fallback metadata)
    c2 = next(c for c in candidates if c.video_id == "vid87654321")
    assert c2.video_id == "vid87654321"
    assert c2.channel_id == "UC12345channel"


def test_tubearchivist_migration_copy_mode(
    tubearchivist_source: Path, tmp_path: Path, test_db: Database
) -> None:
    """Test full migration run copying files into Jellyfin structure and populating DB."""
    dest_dir = tmp_path / "jellyfin_media"
    migrator = TubeArchivistMigrator(
        source_dir=tubearchivist_source,
        dest_media_root=dest_dir,
        library="dokus",
        mode="copy",
        dry_run=False,
        database=test_db,
    )

    result = migrator.migrate()
    assert result.scanned_count == 2
    assert result.migrated_count == 2
    assert result.failed_count == 0
    assert result.channels_created >= 1

    # Verify destination files
    channel_folder = dest_dir / "dokus" / "Science Today"
    assert channel_folder.is_dir()
    assert (channel_folder / "tvshow.nfo").is_file()

    season_2023 = channel_folder / "Season 2023"
    assert season_2023.is_dir()

    expected_video = season_2023 / "2023-05-14 - Introduction to Astronomy [vid12345678].mp4"
    assert expected_video.is_file()
    assert expected_video.read_bytes() == b"dummy video data 1"

    expected_nfo = season_2023 / "2023-05-14 - Introduction to Astronomy [vid12345678].nfo"
    assert expected_nfo.is_file()
    nfo_content = expected_nfo.read_text(encoding="utf-8")
    assert "<title>Introduction to Astronomy</title>" in nfo_content
    assert "<aired>2023-05-14</aired>" in nfo_content
    assert "<season>2023</season>" in nfo_content
    assert '<uniqueid type="youtube" default="true">vid12345678</uniqueid>' in nfo_content

    expected_thumb = season_2023 / "2023-05-14 - Introduction to Astronomy [vid12345678]-thumb.jpg"
    assert expected_thumb.is_file()

    # Verify SQLite database population
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE id = 'UC12345channel'")
        ch_row = cursor.fetchone()
        assert ch_row is not None
        assert ch_row["name"] == "Science Today"
        assert ch_row["library"] == "dokus"

        cursor.execute("SELECT * FROM videos WHERE id = 'vid12345678'")
        v_row = cursor.fetchone()
        assert v_row is not None
        assert v_row["title"] == "Introduction to Astronomy"
        assert v_row["channel_id"] == "UC12345channel"
        assert v_row["duration"] == 720
        assert v_row["file_path"] == str(expected_video)
        assert v_row["nfo_path"] == str(expected_nfo)
        assert v_row["thumbnail_path"] == str(expected_thumb)


def test_tubearchivist_migration_dry_run(
    tubearchivist_source: Path, tmp_path: Path, test_db: Database
) -> None:
    """Test that dry-run does not write files or touch the database."""
    dest_dir = tmp_path / "jellyfin_media_dry"
    migrator = TubeArchivistMigrator(
        source_dir=tubearchivist_source,
        dest_media_root=dest_dir,
        mode="copy",
        dry_run=True,
        database=test_db,
    )

    result = migrator.migrate()
    assert result.scanned_count == 2
    assert result.migrated_count == 2
    assert not dest_dir.exists()

    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM videos")
        assert cursor.fetchone()[0] == 0


def test_tubearchivist_migration_move_mode(
    tubearchivist_source: Path, tmp_path: Path, test_db: Database
) -> None:
    """Test moving files removes them from the source folder."""
    dest_dir = tmp_path / "jellyfin_media_move"
    migrator = TubeArchivistMigrator(
        source_dir=tubearchivist_source,
        dest_media_root=dest_dir,
        mode="move",
        dry_run=False,
        database=test_db,
    )

    migrator.migrate()

    # Source video file should no longer exist
    v1_src = tubearchivist_source / "UC12345channel" / "vid12345678.mp4"
    assert not v1_src.exists()
