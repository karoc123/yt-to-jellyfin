"""Tests for SQLite queue, background worker, crash recovery, and RSS poller."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.db.database import Database
from app.downloader.options import DownloadResult, VideoMetadata
from app.jellyfin.layout import JellyfinLayout
from app.jellyfin.nfo import NfoGenerator
from app.worker.poller import RssFeedParser, SubscriptionPoller
from app.worker.queue import JobQueue
from app.worker.worker import BackgroundWorker


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db_file = tmp_path / "worker_test.db"
    db = Database(db_path=db_file)
    db.run_migrations()
    return db


@pytest.fixture
def queue(temp_db: Database) -> JobQueue:
    return JobQueue(database=temp_db)


def test_queue_enqueue_claim_and_completion(queue: JobQueue) -> None:
    """Test enqueue, atomic claim, progress update, and completion."""
    # Enqueue a job
    job = queue.enqueue("download_video", {"video_id": "vid123", "title": "Test 1"})
    assert job is not None
    assert job.status == "pending"
    assert job.progress == 0

    # Claim job
    claimed = queue.claim_next_job()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.started_at is not None

    # No more pending jobs
    assert queue.claim_next_job() is None

    # Update progress
    queue.update_progress(claimed.id, 45)
    updated = queue.get_job(claimed.id)
    assert updated is not None
    assert updated.progress == 45

    # Complete job
    queue.mark_completed(claimed.id)
    completed = queue.get_job(claimed.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.progress == 100
    assert completed.completed_at is not None


def test_queue_deduplication(queue: JobQueue) -> None:
    """Test deduplicate_key prevents duplicate pending/running jobs."""
    job1 = queue.enqueue(
        "download_video",
        {"video_id": "vid_dup", "title": "Duplicate Video"},
        deduplicate_key="video_id",
    )
    assert job1 is not None

    # Second enqueue with same video_id while job1 is pending should return None
    job2 = queue.enqueue(
        "download_video",
        {"video_id": "vid_dup", "title": "Duplicate Video Again"},
        deduplicate_key="video_id",
    )
    assert job2 is None

    # Claim job1 (now 'running')
    claimed = queue.claim_next_job()
    assert claimed is not None
    assert claimed.status == "running"

    # Enqueue while running should still be rejected
    job3 = queue.enqueue(
        "download_video",
        {"video_id": "vid_dup"},
        deduplicate_key="video_id",
    )
    assert job3 is None

    # Once completed, a new job for the same video_id can be queued
    queue.mark_completed(claimed.id)
    job4 = queue.enqueue(
        "download_video",
        {"video_id": "vid_dup"},
        deduplicate_key="video_id",
    )
    assert job4 is not None


def test_queue_update_payload(queue: JobQueue) -> None:
    """Test updating existing job payload fields in-place."""
    job = queue.enqueue("download_video", {"video_id": "vid_orig", "url": "http://example.com"})
    assert job is not None

    queue.update_payload(
        job.id, {"title": "Updated Title", "thumbnail": "http://example.com/thumb.jpg"}
    )
    updated = queue.get_job(job.id)
    assert updated is not None
    assert updated.payload["video_id"] == "vid_orig"
    assert updated.payload["url"] == "http://example.com"
    assert updated.payload["title"] == "Updated Title"
    assert updated.payload["thumbnail"] == "http://example.com/thumb.jpg"


def test_queue_crash_recovery(queue: JobQueue, temp_db: Database) -> None:
    """Test recovering crashed 'running' jobs back to 'pending'."""
    job = queue.enqueue("download_video", {"video_id": "vid_crash"})
    assert job is not None

    claimed = queue.claim_next_job()
    assert claimed is not None
    assert claimed.status == "running"

    # Simulate worker crash and recovery
    recovered_count = queue.recover_crashed_jobs()
    assert recovered_count == 1

    recovered_job = queue.get_job(claimed.id)
    assert recovered_job is not None
    assert recovered_job.status == "pending"
    assert "Recovered" in (recovered_job.error_message or "")


def test_rss_feed_parser() -> None:
    """Test parsing YouTube Atom/RSS feed with normal videos, shorts, and live streams."""
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/"
          xmlns="http://www.w3.org/2005/Atom">
     <title>Sample Channel</title>
     <entry>
      <id>yt:video:vid_standard</id>
      <yt:videoId>vid_standard</yt:videoId>
      <yt:channelId>UC123456</yt:channelId>
      <title>Normal In-Depth Document</title>
      <link rel="alternate" href="https://www.youtube.com/watch?v=vid_standard"/>
      <author><name>Sample Channel</name></author>
      <published>2026-11-01T10:00:00+00:00</published>
      <media:group>
       <media:description>In-depth documentary description.</media:description>
       <media:thumbnail url="https://i.ytimg.com/vi/vid_standard/hqdefault.jpg"/>
      </media:group>
     </entry>
     <entry>
      <id>yt:video:vid_short</id>
      <yt:videoId>vid_short</yt:videoId>
      <yt:channelId>UC123456</yt:channelId>
      <title>Quick Tip #shorts</title>
      <link rel="alternate" href="https://www.youtube.com/shorts/vid_short"/>
      <published>2026-11-02T10:00:00+00:00</published>
     </entry>
     <entry>
      <id>yt:video:vid_live</id>
      <yt:videoId>vid_live</yt:videoId>
      <yt:channelId>UC123456</yt:channelId>
      <title>Live Stream Q&amp;A</title>
      <link rel="alternate" href="https://www.youtube.com/watch?v=vid_live"/>
      <published>2026-11-03T10:00:00+00:00</published>
     </entry>
    </feed>
    """

    entries = RssFeedParser.parse_feed(sample_xml)
    assert len(entries) == 3

    assert entries[0].video_id == "vid_standard"
    assert entries[0].is_short is False
    assert entries[0].is_live is False
    assert entries[0].author == "Sample Channel"
    assert entries[0].thumbnail_url == "https://i.ytimg.com/vi/vid_standard/hqdefault.jpg"

    assert entries[1].video_id == "vid_short"
    assert entries[1].is_short is True

    assert entries[2].video_id == "vid_live"
    assert entries[2].is_live is True


def test_subscription_poller(queue: JobQueue, temp_db: Database) -> None:
    """Test polling channel subscriptions and enqueuing download jobs (#8)."""
    # Insert subscribed channel
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("UCtest", "Test Channel", "https://youtube.com/@test", "Test Channel", "kids"),
        )

    sample_rss = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/"
          xmlns="http://www.w3.org/2005/Atom">
     <entry>
      <yt:videoId>ep1</yt:videoId>
      <yt:channelId>UCtest</yt:channelId>
      <title>Episode 1</title>
      <link rel="alternate" href="https://www.youtube.com/watch?v=ep1"/>
      <published>2026-11-05T12:00:00Z</published>
     </entry>
     <entry>
      <yt:videoId>ep_short</yt:videoId>
      <yt:channelId>UCtest</yt:channelId>
      <title>Short Clip #shorts</title>
      <link rel="alternate" href="https://www.youtube.com/shorts/ep_short"/>
      <published>2026-11-06T12:00:00Z</published>
     </entry>
    </feed>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sample_rss)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    poller = SubscriptionPoller(
        queue=queue,
        database=temp_db,
        client=client,
        ignore_shorts=True,
    )

    enqueued = poller.poll_channel("UCtest")
    assert enqueued == ["ep1"]

    # Verify job in queue
    job = queue.claim_next_job()
    assert job is not None
    assert job.payload["video_id"] == "ep1"
    assert job.payload["title"] == "Episode 1"
    assert "thumbnail" in job.payload
    assert job.payload["library"] == "kids"

    # Second poll should not duplicate job
    second_enqueued = poller.poll_channel("UCtest")
    assert second_enqueued == []


def test_background_worker_execution(
    queue: JobQueue,
    temp_db: Database,
    tmp_path: Path,
) -> None:
    """Test BackgroundWorker executing a download_video job end-to-end (#7)."""
    media_dir = tmp_path / "media"
    layout = JellyfinLayout(media_root=media_dir)
    nfo = NfoGenerator()

    # Mock YtDlpService
    mock_downloader = MagicMock()
    mock_downloader.async_extract_info = AsyncMock(
        return_value=VideoMetadata(
            id="vid_worker_1",
            title="Worker Video Test",
            channel="Tech Channel",
            channel_id="UCtech",
            channel_url="https://youtube.com/@tech",
            upload_date="2026-11-08",
            duration=300,
            description="Detailed tech overview",
        )
    )

    dummy_video_path = (
        media_dir
        / "tech"
        / "Tech Channel"
        / "Season 2026"
        / "2026-11-08 - Worker Video Test [vid_worker_1].mp4"
    )
    dummy_video_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_video_path.write_bytes(b"dummy mp4")

    mock_downloader.async_download = AsyncMock(
        return_value=DownloadResult(
            id="vid_worker_1",
            metadata=mock_downloader.async_extract_info.return_value,
            video_path=dummy_video_path,
            thumbnail_path=None,
            subtitle_paths=[],
            file_size=len(b"dummy mp4"),
        )
    )

    worker = BackgroundWorker(
        queue=queue,
        downloader_service=mock_downloader,
        layout=layout,
        nfo=nfo,
        database=temp_db,
    )

    # Enqueue a download job with max_height and sponsorblock options
    job = queue.enqueue(
        "download_video",
        {
            "video_id": "vid_worker_1",
            "channel_id": "UCtech",
            "channel_name": "Tech Channel",
            "library": "tech",
            "max_height": 1080,
            "sponsorblock_remove": ["sponsor"],
        },
    )
    assert job is not None

    # Process job
    processed = asyncio.run(worker.process_next_job())
    assert processed is True

    # Job should be marked completed
    completed_job = queue.get_job(job.id)
    assert completed_job is not None
    assert completed_job.status == "completed"
    assert completed_job.progress == 100
    assert completed_job.payload.get("title") == "Worker Video Test"

    # Companion .nfo file should have been generated
    expected_nfo = layout.get_episode_nfo_path(dummy_video_path)
    assert expected_nfo.is_file()

    # Channel tvshow.nfo should have been generated
    channel_nfo = layout.get_channel_nfo_path("Tech Channel", library="tech")
    assert channel_nfo.is_file()

    # Video should be recorded in database
    with temp_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM videos WHERE id = ?", ("vid_worker_1",))
        video_row = cursor.fetchone()
        assert video_row is not None
        assert video_row["title"] == "Worker Video Test"
        assert video_row["file_path"] == str(dummy_video_path)

        # Quick download must NOT create an auto-download subscription (auto_download=0)
        cursor.execute("SELECT * FROM channels WHERE id = ?", ("UCtech",))
        channel_row = cursor.fetchone()
        assert channel_row is not None
        assert channel_row["auto_download"] == 0


def test_channel_rename_preserves_folder_continuity(
    queue: JobQueue,
    temp_db: Database,
    tmp_path: Path,
) -> None:
    """When a channel changes its name on YouTube, new videos stay in the existing folder."""
    media_dir = tmp_path / "media"
    layout = JellyfinLayout(media_root=media_dir)
    nfo = NfoGenerator()

    # Pre-populate channel in DB
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES (
                'UCrename123', 'Old Channel Name', 'https://youtube.com/@old',
                'Old Folder Name', 'tech', 1
            )
            """
        )

    # Mock YtDlpService returning updated channel name
    mock_downloader = MagicMock()
    mock_downloader.async_extract_info = AsyncMock(
        return_value=VideoMetadata(
            id="vid_renamed_1",
            title="Video After Rename",
            channel="New Channel Name",
            channel_id="UCrename123",
            channel_url="https://youtube.com/@new",
            upload_date="2026-11-08",
            duration=120,
            description="Test video",
        )
    )

    dummy_video_path = (
        media_dir
        / "tech"
        / "Old Folder Name"
        / "Season 2026"
        / "2026-11-08 - Video After Rename [vid_renamed_1].mp4"
    )
    dummy_video_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_video_path.write_bytes(b"dummy mp4")

    mock_downloader.async_download = AsyncMock(
        return_value=DownloadResult(
            id="vid_renamed_1",
            metadata=mock_downloader.async_extract_info.return_value,
            video_path=dummy_video_path,
            thumbnail_path=None,
            subtitle_paths=[],
            file_size=len(b"dummy mp4"),
        )
    )

    worker = BackgroundWorker(
        queue=queue,
        downloader_service=mock_downloader,
        layout=layout,
        nfo=nfo,
        database=temp_db,
    )

    job = queue.enqueue(
        "download_video",
        {
            "video_id": "vid_renamed_1",
            "channel_id": "UCrename123",
        },
    )
    assert job is not None

    processed = asyncio.run(worker.process_next_job())
    assert processed is True

    # Folder continuity: no 'New Channel Name' directory created
    new_dir = media_dir / "tech" / "New Channel Name"
    assert not new_dir.exists()

    # Old directory was used
    old_dir = media_dir / "tech" / "Old Folder Name"
    assert old_dir.exists()

    # Database updated channel name, preserved folder_name
    with temp_db.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, folder_name FROM channels WHERE id = 'UCrename123'")
        row = cursor.fetchone()
        assert row["name"] == "New Channel Name"
        assert row["folder_name"] == "Old Folder Name"


def test_subscription_poller_initial_download_limit_zero(
    queue: JobQueue, temp_db: Database
) -> None:
    """Test initial download limit=0 marks all existing feed videos as skipped."""
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("UClimit0", "Limit 0 Channel", "https://youtube.com/@limit0", "Limit 0", "default"),
        )

    feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
     <entry>
      <yt:videoId>v1</yt:videoId>
      <yt:channelId>UClimit0</yt:channelId>
      <title>Video 1</title>
      <published>2026-11-05T12:00:00Z</published>
     </entry>
     <entry>
      <yt:videoId>v2</yt:videoId>
      <yt:channelId>UClimit0</yt:channelId>
      <title>Video 2</title>
      <published>2026-11-06T12:00:00Z</published>
     </entry>
    </feed>
    """

    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=feed_xml))
    )
    poller = SubscriptionPoller(queue=queue, database=temp_db, client=client)

    enqueued = poller.poll_channel("UClimit0", limit=0, mark_skipped_below_limit=True)
    assert enqueued == []
    assert queue.claim_next_job() is None

    # Check skipped status in DB
    with temp_db.connection() as conn:
        rows = conn.execute(
            "SELECT id, status FROM videos WHERE channel_id = 'UClimit0'"
        ).fetchall()
        assert len(rows) == 2
        assert all(r["status"] == "skipped" for r in rows)

    # Subsequent polling does not enqueue them
    second_poll = poller.poll_channel("UClimit0")
    assert second_poll == []


def test_subscription_poller_initial_download_limit_n(queue: JobQueue, temp_db: Database) -> None:
    """Test initial download limit=N enqueues top N and skips the rest."""
    with temp_db.connection() as conn:
        conn.execute(
            """
            INSERT INTO channels (id, name, url, folder_name, library, auto_download)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            ("UClimitN", "Limit N Channel", "https://youtube.com/@limitN", "Limit N", "default"),
        )

    feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
     <entry>
      <yt:videoId>v_new</yt:videoId>
      <yt:channelId>UClimitN</yt:channelId>
      <title>Video New</title>
      <published>2026-11-07T12:00:00Z</published>
     </entry>
     <entry>
      <yt:videoId>v_old</yt:videoId>
      <yt:channelId>UClimitN</yt:channelId>
      <title>Video Old</title>
      <published>2026-11-05T12:00:00Z</published>
     </entry>
    </feed>
    """

    client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=feed_xml))
    )
    poller = SubscriptionPoller(queue=queue, database=temp_db, client=client)

    enqueued = poller.poll_channel("UClimitN", limit=1, mark_skipped_below_limit=True)
    assert enqueued == ["v_new"]

    job = queue.claim_next_job()
    assert job is not None
    assert job.payload["video_id"] == "v_new"

    # v_old should be skipped
    with temp_db.connection() as conn:
        row = conn.execute("SELECT status FROM videos WHERE id = 'v_old'").fetchone()
        assert row is not None
        assert row["status"] == "skipped"
