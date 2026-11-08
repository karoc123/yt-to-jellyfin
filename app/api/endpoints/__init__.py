"""API endpoints package."""

from app.api.endpoints.actions import router as actions_router
from app.api.endpoints.health import router as health_router
from app.api.endpoints.views import router as views_router

__all__ = ["actions_router", "health_router", "views_router"]
