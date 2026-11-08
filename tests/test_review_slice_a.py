"""Reproductions for review findings of Slice A (see review plan, A1-A5)."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Database, get_db
from app.downloader.options import DownloadResult, VideoMetadata
from app.jellyfin.layout import JellyfinLayout
from app.jellyfin.nfo import NfoGenerator
from app.main import app
from app.retention import RetentionEngine
from app.worker.queue import JobQueue
from app.worker.worker import BackgroundWorker


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "slice_a.db")
    db.run_migrations()
    return db


@pytest.fixture
def queue(temp_db: Database) -> JobQueue:
    return JobQueue(database=temp_db)


@pytest.fixture
def test_client(temp_db: Database, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("app.db.database.db", temp_db)
    monkeypatch.setattr("app.db.db", temp_db)
    monkeypatch.setattr("app.main.db", temp_db)
    monkeypatch.setattr("app.retention.retention_engine.db", temp_db)
    monkeypatch.setattr("app.worker.queue.db", temp_db)
    monkeypatch.setattr("app.api.endpoints.actions.db", temp_db)

    def override_get_db():
        with temp_db.connection() as conn:
            yield conn

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _make_fake_downloader(meta: VideoMetadata) -> MagicMock:
    """Fake downloader that writes a dummy file into the requested output directory."""
    fake = MagicMock()
    fake.async_extract_info = AsyncMock(return_value=meta)

    async def _download(url: str, options: Any, progress_hook: Any = None) -> DownloadResult:
        out_dir = Path(options.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        video_path = out_dir / options.output_template.replace("%(ext)s", "mp4")
        video_path.write_bytes(b"dummy mp4")
        return DownloadResult(
            id=meta.id,
            metadata=meta,
            video_path=video_path,
            thumbnail_path=None,
            subtitle_paths=[],
            file_size=9,
        )

    fake.async_download = AsyncMock(side_effect=_download)
    return fake


def _make_worker(
    queue: JobQueue, temp_db: Database, media_dir: Path, meta: VideoMetadata
) -> tuple[BackgroundWorker, MagicMock]:
    fake = _make_fake_downloader(meta)
    worker = BackgroundWorker(
        queue=queue,
        downloader_service=fake,
        layout=JellyfinLayout(media_root=media_dir),
        nfo=NfoGenerator(),
        database=temp_db,
    )
    return worker, fake


SAMPLE_META = VideoMetadata(
    id="vidA0001",
    title="Synthetic Test Video",
    channel="Synthetic Channel",
    channel_id="UCsynthetic0001",
    channel_url="https://www.youtube.com/channel/UCsynthetic0001",
    upload_date="2026-03-15",
    duration=120,
    description="Synthetic description",
)


def test_a1_quick_download_uses_channel_from_metadata(
    queue: JobQueue, temp_db: Database, tmp_path: Path
) -> None:
    media_dir = tmp_path / "media"
    worker, fake = _make_worker(queue, temp_db, media_dir, SAMPLE_META)

    # Quick download payload: only url + library, no channel_* fields
    job = queue.enqueue(
        "download_video",
        {"url": "https://www.youtube.com/watch?v=vidA0001", "library": "default"},
    )
    assert job is not None
    assert asyncio.run(worker.process_next_job()) is True
    assert queue.get_job(job.id).status == "completed"

    options = fake.async_download.call_args.kwargs["options"]
    expected_season = media_dir / "default" / "Synthetic Channel" / "Season 2026"
    assert Path(options.output_dir) == expected_season
    assert not (media_dir / "default" / "Unknown Channel").exists()

    with temp_db.connection() as conn:
        row = conn.execute("SELECT channel_id FROM videos WHERE id = ?", ("vidA0001",)).fetchone()
    assert row["channel_id"] == "UCsynthetic0001"


def test_a2_resubscribe_keeps_videos(test_client: TestClient, temp_db: Database) -> None:
    form = {
        "channel_input": "UCresub0001",
        "name": "Resub Channel",
        "library": "default",
    }
    res = test_client.post("/api/subscriptions", data=form, follow_redirects=False)
    assert res.status_code == 303

    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, is_pinned)
            VALUES ('vidPinned1', 'UCresub0001', 'Pinned Video', '2026-01-01', 1)
            """
        )

    res = test_client.post("/api/subscriptions", data=form, follow_redirects=False)
    assert res.status_code == 303

    with temp_db.connection() as conn:
        row = conn.execute("SELECT is_pinned FROM videos WHERE id = 'vidPinned1'").fetchone()
    assert row is not None, "video row was deleted by re-subscribing"
    assert row["is_pinned"] == 1


def test_a3_retention_count_zero_means_unlimited(temp_db: Database) -> None:
    engine = RetentionEngine(database=temp_db)
    now = datetime.now(UTC)
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, retention_count)
            VALUES ('UCzero0001', 'Zero Channel', 'https://youtube.com/@zero', 'Zero Channel', 0)
            """
        )
        for i in range(3):
            conn.execute(
                """
                INSERT INTO videos (id, channel_id, title, published_at)
                VALUES (?, 'UCzero0001', ?, ?)
                """,
                (f"vidZero{i}", f"Video {i}", (now - timedelta(days=i)).strftime("%Y-%m-%d")),
            )

    results = engine.evaluate_channel_retention("UCzero0001")
    assert [r.video_id for r in results if r.success] == []
    with temp_db.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    assert count == 3


@pytest.mark.parametrize("value", ["0", "", " "])
def test_a3_subscription_zero_or_empty_retention_stored_as_null(
    test_client: TestClient, temp_db: Database, value: str
) -> None:
    form = {
        "channel_input": "UCnull0001",
        "name": "Null Channel",
        "library": "default",
        "retention_count": value,
        "retention_days": value,
    }
    res = test_client.post("/api/subscriptions", data=form, follow_redirects=False)
    assert res.status_code == 303

    with temp_db.connection() as conn:
        row = conn.execute(
            "SELECT retention_count, retention_days FROM channels WHERE id = 'UCnull0001'"
        ).fetchone()
    assert row["retention_count"] is None
    assert row["retention_days"] is None


def test_a4_sync_does_not_block_event_loop(
    temp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.endpoints import actions

    monkeypatch.setattr("app.api.endpoints.actions.db", temp_db)
    monkeypatch.setattr("app.worker.queue.db", temp_db)
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name)
            VALUES ('UCslow0001', 'Slow Channel', 'https://youtube.com/@slow', 'Slow Channel')
            """
        )

    def slow_get(self: httpx.Client, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        time.sleep(0.3)  # simulate slow YouTube RSS response
        return httpx.Response(
            200,
            text="<feed xmlns='http://www.w3.org/2005/Atom'/>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", slow_get)

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        max_gap = 0.0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal max_gap
            last = loop.time()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                current = loop.time()
                max_gap = max(max_gap, current - last)
                last = current

        tick_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.02)
        await actions.sync_single_subscription(channel_id="UCslow0001")
        stop.set()
        await tick_task
        return max_gap

    max_gap = asyncio.run(scenario())
    assert max_gap < 0.15, f"event loop was blocked for {max_gap:.2f}s"


def test_a5_redownload_keeps_pin_and_created_at(
    queue: JobQueue, temp_db: Database, tmp_path: Path
) -> None:
    media_dir = tmp_path / "media"
    worker, _ = _make_worker(queue, temp_db, media_dir, SAMPLE_META)
    payload = {
        "video_id": "vidA0001",
        "channel_id": "UCsynthetic0001",
        "channel_name": "Synthetic Channel",
        "library": "default",
    }

    queue.enqueue("download_video", payload)
    assert asyncio.run(worker.process_next_job()) is True

    original_created = "2020-01-01T00:00:00Z"
    with temp_db.connection() as conn:
        conn.execute(
            "UPDATE videos SET is_pinned = 1, created_at = ? WHERE id = 'vidA0001'",
            (original_created,),
        )

    queue.enqueue("download_video", payload)
    assert asyncio.run(worker.process_next_job()) is True

    with temp_db.connection() as conn:
        row = conn.execute(
            "SELECT is_pinned, created_at, status FROM videos WHERE id = 'vidA0001'"
        ).fetchone()
    assert row["status"] == "completed"
    assert row["is_pinned"] == 1
    assert row["created_at"] == original_created
