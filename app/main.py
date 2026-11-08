"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router
from app.db import db
from app.worker import BackgroundWorker

# Global background worker instance
worker = BackgroundWorker()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Run database migrations on startup
    db.run_migrations()
    # Ensure worker points to active db instance
    worker.db = db
    worker.queue.db = db
    # Start background job worker
    worker.start()
    try:
        yield
    finally:
        await worker.stop()


app = FastAPI(title="yt-to-jellyfin", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
