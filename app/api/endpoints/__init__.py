"""API endpoints package."""

from app.api.endpoints.health import router as health_router

__all__ = ["health_router"]
