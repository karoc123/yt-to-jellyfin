"""Integration tests for web views and frontend endpoints."""

import re
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
    assert "Quick Download" in response.text
    assert "lib-switcher" in response.text
    assert "lib-pill" in response.text


def test_view_browser_rendering_and_library_filter(test_client: TestClient, tmp_path: Path) -> None:
    """Test that library browser renders channels and filters by library."""
    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("UCkids_1", "Kids Channel", "https://youtube.com/@kids", "Kids Channel", "kids"),
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
    assert "Kids Channel" in res_all.text
    assert "Tech Channel" in res_all.text

    # View only kids library
    res_kids = test_client.get("/browser?lib=kids")
    assert res_kids.status_code == 200
    assert "Kids Channel" in res_kids.text
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
    assert "5 videos" in res_view.text
    assert "14 days" in res_view.text

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


def test_subscription_unsubscribe_preserves_videos(test_client: TestClient, tmp_path: Path) -> None:
    """Unsubscribing from a channel with videos keeps channel and videos (auto_download=0)."""
    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES ('UCkeep', 'Keep', 'https://youtube.com/@keep', 'Keep', 'default', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, status)
            VALUES ('vid_keep_1', 'UCkeep', 'Keep This Video', '2026-11-08', 'completed')
            """
        )

    # Post unsubscribe mode
    res = test_client.post(
        "/api/subscriptions/delete",
        data={"channel_id": "UCkeep", "mode": "unsubscribe"},
        follow_redirects=False,
    )
    assert res.status_code == 303

    # Channel still exists, but auto_download is 0
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT auto_download FROM channels WHERE id = 'UCkeep'")
        row = cursor.fetchone()
        assert row is not None
        assert row["auto_download"] == 0

        # Video is preserved
        cursor.execute("SELECT COUNT(*) FROM videos WHERE id = 'vid_keep_1'")
        assert cursor.fetchone()[0] == 1

    # Channel is no longer shown under Active Subscriptions (/subscriptions)
    res_subs = test_client.get("/subscriptions")
    assert "Keep" not in res_subs.text

    # But channel is still visible in the Library (/browser)
    res_browser = test_client.get("/browser")
    assert "Keep" in res_browser.text


def test_subscription_delete_all_removes_channel_and_videos(
    test_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_all mode removes channel and cascades to videos and optionally removes files."""
    from app.core import config

    media_dir = tmp_path / "media"
    ch_dir = media_dir / "default" / "Purge Channel"
    ch_dir.mkdir(parents=True, exist_ok=True)
    dummy_file = ch_dir / "video.mp4"
    dummy_file.write_bytes(b"data")
    monkeypatch.setattr(config.settings, "media_dir", media_dir)

    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES ('UCpurge', 'Purge', 'https://youtube.com/@purge', 'Purge Channel', 'default', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, status)
            VALUES ('vid_purge_1', 'UCpurge', 'Purge Video', '2026-11-08', 'completed')
            """
        )

    res = test_client.post(
        "/api/subscriptions/delete",
        data={"channel_id": "UCpurge", "mode": "delete_all", "delete_files": "1"},
        follow_redirects=False,
    )
    assert res.status_code == 303

    # Channel and video deleted from DB
    with test_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM channels WHERE id = 'UCpurge'")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT COUNT(*) FROM videos WHERE id = 'vid_purge_1'")
        assert cursor.fetchone()[0] == 0

    # Directory was deleted from disk
    assert not ch_dir.exists()


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


def test_import_subscriptions_form_location(test_client: TestClient) -> None:
    """Import subscriptions block is located on /settings, not on /subscriptions."""
    res_subs = test_client.get("/subscriptions")
    assert res_subs.status_code == 200
    assert 'action="/api/subscriptions/import"' not in res_subs.text

    res_settings = test_client.get("/settings")
    assert res_settings.status_code == 200
    assert 'action="/api/subscriptions/import"' in res_settings.text
    assert "Import subscriptions" in res_settings.text


def test_import_subscriptions_empty_redirects_to_settings(test_client: TestClient) -> None:
    """Submitting empty import form redirects back to settings with error flash."""
    res = test_client.post(
        "/api/subscriptions/import",
        data={"library": "default"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith("/settings")
    assert "msg=import_missing" in res.headers["location"]


def test_browser_shows_channel_poster(
    test_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Channel grid uses the local poster.jpg via /api/media/poster when present."""
    from app.core import config

    media = tmp_path / "media"
    monkeypatch.setattr(config.settings, "media_dir", media)
    poster = media / "default" / "Poster Channel" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"\xff\xd8synthetic\xff\xd9")
    with Database(tmp_path / "web_test.db").connection() as conn:
        conn.execute(
            "INSERT INTO channels (id, name, url, folder_name, library)"
            " VALUES ('UCposter1', 'Poster Channel', 'u', 'Poster Channel', 'default')"
        )

    page = test_client.get("/browser")
    assert "/api/media/poster?channel_id=UCposter1" in page.text
    res = test_client.get("/api/media/poster?channel_id=UCposter1")
    assert res.status_code == 200
    assert res.content == poster.read_bytes()
    assert test_client.get("/api/media/poster?channel_id=UCmissing").status_code == 404


def test_subscribe_unresolvable_channel_is_not_inserted(
    test_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed @handle resolution redirects back without creating a channel row."""

    def _fail(channel_input: str) -> tuple[str, str]:
        raise ValueError("synthetic resolution failure")

    monkeypatch.setattr("app.api.endpoints.actions.resolve_channel", _fail)
    res = test_client.post(
        "/api/subscriptions",
        data={"channel_input": "@doesnotexist", "library": "default"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    with Database(tmp_path / "web_test.db").connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0


def test_queue_rendering_with_title_and_thumbnail(test_client: TestClient, tmp_path: Path) -> None:
    """Queue card displays video title and thumbnail image when available."""
    import json

    test_db = Database(tmp_path / "web_test.db")
    payload = {
        "title": "Super Cool Video Title",
        "thumbnail": "https://i.ytimg.com/vi/cool1234567/hqdefault.jpg",
        "url": "https://www.youtube.com/watch?v=cool1234567",
        "channel_name": "Cool Channel",
        "library": "default",
    }
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, type, payload, status, progress, created_at)
            VALUES (?, 'download_video', ?, 'running', 42, '2026-11-08T20:00:00Z')
            """,
            ("job_test_thumb", json.dumps(payload)),
        )

    response = test_client.get("/partials/queue")
    assert response.status_code == 200
    assert "Super Cool Video Title" in response.text
    assert "https://i.ytimg.com/vi/cool1234567/hqdefault.jpg" in response.text
    assert "Cool Channel" in response.text
    assert "queue-link" in response.text
    assert "https://www.youtube.com/watch?v=cool1234567" in response.text


def test_queue_rendering_without_thumbnail(test_client: TestClient, tmp_path: Path) -> None:
    """Queue card renders cleanly when thumbnail is absent (no broken image tag)."""
    import json

    test_db = Database(tmp_path / "web_test.db")
    payload = {
        "title": "Audio Only Track",
        "url": "https://example.com/audio",
        "library": "default",
    }
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, type, payload, status, progress, created_at)
            VALUES (?, 'download_video', ?, 'pending', 0, '2026-11-08T20:05:00Z')
            """,
            ("job_test_nothumb", json.dumps(payload)),
        )

    response = test_client.get("/partials/queue")
    assert response.status_code == 200
    assert "Audio Only Track" in response.text
    assert '<div class="card-media">' not in response.text


def test_extract_youtube_video_id_patterns() -> None:
    """Test extracting 11-character YouTube video IDs from various URL formats."""
    from app.api.endpoints.actions import extract_youtube_video_id

    assert extract_youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_youtube_video_id("https://www.youtube.com/playlist?list=PL1234567890") is None
    assert extract_youtube_video_id("https://vimeo.com/12345") is None


@pytest.mark.anyio
async def test_fetch_youtube_oembed_parser() -> None:
    """Test oEmbed metadata parsing with mock httpx AsyncClient."""
    import httpx

    from app.api.endpoints.actions import fetch_youtube_oembed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "title": "Mock Video Title",
                "author_name": "Mock Author",
                "thumbnail_url": "https://i.ytimg.com/vi/mock/hqdefault.jpg",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await fetch_youtube_oembed(
            "https://www.youtube.com/watch?v=mock1234567",
            client=client,
        )
        assert res["title"] == "Mock Video Title"
        assert res["channel_name"] == "Mock Author"
        assert res["thumbnail"] == "https://i.ytimg.com/vi/mock/hqdefault.jpg"


def test_browser_subscribe_workflow(test_client: TestClient, tmp_path: Path) -> None:
    """Test subscribing retroactively to a channel from the Library Overview (/browser)."""
    test_db = Database(tmp_path / "web_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES ('UCresub', 'Resub', 'https://youtube.com/@resub', 'Resub', 'default', 0)
            """
        )

    # In browser channel view, channel shows as Not Subscribed and shows Subscribe button
    res = test_client.get("/browser?channel_id=UCresub")
    assert res.status_code == 200
    assert "Not subscribed" in res.text
    assert "subscribe-modal" in res.text

    # Submit subscription from browser modal
    sub_data = {
        "channel_input": "UCresub",
        "name": "Resub Channel",
        "library": "default",
        "initial_download": "0",
        "return_to": "/browser",
        "channel_id_redirect": "UCresub",
    }
    res_sub = test_client.post("/api/subscriptions", data=sub_data, follow_redirects=False)
    assert res_sub.status_code == 303
    assert "/browser" in res_sub.headers["location"]
    assert "channel_id=UCresub" in res_sub.headers["location"]

    # Verify channel is now auto_download = 1 in DB
    with test_db.connection() as conn:
        row = conn.execute("SELECT auto_download FROM channels WHERE id = 'UCresub'").fetchone()
        assert row is not None
        assert row["auto_download"] == 1

    # In browser channel view, channel now shows as Subscribed
    res_after = test_client.get("/browser?channel_id=UCresub")
    assert res_after.status_code == 200
    assert "Subscribed" in res_after.text


def test_ui_has_no_emoji(test_client: TestClient) -> None:
    """Main views use the SVG icon set, not emoji."""
    emoji = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff]")
    for path in ["/", "/browser", "/subscriptions", "/settings"]:
        res = test_client.get(path)
        assert res.status_code == 200
        assert not emoji.findall(res.text), path
