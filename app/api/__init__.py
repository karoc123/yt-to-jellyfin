"""Main API router aggregating all endpoints."""

from fastapi import APIRouter

from app.api.endpoints.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router)

__all__ = ["api_router"]
