"""Retention evaluation and video lifecycle manager."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.db.database import Database, db
from app.retention.deletion import delete_video_artifacts

logger = logging.getLogger(__name__)


@dataclass
class RetentionConfig:
    """Global or per-channel retention rules."""

    max_days: int | None = None  # TTL: delete videos older than X days
    max_count: int | None = None  # Keep latest N videos per channel


@dataclass
class DeletionResult:
    """Summary of a video deletion run."""

    video_id: str
    channel_id: str
    title: str
    deleted_files: list[str]
    success: bool
    reason: str = ""


class RetentionEngine:
    """Evaluates retention rules (TTL, episode count) and manages video pinning."""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database if database is not None else db

    def set_pin_status(self, video_id: str, is_pinned: bool) -> bool:
        """Pin or unpin a video to protect it from automatic retention deletion.

        Returns True if the video record was found and updated.
        """
        pinned_int = 1 if is_pinned else 0
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE videos
                SET is_pinned = ?, updated_at = ?
                WHERE id = ?
                """,
                (pinned_int, now, video_id),
            )
            return cursor.rowcount > 0

    def delete_video(self, video_id: str, force: bool = False) -> DeletionResult:
        """Delete a video's files and keep its row as tombstone (`status='deleted'`).

        The tombstone stops the RSS poller from downloading the video again.
        If video is pinned and force=False, deletion will be refused.
        """
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, channel_id, title, file_path, nfo_path, thumbnail_path, is_pinned
                FROM videos WHERE id = ? AND status != 'deleted'
                """,
                (video_id,),
            )
            row = cursor.fetchone()
            if not row:
                return DeletionResult(
                    video_id=video_id,
                    channel_id="",
                    title="",
                    deleted_files=[],
                    success=False,
                    reason="Video not found in database",
                )

            is_pinned = bool(row["is_pinned"])
            if is_pinned and not force:
                logger.info("Skipping deletion of pinned video %s (%s)", row["title"], video_id)
                return DeletionResult(
                    video_id=video_id,
                    channel_id=row["channel_id"],
                    title=row["title"],
                    deleted_files=[],
                    success=False,
                    reason="Video is pinned and force=False",
                )

            # 1. Clean up filesystem artifacts
            deleted_paths = delete_video_artifacts(
                video_path=row["file_path"],
                nfo_path=row["nfo_path"],
                thumbnail_path=row["thumbnail_path"],
            )

            # 2. Keep the row as tombstone without file references
            cursor.execute(
                """
                UPDATE videos
                SET status = 'deleted', file_path = NULL, nfo_path = NULL,
                    thumbnail_path = NULL, file_size = 0, updated_at = ?
                WHERE id = ?
                """,
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), video_id),
            )

            logger.info(
                "Successfully deleted video %s and %d artifact(s)",
                video_id,
                len(deleted_paths),
            )
            return DeletionResult(
                video_id=video_id,
                channel_id=row["channel_id"],
                title=row["title"],
                deleted_files=[str(p) for p in deleted_paths],
                success=True,
                reason="Deleted successfully",
            )

    def evaluate_channel_retention(
        self,
        channel_id: str,
        global_config: RetentionConfig | None = None,
    ) -> list[DeletionResult]:
        """Evaluate retention rules for a single channel and delete expired unpinned videos."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            # Fetch channel retention overrides
            cursor.execute(
                """
                SELECT id, retention_days, retention_count
                FROM channels WHERE id = ?
                """,
                (channel_id,),
            )
            ch = cursor.fetchone()
            if not ch:
                return []

            # Per-channel rules take precedence over global defaults
            ttl_days = (
                ch["retention_days"]
                if ch["retention_days"] is not None
                else (global_config.max_days if global_config else None)
            )
            max_count = (
                ch["retention_count"]
                if ch["retention_count"] is not None
                else (global_config.max_count if global_config else None)
            )

            if (ttl_days is None or ttl_days <= 0) and (max_count is None or max_count <= 0):
                # No retention policy configured for this channel
                return []

            # Fetch all unpinned videos for this channel, ordered newest to oldest
            cursor.execute(
                """
                SELECT id, title, published_at, is_pinned
                FROM videos
                WHERE channel_id = ? AND is_pinned = 0 AND status = 'completed'
                ORDER BY published_at DESC, created_at DESC
                """,
                (channel_id,),
            )
            videos = cursor.fetchall()

        to_delete_ids: set[str] = set()
        now = datetime.now(UTC)

        # 1. TTL check (older than X days)
        if ttl_days is not None and ttl_days > 0:
            for v in videos:
                pub_str = v["published_at"]
                try:
                    # Support ISO 8601 strings and YYYY-MM-DD
                    if "T" in pub_str:
                        pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                    else:
                        pub_dt = datetime.fromisoformat(pub_str).replace(tzinfo=UTC)

                    age_days = (now - pub_dt).total_seconds() / 86400.0
                    if age_days > ttl_days:
                        to_delete_ids.add(v["id"])
                except Exception as exc:
                    logger.warning("Could not parse published_at for video %s: %s", v["id"], exc)

        # 2. Max count check (keep latest N videos)
        if max_count is not None and max_count > 0:
            # Remaining candidate videos that haven't already been marked by TTL
            for idx, v in enumerate(videos):
                if idx >= max_count:
                    to_delete_ids.add(v["id"])

        results: list[DeletionResult] = []
        for vid in to_delete_ids:
            res = self.delete_video(vid, force=False)
            results.append(res)

        return results

    def evaluate_all_channels(
        self,
        global_config: RetentionConfig | None = None,
    ) -> dict[str, list[DeletionResult]]:
        """Evaluate retention for all channels."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM channels")
            channel_ids = [row["id"] for row in cursor.fetchall()]

        all_results: dict[str, list[DeletionResult]] = {}
        for cid in channel_ids:
            res = self.evaluate_channel_retention(cid, global_config=global_config)
            if res:
                all_results[cid] = res
        return all_results
