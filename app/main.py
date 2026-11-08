"""FastAPI application entrypoint."""

from fastapi import FastAPI

app = FastAPI(title="yt-to-jellyfin", version="0.1.0")


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
