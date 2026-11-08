"""Reproductions for review findings of Slice B (see review plan, B1-B6)."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.downloader.exceptions import VideoUnavailableError
from app.jellyfin.layout import JellyfinLayout
from app.jellyfin.nfo import NfoGenerator
from app.retention import RetentionEngine
from app.worker.poller import SubscriptionPoller
from app.worker.queue import JobQueue
from app.worker.worker import BackgroundWorker

CHANNEL_ID = "UCsliceB00001"


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "slice_b.db")
    db.run_migrations()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, auto_download)
            VALUES (?, 'Slice B Channel', 'https://youtube.com/@sliceb', 'Slice B Channel', 1)
            """,
            (CHANNEL_ID,),
        )
    return db


@pytest.fixture
def queue(temp_db: Database) -> JobQueue:
    return JobQueue(database=temp_db)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed(entries: list[tuple[str, datetime]]) -> str:
    items = "".join(
        f"""
     <entry>
      <yt:videoId>{vid}</yt:videoId>
      <yt:channelId>{CHANNEL_ID}</yt:channelId>
      <title>Synthetic {vid}</title>
      <link rel="alternate" href="https://www.youtube.com/watch?v={vid}"/>
      <published>{_iso(published)}</published>
     </entry>"""
        for vid, published in entries
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/"
          xmlns="http://www.w3.org/2005/Atom">{items}
    </feed>"""


def _poller(queue: JobQueue, temp_db: Database, xml: str) -> SubscriptionPoller:
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=xml)))
    return SubscriptionPoller(queue=queue, database=temp_db, client=client)


def _insert_video(
    temp_db: Database, video_id: str, published: datetime, media_dir: Path | None = None
) -> Path | None:
    video_path = None
    if media_dir is not None:
        media_dir.mkdir(parents=True, exist_ok=True)
        video_path = media_dir / f"2026-01-01 - Synthetic [{video_id}].mp4"
        video_path.write_bytes(b"dummy mp4")
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, file_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                video_id,
                CHANNEL_ID,
                f"Synthetic {video_id}",
                _iso(published),
                str(video_path) if video_path else None,
            ),
        )
    return video_path


# --- B1: deleted videos must not be re-downloaded by the poller ---------------------------


@pytest.mark.xfail(strict=True, reason="B1: poller re-enqueues manually deleted video")
def test_b1_manually_deleted_video_is_not_repolled(
    queue: JobQueue, temp_db: Database, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    video_path = _insert_video(temp_db, "vidB1del01", now - timedelta(days=1), tmp_path / "m")

    result = RetentionEngine(database=temp_db).delete_video("vidB1del01")
    assert result.success
    assert video_path is not None and not video_path.exists()

    poller = _poller(queue, temp_db, _feed([("vidB1del01", now - timedelta(days=1))]))
    assert poller.poll_channel(CHANNEL_ID) == []


@pytest.mark.xfail(strict=True, reason="B1: poller re-enqueues video removed by retention TTL")
def test_b1_retention_deleted_video_is_not_repolled(queue: JobQueue, temp_db: Database) -> None:
    now = datetime.now(UTC)
    with temp_db.connection() as conn:
        conn.execute("UPDATE channels SET retention_days = 7 WHERE id = ?", (CHANNEL_ID,))
    _insert_video(temp_db, "vidB1ttl01", now - timedelta(days=10))

    results = RetentionEngine(database=temp_db).evaluate_channel_retention(CHANNEL_ID)
    assert [r.video_id for r in results if r.success] == ["vidB1ttl01"]

    poller = _poller(queue, temp_db, _feed([("vidB1ttl01", now - timedelta(days=10))]))
    assert poller.poll_channel(CHANNEL_ID) == []


# --- B2: permanently unavailable videos must not be retried every poll --------------------


@pytest.mark.xfail(strict=True, reason="B2: VideoUnavailableError video is re-enqueued")
def test_b2_unavailable_video_is_not_retried(
    queue: JobQueue, temp_db: Database, tmp_path: Path
) -> None:
    fake = MagicMock()
    fake.async_extract_info = AsyncMock(side_effect=VideoUnavailableError("members-only"))
    fake.async_download = AsyncMock()
    worker = BackgroundWorker(
        queue=queue,
        downloader_service=fake,
        layout=JellyfinLayout(media_root=tmp_path / "media"),
        nfo=NfoGenerator(),
        database=temp_db,
    )

    now = datetime.now(UTC)
    poller = _poller(queue, temp_db, _feed([("vidB2una01", now - timedelta(hours=2))]))
    assert poller.poll_channel(CHANNEL_ID) == ["vidB2una01"]

    assert asyncio.run(worker.process_next_job()) is True
    failed = queue.list_jobs(status="failed")
    assert [j.payload.get("video_id") for j in failed] == ["vidB2una01"]

    assert poller.poll_channel(CHANNEL_ID) == []


# --- B3: poller must not enqueue videos that fall under retention immediately ------------


@pytest.mark.xfail(strict=True, reason="B3: poller enqueues entries older than retention_days")
def test_b3_poller_respects_retention_days(queue: JobQueue, temp_db: Database) -> None:
    now = datetime.now(UTC)
    with temp_db.connection() as conn:
        conn.execute("UPDATE channels SET retention_days = 7 WHERE id = ?", (CHANNEL_ID,))

    xml = _feed([("vidB3new01", now - timedelta(days=1)), ("vidB3old01", now - timedelta(days=10))])
    assert _poller(queue, temp_db, xml).poll_channel(CHANNEL_ID) == ["vidB3new01"]


@pytest.mark.xfail(strict=True, reason="B3: poller enqueues entries beyond retention_count")
def test_b3_poller_respects_retention_count(queue: JobQueue, temp_db: Database) -> None:
    now = datetime.now(UTC)
    with temp_db.connection() as conn:
        conn.execute("UPDATE channels SET retention_count = 2 WHERE id = ?", (CHANNEL_ID,))
    _insert_video(temp_db, "vidB3kept1", now - timedelta(days=1))
    _insert_video(temp_db, "vidB3kept2", now - timedelta(days=2))

    xml = _feed(
        [
            ("vidB3fresh", now - timedelta(hours=3)),  # newer than known -> download
            ("vidB3kept1", now - timedelta(days=1)),
            ("vidB3kept2", now - timedelta(days=2)),
            ("vidB3stale", now - timedelta(days=5)),  # older than 2 newest known -> skip
        ]
    )
    assert _poller(queue, temp_db, xml).poll_channel(CHANNEL_ID) == ["vidB3fresh"]


# --- B4: scheduler triggers RSS polling and retention -------------------------------------


@pytest.mark.xfail(strict=True, reason="B4: no scheduler module exists")
def test_b4_scheduler_tick_enqueues_poll_and_retention(queue: JobQueue) -> None:
    """Desired API: `app.worker.scheduler.Scheduler(queue=...)` with a sync `tick()` that
    enqueues one `poll_channels` and one `retention` job (deduplicated)."""
    from app.worker.scheduler import Scheduler

    scheduler = Scheduler(queue=queue)
    scheduler.tick()
    scheduler.tick()

    types = sorted(j.type for j in queue.list_jobs(status="pending"))
    assert types == ["poll_channels", "retention"]


@pytest.mark.xfail(strict=True, reason="B4: lifespan starts no scheduler")
def test_b4_lifespan_runs_scheduler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Desired API: module-level `app.main.scheduler` with attribute `interval_seconds`;
    the lifespan starts it and it calls `tick()` periodically."""
    import app.main as main_module

    temp = Database(db_path=tmp_path / "lifespan.db")
    monkeypatch.setattr("app.main.db", temp)
    monkeypatch.setattr(main_module.worker, "start", lambda: None)
    monkeypatch.setattr(main_module.worker, "stop", AsyncMock())

    scheduler = main_module.scheduler
    tick = MagicMock()
    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(scheduler, "interval_seconds", 0.01)

    with TestClient(main_module.app):
        deadline = time.monotonic() + 2.0
        while not tick.called and time.monotonic() < deadline:
            time.sleep(0.02)
    assert tick.called


def _worker(queue: JobQueue, temp_db: Database, tmp_path: Path) -> BackgroundWorker:
    return BackgroundWorker(
        queue=queue,
        downloader_service=MagicMock(),
        layout=JellyfinLayout(media_root=tmp_path / "media"),
        nfo=NfoGenerator(),
        database=temp_db,
    )


@pytest.mark.xfail(strict=True, reason="B4: worker cannot run poll_channels jobs")
def test_b4_worker_runs_poll_channels_job(
    queue: JobQueue, temp_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    xml = _feed([("vidB4poll1", now - timedelta(hours=1))])
    monkeypatch.setattr(SubscriptionPoller, "fetch_feed_content", lambda self, url: xml)

    job = queue.enqueue("poll_channels", {})
    assert job is not None
    assert asyncio.run(_worker(queue, temp_db, tmp_path).process_next_job()) is True

    assert queue.get_job(job.id).status == "completed"
    pending = queue.list_jobs(status="pending")
    assert [j.payload.get("video_id") for j in pending] == ["vidB4poll1"]


@pytest.mark.xfail(strict=True, reason="B4: worker cannot run retention jobs")
def test_b4_worker_runs_retention_job(queue: JobQueue, temp_db: Database, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    with temp_db.connection() as conn:
        conn.execute("UPDATE channels SET retention_days = 7 WHERE id = ?", (CHANNEL_ID,))
    _insert_video(temp_db, "vidB4keep1", now - timedelta(days=1))
    _insert_video(temp_db, "vidB4old01", now - timedelta(days=10))

    job = queue.enqueue("retention", {})
    assert job is not None
    assert asyncio.run(_worker(queue, temp_db, tmp_path).process_next_job()) is True

    assert queue.get_job(job.id).status == "completed"
    with temp_db.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM videos WHERE status = 'completed' ORDER BY id"
        ).fetchall()
    assert [r["id"] for r in rows] == ["vidB4keep1"]


# --- B5: dedup behaviour must stay the same once it no longer scans JSON payloads ---------


def test_b5_dedup_behaviour_regression(queue: JobQueue) -> None:
    payload = {"video_id": "vidB5dup01", "url": "https://www.youtube.com/watch?v=vidB5dup01"}
    first = queue.enqueue("download_video", payload, deduplicate_key="video_id")
    assert first is not None
    # pending duplicate -> skipped
    assert queue.enqueue("download_video", payload, deduplicate_key="video_id") is None
    # other video id -> allowed
    other = {"video_id": "vidB5dup02"}
    assert queue.enqueue("download_video", other, deduplicate_key="video_id") is not None

    # running duplicate -> skipped
    claimed = queue.claim_next_job()
    assert claimed is not None and claimed.id == first.id
    assert queue.enqueue("download_video", payload, deduplicate_key="video_id") is None

    # finished job no longer blocks the queue itself
    queue.mark_completed(first.id)
    assert queue.enqueue("download_video", payload, deduplicate_key="video_id") is not None


# --- B6: finished jobs must be cleaned up ------------------------------------------------


@pytest.mark.xfail(strict=True, reason="B6: no cleanup for finished jobs")
def test_b6_cleanup_removes_old_finished_jobs(queue: JobQueue, temp_db: Database) -> None:
    """Desired API: `JobQueue.cleanup_finished(older_than_days: int) -> int` deletes
    completed/failed/cancelled jobs finished more than N days ago."""
    now = datetime.now(UTC)
    old = _iso(now - timedelta(days=40))
    recent = _iso(now - timedelta(days=1))
    rows = [
        ("job_old_done", "completed", old),
        ("job_old_fail", "failed", old),
        ("job_old_cncl", "cancelled", old),
        ("job_new_done", "completed", recent),
        ("job_old_pend", "pending", None),
    ]
    with temp_db.connection() as conn:
        for job_id, status, finished in rows:
            conn.execute(
                """
                INSERT INTO jobs (id, type, payload, status, created_at, completed_at)
                VALUES (?, 'download_video', '{}', ?, ?, ?)
                """,
                (job_id, status, old, finished),
            )

    removed = queue.cleanup_finished(older_than_days=30)

    assert removed == 3
    with temp_db.connection() as conn:
        left = [r["id"] for r in conn.execute("SELECT id FROM jobs ORDER BY id").fetchall()]
    assert left == ["job_new_done", "job_old_pend"]
