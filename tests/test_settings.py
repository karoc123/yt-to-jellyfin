"""Integration and unit tests for the Settings view and cookies management."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.db import Database, get_db
from app.main import app

VALID_COOKIES_TEXT = (
    "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\taf6s7d8f\n"
)


@pytest.fixture
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Fixture providing TestClient backed by temporary database and cookie path."""
    test_db = Database(tmp_path / "settings_test.db")
    test_db.run_migrations()

    cookies_file = tmp_path / "data" / "cookies.txt"
    monkeypatch.setattr(config.settings, "cookies_path", cookies_file)
    monkeypatch.setattr("app.db.database.db", test_db)
    monkeypatch.setattr("app.db.db", test_db)
    monkeypatch.setattr("app.main.db", test_db)
    monkeypatch.setattr("app.api.endpoints.actions.db", test_db)
    monkeypatch.setattr("app.core.settings_service.settings_service._db", test_db)
    monkeypatch.setattr("app.worker.queue.db", test_db)

    def override_get_db():
        with test_db.connection() as conn:
            yield conn

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


def test_settings_view_no_cookies(test_client: TestClient) -> None:
    """Settings page renders correctly when no cookies file exists."""
    response = test_client.get("/settings")
    assert response.status_code == 200
    assert "Einstellungen" in response.text
    assert "YouTube-Authentifizierung (Cookies)" in response.text
    assert "Keine Cookies hinterlegt" in response.text
    assert "Netscape" in response.text


def test_settings_view_with_valid_cookies(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Settings page reflects active status when a valid cookies file exists."""
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(VALID_COOKIES_TEXT, encoding="utf-8")
    monkeypatch.setattr(config.settings, "cookies_path", cookies_file)

    response = test_client.get("/settings")
    assert response.status_code == 200
    assert "Cookies aktiv" in response.text
    assert "Gespeicherte Cookies löschen" in response.text


def test_settings_view_with_invalid_cookies(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Settings page reflects invalid status when a corrupted cookies file exists."""
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text("not a netscape cookie file", encoding="utf-8")
    monkeypatch.setattr(config.settings, "cookies_path", cookies_file)

    response = test_client.get("/settings")
    assert response.status_code == 200
    assert "Datei ungültig" in response.text


def test_save_cookies_via_file_upload(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saving cookies via .txt file upload writes the file and sets permissions."""
    dest = tmp_path / "data" / "cookies.txt"
    monkeypatch.setattr(config.settings, "cookies_path", dest)

    files = {"file": ("cookies.txt", VALID_COOKIES_TEXT.encode("utf-8"), "text/plain")}
    response = test_client.post(
        "/api/settings/cookies",
        files=files,
        data={"library": "kids"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?lib=kids&msg=cookies_saved"

    assert dest.is_file()
    assert dest.read_text(encoding="utf-8").strip() == VALID_COOKIES_TEXT.strip()
    # Check permissions (0o600)
    mode = os.stat(dest).st_mode & 0o777
    assert mode == 0o600


def test_save_cookies_via_textarea(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saving cookies via textarea writes the file."""
    dest = tmp_path / "data" / "cookies.txt"
    monkeypatch.setattr(config.settings, "cookies_path", dest)

    response = test_client.post(
        "/api/settings/cookies",
        data={"cookies": VALID_COOKIES_TEXT, "library": "tech"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?lib=tech&msg=cookies_saved"

    assert dest.is_file()
    assert dest.read_text(encoding="utf-8").strip() == VALID_COOKIES_TEXT.strip()


def test_save_cookies_empty_rejected(test_client: TestClient) -> None:
    """Submitting empty cookie content redirects with cookies_empty alert."""
    response = test_client.post(
        "/api/settings/cookies",
        data={"cookies": "   \n  "},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "msg=cookies_empty" in response.headers["location"]


def test_save_cookies_invalid_rejected(test_client: TestClient) -> None:
    """Submitting non-Netscape content redirects with cookies_invalid alert."""
    response = test_client.post(
        "/api/settings/cookies",
        data={"cookies": "just random invalid text without tabs or hash"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "msg=cookies_invalid" in response.headers["location"]


def test_delete_cookies(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deleting cookies unlinks the file and redirects with flash notice."""
    dest = tmp_path / "data" / "cookies.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(VALID_COOKIES_TEXT, encoding="utf-8")
    monkeypatch.setattr(config.settings, "cookies_path", dest)
    assert dest.is_file()

    response = test_client.post(
        "/api/settings/cookies/delete",
        data={"library": "kids"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?lib=kids&msg=cookies_deleted"
    assert not dest.exists()


def test_settings_flash_messages(test_client: TestClient) -> None:
    """Flash messages are correctly rendered on the settings page."""
    res_saved = test_client.get("/settings?msg=cookies_saved")
    assert "Cookies wurden erfolgreich gespeichert" in res_saved.text

    res_deleted = test_client.get("/settings?msg=cookies_deleted")
    assert "Cookies wurden gelöscht" in res_deleted.text

    res_invalid = test_client.get("/settings?msg=cookies_invalid")
    assert "Ungültiges Cookie-Format" in res_invalid.text


def test_libraries_management(test_client: TestClient) -> None:
    """Libraries can be added, viewed and deleted via settings."""
    # Add new library
    res = test_client.post(
        "/api/settings/libraries",
        data={"name": "gaming", "label": "Gaming", "icon": "🎮", "library": "all"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=library_added" in res.headers["location"]

    # Verify present on page
    view = test_client.get("/settings")
    assert "gaming" in view.text
    assert "Gaming" in view.text

    # Cannot delete default
    del_default = test_client.post(
        "/api/settings/libraries/default/delete",
        data={"library": "all"},
        follow_redirects=False,
    )
    assert del_default.status_code == 303
    assert "msg=library_delete_forbidden" in del_default.headers["location"]

    # Delete gaming
    del_res = test_client.post(
        "/api/settings/libraries/gaming/delete",
        data={"library": "all"},
        follow_redirects=False,
    )
    assert del_res.status_code == 303
    assert "msg=library_deleted" in del_res.headers["location"]

    # Verify gone
    view2 = test_client.get("/settings")
    assert "<code>gaming</code>" not in view2.text


def test_cannot_delete_library_with_channels(test_client: TestClient, tmp_path: Path) -> None:
    """Libraries containing channels cannot be deleted."""
    test_db = Database(tmp_path / "settings_test.db")
    with test_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library)
            VALUES ('UCtest', 'Test Channel', 'https://youtube.com/test', 'Test Channel', 'kids')
            """
        )

    res = test_client.post(
        "/api/settings/libraries/kids/delete",
        data={"library": "all"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=library_delete_forbidden" in res.headers["location"]


def test_retention_settings_and_run(test_client: TestClient) -> None:
    """Global retention settings can be saved and cleanup triggered."""
    res = test_client.post(
        "/api/settings/retention",
        data={"retention_days": "60", "retention_count": "25", "library": "all"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=retention_saved" in res.headers["location"]

    view = test_client.get("/settings")
    assert 'value="60"' in view.text
    assert 'value="25"' in view.text

    # Trigger retention run
    run_res = test_client.post(
        "/api/settings/retention/run",
        data={"library": "all"},
        follow_redirects=False,
    )
    assert run_res.status_code == 303
    assert "msg=retention_triggered" in run_res.headers["location"]


def test_download_and_format_settings(test_client: TestClient) -> None:
    """Download preferences (height, sort, subtitles, rate limit) can be configured."""
    res = test_client.post(
        "/api/settings/download",
        data={
            "default_max_height": "1080",
            "format_sort": "res:1080,fps",
            "subtitles_langs": "de,en,fr",
            "throttled_rate_bytes": "5242880",
            "library": "all",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=download_settings_saved" in res.headers["location"]

    view = test_client.get("/settings")
    assert "res:1080,fps" in view.text
    assert "de,en,fr" in view.text
    assert "5242880" in view.text


def test_filter_and_sponsorblock_settings(test_client: TestClient) -> None:
    """Filter and SponsorBlock preferences can be configured."""
    res = test_client.post(
        "/api/settings/filters",
        data={
            "ignore_shorts": "1",
            "ignore_live": "1",
            "sponsorblock_enabled": "1",
            "sponsorblock_categories": "sponsor,intro,outro",
            "library": "all",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=filter_settings_saved" in res.headers["location"]

    view = test_client.get("/settings")
    assert "sponsor,intro,outro" in view.text


def test_schedule_and_poller_trigger(test_client: TestClient) -> None:
    """Poll schedule can be updated and sync manually triggered."""
    res = test_client.post(
        "/api/settings/schedule",
        data={"rss_poll_interval": "1800", "library": "all"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=schedule_saved" in res.headers["location"]

    view = test_client.get("/settings")
    assert 'value="1800" selected' in view.text

    # Trigger poller
    poll_res = test_client.post(
        "/api/settings/poller/run",
        data={"library": "all"},
        follow_redirects=False,
    )
    assert poll_res.status_code == 303
    assert "msg=poller_triggered" in poll_res.headers["location"]


def test_cookie_test_action(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Testing cookies reports status via redirect or HTMX fragment."""
    # When no cookies exist
    res_no = test_client.post("/api/settings/cookies/test", follow_redirects=False)
    assert res_no.status_code == 303
    assert "msg=cookie_test_err" in res_no.headers["location"]

    # When valid cookies exist
    cookies_file = tmp_path / "data" / "cookies.txt"
    cookies_file.parent.mkdir(parents=True, exist_ok=True)
    cookies_file.write_text(VALID_COOKIES_TEXT, encoding="utf-8")
    monkeypatch.setattr(config.settings, "cookies_path", cookies_file)

    res_valid = test_client.post("/api/settings/cookies/test", follow_redirects=False)
    assert res_valid.status_code == 303
    assert "msg=cookie_test_ok" in res_valid.headers["location"]

    # Via HTMX header
    res_htmx = test_client.post(
        "/api/settings/cookies/test",
        headers={"HX-Request": "true"},
    )
    assert res_htmx.status_code == 200
    assert "badge-status-completed" in res_htmx.text


def test_jellyfin_settings_and_connection_test(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jellyfin connection settings can be saved and tested."""
    res = test_client.post(
        "/api/settings/jellyfin",
        data={
            "jellyfin_url": "http://jellyfin:8096",
            "jellyfin_api_key": "secret123",
            "library": "all",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=jellyfin_saved" in res.headers["location"]

    # Mock test connection
    async def mock_test_jf(url: str, key: str) -> tuple[bool, str]:
        return True, "Verbindung erfolgreich! Server: MyJellyfin (v10.8.10)"

    monkeypatch.setattr("app.api.endpoints.actions.test_jellyfin_connection", mock_test_jf)

    res_test = test_client.post(
        "/api/settings/jellyfin/test",
        headers={"HX-Request": "true"},
        data={"jellyfin_url": "http://jellyfin:8096", "jellyfin_api_key": "secret123"},
    )
    assert res_test.status_code == 200
    assert "MyJellyfin" in res_test.text
    assert "badge-status-completed" in res_test.text
