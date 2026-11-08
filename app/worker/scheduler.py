"""Periodic scheduler: enqueues RSS polling and retention jobs for the background worker."""

import asyncio
import logging

from app.core.config import settings
from app.worker.queue import JobQueue

logger = logging.getLogger(__name__)


class Scheduler:
    """Every `interval_seconds`, enqueue one `poll_channels` and one `retention` job."""

    def __init__(
        self,
        queue: JobQueue | None = None,
        interval_seconds: float = settings.rss_poll_interval_seconds,
    ) -> None:
        self.queue = queue or JobQueue()
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    def tick(self) -> None:
        """Enqueue periodic jobs; at most one pending/running job of each type."""
        for job_type in ("poll_channels", "retention"):
            self.queue.enqueue(job_type, {}, unique=True)

    def start(self) -> None:
        """Start the scheduler loop as an asyncio task (first tick after one interval)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Scheduler started with interval=%ss", self.interval_seconds)

    async def stop(self) -> None:
        """Cancel the scheduler loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:
                logger.error("Scheduler tick failed: %s", exc)
