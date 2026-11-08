"""SQLite-backed queue manager with atomic transitions, deduplication, and crash recovery."""

import logging
from typing import Any

from app.db.database import Database, db
from app.worker.models import Job, utc_now_str

logger = logging.getLogger(__name__)


class JobQueue:
    """Manages transactional job queue operations on top of SQLite."""

    def __init__(self, database: Database = db) -> None:
        self.db = database

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        deduplicate_key: str | None = None,
    ) -> Job | None:
        """Enqueue a job.

        If `deduplicate_key` (e.g. video ID or channel ID) is supplied and a job with
        matching type and deduplicate payload value is already pending or running, returns None.
        """
        job = Job.create(job_type, payload)

        with self.db.connection() as conn:
            cursor = conn.cursor()

            if deduplicate_key:
                val = str(payload.get(deduplicate_key, ""))
                if val:
                    # Check existing pending or running jobs
                    cursor.execute(
                        """
                        SELECT * FROM jobs
                        WHERE type = ? AND status IN ('pending', 'running')
                        """,
                        (job_type,),
                    )
                    for row in cursor.fetchall():
                        candidate = Job.from_row(row)
                        if str(candidate.payload.get(deduplicate_key, "")) == val:
                            logger.info(
                                "Skipping enqueue: Job of type %s for %s=%s already queued (%s)",
                                job_type,
                                deduplicate_key,
                                val,
                                candidate.id,
                            )
                            return None

            cursor.execute(
                """
                INSERT INTO jobs (id, type, payload, status, progress, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.type,
                    job.payload_json(),
                    job.status,
                    job.progress,
                    job.error_message,
                    job.created_at,
                ),
            )
            return job

    def claim_next_job(self) -> Job | None:
        """Atomically fetch and mark the oldest pending job as running."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if not row:
                return None

            job_id = row["id"]
            now = utc_now_str()
            cursor.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, job_id),
            )
            if cursor.rowcount == 0:
                return None

            cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            claimed_row = cursor.fetchone()
            return Job.from_row(claimed_row) if claimed_row else None

    def update_progress(self, job_id: str, progress: int) -> None:
        """Update job progress percentage (0-100)."""
        clamped = max(0, min(100, int(progress)))
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE jobs SET progress = ? WHERE id = ?",
                (clamped, job_id),
            )

    def mark_completed(self, job_id: str) -> None:
        """Mark job as successfully completed."""
        now = utc_now_str()
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'completed', progress = 100, completed_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )

    def mark_failed(self, job_id: str, error_message: str) -> None:
        """Mark job as failed with error details."""
        now = utc_now_str()
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', error_message = ?, completed_at = ?
                WHERE id = ?
                """,
                (str(error_message), now, job_id),
            )

    def recover_crashed_jobs(self) -> int:
        """Reset stuck 'running' jobs to 'pending' upon worker reboot."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE jobs
                SET status = 'pending', started_at = NULL, error_message = 'Recovered after crash'
                WHERE status = 'running'
                """
            )
            count = cursor.rowcount
            if count > 0:
                logger.warning("Recovered %d crashed job(s) back to pending", count)
            return count

    def get_job(self, job_id: str) -> Job | None:
        """Retrieve a specific job by id."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            return Job.from_row(row) if row else None

    def list_jobs(self, limit: int = 50, status: str | None = None) -> list[Job]:
        """List recent jobs with optional status filter."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    """
                    SELECT * FROM jobs WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                )
            else:
                cursor.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            return [Job.from_row(r) for r in cursor.fetchall()]
