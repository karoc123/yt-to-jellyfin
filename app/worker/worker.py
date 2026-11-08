"""Background worker processing SQLite jobs queue."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.settings_service import notify_jellyfin_refresh, settings_service
from app.db.database import Database, db
from app.downloader.exceptions import VideoUnavailableError
from app.downloader.options import DownloadOptions, ProgressUpdate
from app.downloader.service import YtDlpService, downloader, wrap_progress_hook
from app.jellyfin import JellyfinLayout, NfoGenerator, jellyfin_layout, nfo_generator
from app.jellyfin.episodes import assign_episode
from app.retention.engine import RetentionConfig, RetentionEngine
from app.worker.models import MAX_VIDEO_ATTEMPTS, Job, utc_now_str
from app.worker.poller import SubscriptionPoller
from app.worker.queue import JobQueue

logger = logging.getLogger(__name__)

# Minimum seconds between two progress writes of the same job
PROGRESS_WRITE_INTERVAL = 1.0

PLAYLIST_TYPES = ("playlist", "multi_video")
# Payload keys a playlist job hands down to its per-video jobs
INHERITED_PAYLOAD_KEYS = (
    "library",
    "max_height",
    "format",
    "format_selector",
    "format_sort",
    "sponsorblock_remove",
    "sponsorblock_mark",
)


class BackgroundWorker:
    """Consumes and executes jobs from the SQLite queue with crash recovery and progress."""

    def __init__(
        self,
        queue: JobQueue | None = None,
        downloader_service: YtDlpService = downloader,
        layout: JellyfinLayout = jellyfin_layout,
        nfo: NfoGenerator = nfo_generator,
        database: Database = db,
        poll_interval: float = 2.0,
    ) -> None:
        self.db = database
        self.queue = queue or JobQueue(self.db)
        self.downloader = downloader_service
        self.layout = layout
        self.nfo = nfo
        self.poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start worker in an asyncio background task."""
        if self._running:
            return
        # Crash recovery: reset stuck 'running' jobs to 'pending'
        self.queue.recover_crashed_jobs()
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("BackgroundWorker started with poll_interval=%.1fs", self.poll_interval)

    async def stop(self) -> None:
        """Gracefully stop the worker loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("BackgroundWorker stopped")

    async def _run_loop(self) -> None:
        """Continuous execution loop."""
        while self._running:
            try:
                processed = await self.process_next_job()
                if not processed:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected error in worker loop: %s", exc)
                await asyncio.sleep(self.poll_interval)

    async def process_next_job(self) -> bool:
        """Fetch and execute the next pending job. Returns True if a job was processed."""
        job = self.queue.claim_next_job()
        if not job:
            return False

        logger.info("Processing job %s (type: %s)", job.id, job.type)
        try:
            if job.type == "download_video":
                await self._handle_download_video(job)
            elif job.type == "poll_channels":
                poller = SubscriptionPoller(queue=self.queue, database=self.db)
                await asyncio.to_thread(poller.poll_all_channels)
            elif job.type == "retention":
                max_days = settings_service.get_setting_int(
                    "retention_days", settings.retention_days
                )
                max_count = settings_service.get_setting_int(
                    "retention_count", settings.retention_count
                )
                global_config = RetentionConfig(max_days=max_days, max_count=max_count)
                await asyncio.to_thread(
                    RetentionEngine(database=self.db).evaluate_all_channels, global_config
                )
            else:
                logger.warning("Unknown job type: %s", job.type)
                self.queue.mark_failed(job.id, f"Unknown job type: {job.type}")
                return True

            self.queue.mark_completed(job.id)
            logger.info("Completed job %s successfully", job.id)
            return True
        except Exception as exc:
            logger.error("Job %s failed: %s", job.id, exc)
            self.queue.mark_failed(job.id, str(exc))
            if job.type == "download_video":
                try:
                    self._record_video_failure(job, exc)
                except Exception as record_exc:
                    logger.error("Could not record failure for job %s: %s", job.id, record_exc)
            return True

    def _record_video_failure(self, job: Job, exc: Exception) -> None:
        """Mark the video row as failed so the poller does not retry it every cycle.

        `VideoUnavailableError` is permanent (no automatic retry). Other errors get an
        exponential backoff (1h, 4h, ...) until MAX_VIDEO_ATTEMPTS is reached.
        `next_attempt_at = NULL` means no automatic retry.
        """
        payload = job.payload
        video_id = payload.get("video_id")
        if not video_id:
            return  # e.g. quick download by URL: no ID known, nothing to track

        now = datetime.now(UTC)
        now_str = utc_now_str()
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT status, attempts FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if row and row["status"] == "completed":
                return  # files of an earlier download are still in place

            attempts = (row["attempts"] if row else 0) + 1
            retry = not isinstance(exc, VideoUnavailableError) and attempts < MAX_VIDEO_ATTEMPTS
            next_attempt_at = (
                (now + timedelta(hours=4 ** (attempts - 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
                if retry
                else None
            )
            if row:
                conn.execute(
                    """
                    UPDATE videos
                    SET status = 'failed', attempts = ?, last_error = ?,
                        next_attempt_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (attempts, str(exc), next_attempt_at, now_str, video_id),
                )
            else:
                # Minimal row; only possible when the channel is known (FK)
                channel_id = payload.get("channel_id")
                conn.execute(
                    """
                    INSERT INTO videos (
                        id, channel_id, title, published_at, status,
                        attempts, last_error, next_attempt_at, created_at, updated_at
                    )
                    SELECT ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?
                    WHERE EXISTS (SELECT 1 FROM channels WHERE id = ?)
                    """,
                    (
                        video_id,
                        channel_id,
                        payload.get("title") or video_id,
                        payload.get("published_at") or now_str,
                        attempts,
                        str(exc),
                        next_attempt_at,
                        now_str,
                        now_str,
                        channel_id,
                    ),
                )

    def _enqueue_playlist_entries(self, job: Job, info: dict[str, Any]) -> int:
        """Enqueue one `download_video` job per (flat) playlist entry; returns the count.

        Entries already downloaded (`completed`) or already queued are skipped.
        """
        inherited = {k: job.payload[k] for k in INHERITED_PAYLOAD_KEYS if k in job.payload}
        seen: set[str] = set()
        count = 0
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("ie_key") == "YoutubeTab":
                continue  # nested playlists / channel tabs are not fanned out
            video_id = str(entry.get("id") or "")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            with self.db.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM videos WHERE id = ? AND status = 'completed'", (video_id,)
                ).fetchone()
            if row:
                continue

            entry_url = str(entry.get("url") or "")
            if not entry_url.startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={video_id}"
            payload: dict[str, Any] = {**inherited, "video_id": video_id, "url": entry_url}
            if entry.get("title"):
                payload["title"] = entry["title"]
            if entry.get("channel_id"):
                payload["channel_id"] = entry["channel_id"]
            channel_name = entry.get("channel") or entry.get("uploader")
            if channel_name:
                payload["channel_name"] = channel_name
            if self.queue.enqueue("download_video", payload, deduplicate_key="video_id"):
                count += 1
        return count

    async def _fetch_channel_artwork(
        self, channel_url: str, channel_dir: Path, options: DownloadOptions
    ) -> str:
        """Fetch poster.jpg/fanart.jpg; returns the channel description. Never raises."""
        try:
            info = await asyncio.to_thread(
                self.downloader.fetch_channel_artwork, channel_url, channel_dir, options
            )
        except Exception as exc:
            logger.warning("Channel artwork for %s failed: %s", channel_url, exc)
            return ""
        return str(info.get("description") or "") if isinstance(info, dict) else ""

    async def _handle_download_video(self, job: Job) -> None:
        """Execute video download, layout organization, NFO writing, and DB record creation."""
        payload = job.payload
        url = payload.get("url") or f"https://www.youtube.com/watch?v={payload.get('video_id')}"
        library = payload.get("library") or "default"

        # Read format, subtitle and SponsorBlock settings from payload,
        # falling back to DB/env settings
        format_selector = (
            payload.get("format")
            or payload.get("format_selector")
            or settings_service.get_setting("default_format")
            or settings.default_format
        )
        format_sort = (
            payload.get("format_sort")
            or settings_service.get_setting("format_sort")
            or settings.format_sort
        )
        max_height = (
            payload.get("max_height")
            or settings_service.get_setting_int("default_max_height")
            or settings.default_max_height
        )
        throttled_rate = (
            payload.get("throttled_rate_bytes")
            or payload.get("throttled_rate")
            or settings_service.get_setting_int("throttled_rate_bytes")
            or settings.throttled_rate_bytes
        )
        subtitles_langs_raw = payload.get("subtitles_langs") or settings_service.get_setting(
            "subtitles_langs"
        )
        sponsorblock_remove = payload.get("sponsorblock_remove", [])
        sponsorblock_mark = payload.get("sponsorblock_mark", [])

        # Download options independent of the target path (Payload > Settings)
        opts_kwargs: dict[str, Any] = {
            "max_height": max_height,
            "sponsorblock_remove": sponsorblock_remove,
            "sponsorblock_mark": sponsorblock_mark,
        }
        if subtitles_langs_raw:
            opts_kwargs["subtitles_langs"] = [
                s.strip() for s in subtitles_langs_raw.split(",") if s.strip()
            ]
        if format_selector:
            opts_kwargs["format_selector"] = format_selector
        if format_sort:
            opts_kwargs["format_sort"] = format_sort
        if throttled_rate:
            opts_kwargs["throttled_rate_bytes"] = int(throttled_rate)
        # Cookies only when the file exists (no warning spam when none are configured)
        if settings.cookies_path and Path(settings.cookies_path).is_file():
            opts_kwargs["cookies_path"] = settings.cookies_path

        # Step 1: Extract metadata once (channel + publish date/year for Season directory)
        meta = await self.downloader.async_extract_info(url, DownloadOptions(**opts_kwargs))
        if meta.raw_info.get("_type") in PLAYLIST_TYPES or "entries" in meta.raw_info:
            count = self._enqueue_playlist_entries(job, meta.raw_info)
            logger.info("Playlist job %s fanned out into %d video job(s)", job.id, count)
            return

        channel_id = payload.get("channel_id") or meta.channel_id or "unknown_channel"
        channel_name = payload.get("channel_name") or meta.channel or "Unknown Channel"
        pub_date = meta.upload_date or payload.get("published_at", "") or ""
        year = pub_date[:4] if len(pub_date) >= 4 and pub_date[:4].isdigit() else "Unknown"

        # Step 2: Ensure channel folder, artwork (first download) and tvshow.nfo
        channel_dir = self.layout.ensure_channel_structure(channel_name, library=library)
        channel_plot = ""
        if not self.layout.get_channel_poster_path(channel_name, library=library).exists():
            channel_url = (
                f"https://www.youtube.com/channel/{channel_id}"
                if channel_id.startswith("UC")
                else meta.channel_url
            )
            channel_plot = await self._fetch_channel_artwork(
                channel_url, channel_dir, DownloadOptions(**opts_kwargs)
            )
        ch_nfo_path = self.layout.get_channel_nfo_path(channel_name, library=library)
        if not ch_nfo_path.exists():
            self.nfo.write_tvshow_nfo(
                dest_path=ch_nfo_path,
                title=channel_name,
                plot=channel_plot,
                channel_id=channel_id,
            )

        season_dir = self.layout.ensure_season_structure(channel_name, year, library=library)
        video_filename = self.layout.get_video_filename(
            date_str=pub_date,
            title=meta.title,
            video_id=meta.id,
            extension="mp4",
        )

        # Step 3: Target path into the download options
        dl_options = DownloadOptions(
            output_dir=season_dir,
            # Literal stem: escape % so yt-dlp does not interpolate titles like "100%"
            output_template=f"{Path(video_filename).stem.replace('%', '%%')}.%(ext)s",
            **opts_kwargs,
        )

        # Progress reporting (runs in the download thread); throttled DB writes
        last_written: dict[str, float] = {}

        def _on_progress(update: ProgressUpdate) -> None:
            if update.status != "downloading":
                return
            pct = int(update.percentage)
            now_ts = time.monotonic()
            if last_written and (
                abs(pct - last_written["pct"]) < 1
                or now_ts - last_written["ts"] < PROGRESS_WRITE_INTERVAL
            ):
                return
            last_written.update(pct=pct, ts=now_ts)
            try:
                self.queue.update_progress(job.id, pct)
            except Exception as exc:
                logger.debug("Could not write progress for job %s: %s", job.id, exc)

        # Step 4: Execute download, reusing the extracted info (no second extraction)
        download_kwargs: dict[str, Any] = {
            "url": url,
            "options": dl_options,
            "progress_hook": wrap_progress_hook(_on_progress),
        }
        if meta.raw_info:
            download_kwargs["info"] = meta.raw_info
        result = await self.downloader.async_download(**download_kwargs)

        # Step 5: Write episode .nfo with a stable episode number (MMDD + daily counter)
        now = utc_now_str()
        published_at = meta.upload_date or now
        with self.db.connection() as conn:
            episode = assign_episode(conn, result.id, channel_id, published_at)
        ep_nfo_path = self.layout.get_episode_nfo_path(result.video_path)
        self.nfo.write_episode_nfo(
            dest_path=ep_nfo_path,
            title=meta.title,
            plot=meta.description,
            aired=meta.upload_date,
            season=int(year) if year.isdigit() else 1,
            episode=episode,
            duration_seconds=meta.duration,
            video_id=meta.id,
            channel_name=channel_name,
            webpage_url=meta.webpage_url,
            thumbnail_url=meta.thumbnail_url,
            tags=meta.tags,
        )

        # Step 6: Upsert video record in database
        with self.db.connection() as conn:
            # Check or create channel if not present
            conn.execute(
                """
                INSERT OR IGNORE INTO channels (
                    id, name, url, folder_name, library, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    channel_name,
                    meta.channel_url or f"https://www.youtube.com/channel/{channel_id}",
                    channel_name,
                    library,
                    now,
                    now,
                ),
            )

            # Upsert video record (keeps is_pinned and created_at on re-download)
            conn.execute(
                """
                INSERT INTO videos (
                    id, channel_id, title, description, published_at, duration,
                    file_path, nfo_path, thumbnail_path, file_size, episode, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    title = excluded.title,
                    description = excluded.description,
                    published_at = excluded.published_at,
                    duration = excluded.duration,
                    file_path = excluded.file_path,
                    nfo_path = excluded.nfo_path,
                    thumbnail_path = excluded.thumbnail_path,
                    file_size = excluded.file_size,
                    episode = COALESCE(videos.episode, excluded.episode),
                    status = excluded.status,
                    attempts = 0,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    result.id,
                    channel_id,
                    meta.title,
                    meta.description,
                    published_at,
                    meta.duration,
                    str(result.video_path),
                    str(ep_nfo_path),
                    str(result.thumbnail_path) if result.thumbnail_path else None,
                    result.file_size,
                    episode,
                    now,
                    now,
                ),
            )

        # Step 7: Trigger optional Jellyfin library refresh
        try:
            await notify_jellyfin_refresh(settings_service)
        except Exception as jf_exc:
            logger.debug("Jellyfin notification skipped or failed: %s", jf_exc)
