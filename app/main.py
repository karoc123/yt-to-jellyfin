"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.db import db
from app.worker import BackgroundWorker
from app.worker.scheduler import Scheduler

# Global background worker and periodic scheduler (RSS poll + retention)
worker = BackgroundWorker()
scheduler = Scheduler(queue=worker.queue)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Run database migrations on startup
    db.run_migrations()
    # Ensure worker points to active db instance
    worker.db = db
    worker.queue.db = db
    scheduler.queue.db = db
    # Start background job worker and scheduler
    worker.start()
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        await worker.stop()


app = FastAPI(title="yt-to-jellyfin", version="0.1.0", lifespan=lifespan)

# Mount static assets
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(api_router)
