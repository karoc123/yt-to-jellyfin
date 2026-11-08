"""Queue and background job definitions."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now_str() -> str:
    """Return current ISO 8601 UTC timestamp."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    """Job record in SQLite queue."""

    id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending, running, completed, failed, cancelled
    progress: int = 0
    error_message: str | None = None
    created_at: str = field(default_factory=utc_now_str)
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def create(cls, job_type: str, payload: dict[str, Any] | None = None) -> "Job":
        """Instantiate a new pending job."""
        return cls(
            id=f"job_{uuid.uuid4().hex[:12]}",
            type=job_type,
            payload=payload or {},
            status="pending",
            progress=0,
            created_at=utc_now_str(),
        )

    def payload_json(self) -> str:
        """Serialize payload to JSON string."""
        return json.dumps(self.payload)

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        """Reconstruct Job instance from a SQLite row."""
        payload_data = {}
        if row["payload"]:
            try:
                payload_data = json.loads(row["payload"])
            except Exception:
                payload_data = {}
        return cls(
            id=row["id"],
            type=row["type"],
            payload=payload_data,
            status=row["status"],
            progress=int(row["progress"] or 0),
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
