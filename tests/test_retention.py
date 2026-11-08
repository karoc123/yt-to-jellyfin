"""Tests for retention rules (TTL, max count), video pinning, and safe deletion."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db.database import Database
from app.retention import (
    RetentionConfig,
    RetentionEngine,
    delete_video_artifacts,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_file = tmp_path / "retention_test.db"
    db = Database(db_path=db_file)
    db.run_migrations()
    return db


@pytest.fixture
def engine(temp_db: Database) -> RetentionEngine:
    return RetentionEngine(database=temp_db)


def test_delete_video_artifacts(tmp_path: Path) -> None:
    """Test atomic filesystem cleanup of video, NFO, thumbnail, and subtitles (#10)."""
    season_dir = tmp_path / "Channel" / "Season 2026"
    season_dir.mkdir(parents=True)

    video_file = season_dir / "2026-11-08 - My Video [vid123].mp4"
    video_file.write_bytes(b"dummy video")

    nfo_file = season_dir / "2026-11-08 - My Video [vid123].nfo"
    nfo_file.write_text("<episodedetails/>")

    thumb_file = season_dir / "2026-11-08 - My Video [vid123]-thumb.jpg"
    thumb_file.write_bytes(b"dummy thumb")

    sub_en = season_dir / "2026-11-08 - My Video [vid123].en.vtt"
    sub_en.write_text("WEBVTT")

    sub_de = season_dir / "2026-11-08 - My Video [vid123].de.vtt"
    sub_de.write_text("WEBVTT")

    deleted = delete_video_artifacts(
        video_path=video_file,
        nfo_path=nfo_file,
        thumbnail_path=thumb_file,
    )

    assert len(deleted) >= 4

    assert video_file not in season_dir.iterdir() if season_dir.exists() else True
    assert not video_file.exists()
    assert not nfo_file.exists()
    assert not thumb_file.exists()
    assert not sub_en.exists()
    assert not sub_de.exists()

    # Empty parent season directory should have been cleaned up
    assert not season_dir.exists()


def test_video_pinning(engine: RetentionEngine, temp_db: Database) -> None:
    """Test pinning and unpinning videos to protect them from deletion (#10)."""
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name)
            VALUES (?, ?, ?, ?)
            """,
            ("UCpin", "Pinned Channel", "https://youtube.com/@pin", "Pinned Channel"),
        )
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, is_pinned)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("vid_pinned", "UCpin", "Important Video", "2026-11-01T12:00:00Z", 0),
        )

    # Pin video
    assert engine.set_pin_status("vid_pinned", True) is True
    with temp_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_pinned FROM videos WHERE id = ?", ("vid_pinned",))
        assert cursor.fetchone()["is_pinned"] == 1

    # Attempt to delete pinned video without force
    result = engine.delete_video("vid_pinned", force=False)
    assert result.success is False
    assert "pinned" in result.reason.lower()

    # Unpin video
    assert engine.set_pin_status("vid_pinned", False) is True
    result_unpinned = engine.delete_video("vid_pinned", force=False)
    assert result_unpinned.success is True

    # Record should be gone from DB
    with temp_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM videos WHERE id = ?", ("vid_pinned",))
        assert cursor.fetchone() is None


def test_retention_ttl_rules(engine: RetentionEngine, temp_db: Database, tmp_path: Path) -> None:
    """Test TTL expiration (videos older than X days) (#9)."""
    now = datetime.now(UTC)
    old_date = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_date = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    v_old_path = tmp_path / "old.mp4"
    v_old_path.write_bytes(b"old")
    v_recent_path = tmp_path / "recent.mp4"
    v_recent_path.write_bytes(b"recent")
    v_pinned_path = tmp_path / "pinned.mp4"
    v_pinned_path.write_bytes(b"pinned")

    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, retention_days)
            VALUES (?, ?, ?, ?, 30)
            """,
            ("UCttl", "TTL Channel", "https://youtube.com/@ttl", "TTL Channel"),
        )
        # Old video (>30 days) -> should be deleted
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, file_path, is_pinned)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("vid_old", "UCttl", "Old Video", old_date, str(v_old_path)),
        )
        # Recent video (<30 days) -> should be kept
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, file_path, is_pinned)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("vid_recent", "UCttl", "Recent Video", recent_date, str(v_recent_path)),
        )
        # Old video BUT pinned -> should be kept
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, file_path, is_pinned)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("vid_pinned_old", "UCttl", "Pinned Old Video", old_date, str(v_pinned_path)),
        )

    # Evaluate retention
    results = engine.evaluate_channel_retention("UCttl")
    deleted_ids = [r.video_id for r in results]

    assert deleted_ids == ["vid_old"]
    assert not v_old_path.exists()
    assert v_recent_path.exists()
    assert v_pinned_path.exists()


def test_retention_count_rules(engine: RetentionEngine, temp_db: Database) -> None:
    """Test max episode count limit (keep latest N videos per channel) (#9)."""
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, retention_count)
            VALUES (?, ?, ?, ?, 2)
            """,
            ("UCcount", "Count Channel", "https://youtube.com/@count", "Count Channel"),
        )
        # Insert 4 videos with descending publish dates
        for i in range(1, 5):
            conn.execute(
                """
                INSERT INTO videos (id, channel_id, title, published_at, is_pinned)
                VALUES (?, ?, ?, ?, 0)
                """,
                (f"v{i}", "UCcount", f"Video {i}", f"2026-11-0{i}T12:00:00Z"),
            )

    # Retention max_count is 2, so oldest 2 (v1, v2) should be removed
    results = engine.evaluate_channel_retention("UCcount")
    deleted_ids = {r.video_id for r in results}

    assert deleted_ids == {"v1", "v2"}

    with temp_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM videos WHERE channel_id = 'UCcount' ORDER BY id")
        remaining = [r["id"] for r in cursor.fetchall()]
        assert remaining == ["v3", "v4"]


def test_global_retention_defaults(engine: RetentionEngine, temp_db: Database) -> None:
    """Test fallback to global retention config when channel has no overrides."""
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name)
            VALUES (?, ?, ?, ?)
            """,
            ("UCglobal", "Global Channel", "https://youtube.com/@g", "Global Channel"),
        )
        for i in range(1, 4):
            conn.execute(
                """
                INSERT INTO videos (id, channel_id, title, published_at, is_pinned)
                VALUES (?, ?, ?, ?, 0)
                """,
                (f"g{i}", "UCglobal", f"Global {i}", f"2026-11-0{i}T12:00:00Z"),
            )

    # Channel has NULL retention_count, evaluate with global max_count=1
    cfg = RetentionConfig(max_count=1)
    results = engine.evaluate_channel_retention("UCglobal", global_config=cfg)
    deleted_ids = {r.video_id for r in results}

    assert deleted_ids == {"g1", "g2"}
