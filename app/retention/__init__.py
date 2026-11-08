"""Retention rules, video pinning, and cleanup engine."""

from app.retention.deletion import delete_video_artifacts
from app.retention.engine import DeletionResult, RetentionConfig, RetentionEngine

retention_engine = RetentionEngine()

__all__ = [
    "DeletionResult",
    "RetentionConfig",
    "RetentionEngine",
    "delete_video_artifacts",
    "retention_engine",
]
