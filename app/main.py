"""FastAPI application entrypoint."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Run database migrations on startup
    db.run_migrations()
    yield


app = FastAPI(title="yt-to-jellyfin", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
