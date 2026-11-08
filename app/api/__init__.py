"""Main API router aggregating all endpoints."""

from fastapi import APIRouter

from app.api.endpoints.actions import router as actions_router
from app.api.endpoints.health import router as health_router
from app.api.endpoints.views import router as views_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(actions_router)
api_router.include_router(views_router)

__all__ = ["api_router"]
