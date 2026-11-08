"""Health check and service status endpoints."""

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Return basic liveness status."""
    return {"status": "ok"}
