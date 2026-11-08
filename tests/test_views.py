"""Integration tests for web views and frontend endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import Database, get_db
from app.main import app


@pytest.fixture
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Fixture providing TestClient backed by temporary database."""
    test_db = Database(tmp_path / "web_test.db")
    test_db.run_migrations()

    monkeypatch.setattr("app.db.database.db", test_db)
    monkeypatch.setattr("app.db.db", test_db)
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.retention.retention_engine.db", test_db)
    monkeypatch.setattr("app.worker.queue.db", test_db)
    monkeypatch.setattr("app.api.endpoints.actions.db", test_db)

    def override_get_db():
        with test_db.connection() as conn:
            yield conn

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


def test_view_home_rendering(test_client: TestClient) -> None:
    """Test that Quick Download home page renders correctly."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert "Schnell-Download" in response.text
    assert "lib-switcher" in response.text
    assert "lib-pill" in response.text


def test_view_browser_rendering_and_library_filter(test_client: TestClient, tmp_path: Path) -> None:
    """Test that Mediathek browser renders channels and filters by library."""
    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("UCkids_1", "Kinderkanal", "https://youtube.com/@kids", "Kinderkanal", "kids"),
        )
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("UCtech_1", "Tech Channel", "https://youtube.com/@tech", "Tech Channel", "tech"),
        )

    # View all channels
    res_all = test_client.get("/browser")
    assert res_all.status_code == 200
    assert "Kinderkanal" in res_all.text
    assert "Tech Channel" in res_all.text

    # View only kids library
    res_kids = test_client.get("/browser?lib=kids")
    assert res_kids.status_code == 200
    assert "Kinderkanal" in res_kids.text
    assert "Tech Channel" not in res_kids.text


def test_view_channel_video_drilldown(test_client: TestClient, tmp_path: Path) -> None:
    """Test drilldown into a specific channel video grid."""
    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library)
            VALUES ('UCdrill', 'Drill Channel', 'https://youtube.com', 'Drill Channel', 'default')
            """
        )
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, duration)
            VALUES ('v_drill_1', 'UCdrill', 'Drilled Episode 1', '2026-11-08T12:00:00Z', 600)
            """
        )

    response = test_client.get("/browser?channel_id=UCdrill")
    assert response.status_code == 200
    assert "Drill Channel" in response.text
    assert "Drilled Episode 1" in response.text
    assert "bulk-bar" in response.text


def test_quick_download_enqueue(test_client: TestClient, tmp_path: Path) -> None:
    """Test submitting Quick Download form enqueues job with library context."""
    form_data = {
        "url": "https://www.youtube.com/watch?v=new_vid_123",
        "target_library": "kids",
        "max_height": "1080",
        "sponsorblock": "1",
    }
    response = test_client.post("/api/downloads", data=form_data, follow_redirects=False)
    assert response.status_code == 303
    assert "?lib=kids" in response.headers["location"]

    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE type = 'download_video'")
        job = cursor.fetchone()
        assert job is not None
        assert "new_vid_123" in job["payload"]
        assert '"library": "kids"' in job["payload"]
        assert '"max_height": 1080' in job["payload"]


def test_bulk_pin_and_delete_endpoints(test_client: TestClient, tmp_path: Path) -> None:
    """Test bulk pin and bulk delete JSON endpoints."""
    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name)
            VALUES ('UCbulk', 'Bulk Channel', 'https://youtube.com', 'Bulk Channel')
            """
        )
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, is_pinned)
            VALUES ('vb1', 'UCbulk', 'Bulk 1', '2026-11-08', 0),
                   ('vb2', 'UCbulk', 'Bulk 2', '2026-11-08', 0)
            """
        )

    # Bulk pin
    pin_res = test_client.post(
        "/api/videos/bulk-pin",
        json={"video_ids": ["vb1", "vb2"], "is_pinned": True},
    )
    assert pin_res.status_code == 200
    assert pin_res.json() == {"updated": 2}

    # Verify pinned in DB
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM videos WHERE is_pinned = 1")
        assert cursor.fetchone()[0] == 2

    # Bulk delete
    del_res = test_client.post(
        "/api/videos/bulk-delete",
        json={"video_ids": ["vb1", "vb2"]},
    )
    assert del_res.status_code == 200
    assert del_res.json() == {"deleted": 2}

    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM videos WHERE status = 'completed'")
        assert cursor.fetchone()[0] == 0


def test_subscriptions_flow(test_client: TestClient, tmp_path: Path) -> None:
    """Test adding, viewing, and deleting a channel subscription."""
    # Add subscription
    sub_data = {
        "channel_input": "UCnewsub",
        "name": "New Sub Channel",
        "library": "dokus",
        "retention_count": "5",
        "retention_days": "14",
    }
    res_add = test_client.post("/api/subscriptions", data=sub_data, follow_redirects=False)
    assert res_add.status_code == 303

    # View subscriptions
    res_view = test_client.get("/subscriptions")
    assert res_view.status_code == 200
    assert "New Sub Channel" in res_view.text
    assert "dokus" in res_view.text
    assert "5 Videos" in res_view.text
    assert "14 Tage" in res_view.text

    # Delete subscription
    res_del = test_client.post(
        "/api/subscriptions/delete",
        data={"channel_id": "UCnewsub"},
        follow_redirects=False,
    )
    assert res_del.status_code == 303

    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM channels WHERE id = 'UCnewsub'")
        assert cursor.fetchone()[0] == 0


def test_subscriptions_import_endpoint(test_client: TestClient, tmp_path: Path) -> None:
    """Test importing subscriptions via file upload."""
    opml_content = b"""<?xml version="1.0" encoding="UTF-8"?>
    <opml version="1.0">
      <body>
        <outline text="Uploaded Channel" title="Uploaded Channel"
                 xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id=UCimported_upload"
                 htmlUrl="https://www.youtube.com/channel/UCimported_upload"/>
      </body>
    </opml>"""

    files = {"subscriptions_file": ("subs.opml", opml_content, "application/xml")}
    res = test_client.post(
        "/api/subscriptions/import",
        files=files,
        data={"library": "kids"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "?lib=kids" in res.headers["location"]

    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE id = 'UCimported_upload'")
        row = cursor.fetchone()
        assert row is not None
        assert row["name"] == "Uploaded Channel"
        assert row["library"] == "kids"
        assert row["auto_download"] == 1


def test_browser_shows_channel_poster(
    test_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Channel grid uses the local poster.jpg via /api/media/poster when present."""
    from app.core import config

    media = tmp_path / "media"
    monkeypatch.setattr(config.settings, "media_dir", media)
    poster = media / "default" / "Poster Kanal" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"\xff\xd8synthetic\xff\xd9")
    with Database(tmp_path / "web_test.db").connection() as conn:
        conn.execute(
            "INSERT INTO channels (id, name, url, folder_name, library)"
            " VALUES ('UCposter1', 'Poster Kanal', 'u', 'Poster Kanal', 'default')"
        )

    page = test_client.get("/browser")
    assert "/api/media/poster?channel_id=UCposter1" in page.text
    res = test_client.get("/api/media/poster?channel_id=UCposter1")
    assert res.status_code == 200
    assert res.content == poster.read_bytes()
    assert test_client.get("/api/media/poster?channel_id=UCmissing").status_code == 404
