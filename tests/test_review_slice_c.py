"""Reproductions for review findings of Slice C (see review plan, C1-C5).

Assumed API: `app.core.config.Settings` reads the environment at instantiation, and the
app reads the module-level `app.core.config.settings` object at call time. `_apply_env`
therefore sets env vars and copies a fresh `Settings()` into the global object
(monkeypatch restores everything afterwards).
C5 (no JS runtime / yt-dlp-ejs in the image) is verified manually.
"""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.core.config import Settings
from app.db import Database, get_db
from app.downloader.options import DownloadOptions, DownloadResult, VideoMetadata
from app.jellyfin.layout import JellyfinLayout
from app.jellyfin.nfo import NfoGenerator
from app.worker.queue import JobQueue
from app.worker.worker import BackgroundWorker


def _apply_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fresh = Settings()
    for f in dataclasses.fields(fresh):
        monkeypatch.setattr(config.settings, f.name, getattr(fresh, f.name))


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "slice_c.db")
    db.run_migrations()
    return db


def _run_worker_download(temp_db: Database, tmp_path: Path) -> DownloadOptions:
    """Run one download_video job with a fake downloader; return the DownloadOptions used."""
    meta = VideoMetadata(
        id="vidSliceC01",
        title="Synthetic Slice C",
        channel="Slice C Channel",
        channel_id="UCsliceC00001",
        channel_url="https://youtube.com/@slicec",
        upload_date="2026-03-15",
        duration=60,
    )
    video_path = tmp_path / "media" / "synthetic.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"dummy mp4")

    fake = MagicMock()
    fake.async_extract_info = AsyncMock(return_value=meta)
    fake.async_download = AsyncMock(
        return_value=DownloadResult(id=meta.id, metadata=meta, video_path=video_path)
    )
    queue = JobQueue(database=temp_db)
    worker = BackgroundWorker(
        queue=queue,
        downloader_service=fake,
        layout=JellyfinLayout(media_root=tmp_path / "media"),
        nfo=NfoGenerator(),
        database=temp_db,
    )
    job = queue.enqueue("download_video", {"video_id": meta.id})
    assert job is not None
    assert asyncio.run(worker.process_next_job()) is True
    assert queue.get_job(job.id).status == "completed"
    return fake.async_download.call_args.kwargs["options"]


# --- C1: one settings source, env names as documented -------------------------------


@pytest.mark.xfail(strict=True, reason="C1: Settings reads MEDIA_DIR instead of MEDIA_PATH")
def test_c1_settings_media_dir_uses_media_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media = tmp_path / "jellyfin-media"
    monkeypatch.delenv("MEDIA_DIR", raising=False)
    monkeypatch.setenv("MEDIA_PATH", str(media))
    assert Settings().media_dir == media


@pytest.mark.xfail(strict=True, reason="C1: Database default path differs from settings")
def test_c1_database_default_matches_settings() -> None:
    assert Database().db_path == Path(config.settings.database_path)


# --- C2: cookies reach yt-dlp ---------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="C2: worker never passes cookies_path")
def test_c2_worker_passes_cookies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_db: Database
) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tX\tsynthetic\n"
    )
    _apply_env(monkeypatch, COOKIES_PATH=str(cookies))

    options = _run_worker_download(temp_db, tmp_path)
    assert options.cookies_path is not None
    assert Path(options.cookies_path) == cookies


# --- C3: FORMAT_SORT / THROTTLED_RATE_BYTES are read ----------------------------------


@pytest.mark.xfail(strict=True, reason="C3: FORMAT_SORT/THROTTLED_RATE_BYTES never read")
def test_c3_worker_uses_format_sort_and_throttle_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_db: Database
) -> None:
    _apply_env(monkeypatch, FORMAT_SORT="res:1080,fps", THROTTLED_RATE_BYTES="5242880")

    options = _run_worker_download(temp_db, tmp_path)
    assert options.format_sort in ("res:1080,fps", ["res:1080", "fps"])
    assert options.throttled_rate_bytes == 5242880


# --- C4: libraries come from LIBRARIES ------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, temp_db: Database) -> TestClient:
    import app.main as main_module

    monkeypatch.setattr("app.main.db", temp_db)
    monkeypatch.setattr("app.worker.queue.db", temp_db)
    monkeypatch.setattr(main_module.worker, "start", lambda: None)
    monkeypatch.setattr(main_module.worker, "stop", AsyncMock())
    monkeypatch.setattr(main_module.scheduler, "start", lambda: None)
    monkeypatch.setattr(main_module.scheduler, "stop", AsyncMock())

    def override_get_db():
        with temp_db.connection() as conn:
            yield conn

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app) as c:
        yield c
    main_module.app.dependency_overrides.clear()


@pytest.mark.xfail(strict=True, reason="C4: libraries hard-coded in templates")
@pytest.mark.parametrize("path", ["/", "/subscriptions"])
def test_c4_libraries_from_env(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, path: str
) -> None:
    _apply_env(monkeypatch, LIBRARIES="default,musik")

    response = client.get(path)
    assert response.status_code == 200
    assert "musik" in response.text
    assert "dokus" not in response.text
    assert "kids" not in response.text
