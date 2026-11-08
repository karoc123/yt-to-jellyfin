"""Tests for SQLite database manager and migrations."""

import sqlite3
from pathlib import Path

import pytest

from app.db.database import Database


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    """Fixture providing a Database instance backed by a temporary file."""
    db_file = tmp_path / "test.db"
    return Database(db_path=db_file)


def test_database_connection_pragmas(temp_db: Database) -> None:
    """Test that connections enforce foreign keys and WAL mode."""
    with temp_db.connection() as conn:
        cursor = conn.cursor()

        # Check foreign_keys
        cursor.execute("PRAGMA foreign_keys;")
        assert cursor.fetchone()[0] == 1

        # Check journal_mode (WAL)
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0].lower()
        assert journal_mode == "wal"


def test_migrations_application_and_idempotency(temp_db: Database) -> None:
    """Test that migrations are applied correctly and only once."""
    assert temp_db.get_user_version() == 0

    applied = temp_db.run_migrations()
    assert applied == [1, 2, 3, 4, 5]
    assert temp_db.get_user_version() == 5

    # Second run should apply nothing
    applied_again = temp_db.run_migrations()
    assert applied_again == []
    assert temp_db.get_user_version() == 5


def test_schema_tables_and_constraints(temp_db: Database) -> None:
    """Test table schema, foreign key enforcement, and cascade delete."""
    temp_db.run_migrations()

    with temp_db.connection() as conn:
        # Insert channel with default library
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, retention_days, retention_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("UC12345", "Test Channel", "https://youtube.com/@test", "Test Channel", 30, 10),
        )

        cursor = conn.cursor()
        cursor.execute("SELECT library FROM channels WHERE id = ?", ("UC12345",))
        assert cursor.fetchone()["library"] == "default"

        # Insert channel with custom library
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("UCkids", "Kids Channel", "https://youtube.com/@kids", "Kids Channel", "kids"),
        )
        cursor.execute("SELECT library FROM channels WHERE id = ?", ("UCkids",))
        assert cursor.fetchone()["library"] == "kids"

        # Insert video
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, duration, is_pinned)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("vid999", "UC12345", "First Video", "2026-11-08T12:00:00Z", 120, 1),
        )

        cursor = conn.cursor()
        cursor.execute("SELECT * FROM videos WHERE id = ?", ("vid999",))
        video = cursor.fetchone()
        assert video["title"] == "First Video"
        assert video["is_pinned"] == 1

        # Check foreign key failure: insert video with non-existent channel
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO videos (id, channel_id, title, published_at)
                VALUES (?, ?, ?, ?)
                """,
                ("vid_orphan", "non_existent", "Orphan", "2026-11-08"),
            )

        # Check cascade delete
        conn.execute("DELETE FROM channels WHERE id = ?", ("UC12345",))
        cursor.execute("SELECT COUNT(*) FROM videos WHERE channel_id = ?", ("UC12345",))
        assert cursor.fetchone()[0] == 0


def test_jobs_and_settings_tables(temp_db: Database) -> None:
    """Test inserting and updating jobs and settings."""
    temp_db.run_migrations()

    with temp_db.connection() as conn:
        # Settings
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("download_path", "/media/youtube"),
        )
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", ("download_path",))
        assert cursor.fetchone()["value"] == "/media/youtube"

        # Jobs
        conn.execute(
            """
            INSERT INTO jobs (id, type, payload, status, progress)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("job-1", "download_video", '{"url": "https://youtu.be/xxx"}', "pending", 0),
        )
        cursor.execute("SELECT status, progress FROM jobs WHERE id = ?", ("job-1",))
        job = cursor.fetchone()
        assert job["status"] == "pending"
        assert job["progress"] == 0

        # Invalid job status should fail CHECK constraint
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (id, type, status) VALUES (?, ?, ?)",
                ("job-2", "x", "invalid_status"),
            )
