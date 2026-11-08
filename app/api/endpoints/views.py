"""HTML Web Views for Quick Download, Mediathek Browser, and Subscriptions."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.endpoints.actions import channel_poster_path
from app.core.config import settings
from app.db import get_db
from app.worker.models import Job

templates_dir = Path(__file__).parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Display labels for the default libraries; other names are shown capitalized
_LIBRARY_LABELS = {
    "default": ("📁", "Standard"),
    "kids": ("🧒", "Kids"),
    "dokus": ("📚", "Dokus"),
    "tech": ("💻", "Tech"),
}


def library_options() -> list[dict[str, str]]:
    """Libraries from settings (LIBRARIES) with icon and label, read at render time."""
    options = []
    for name in settings.libraries:
        icon, label = _LIBRARY_LABELS.get(name, ("📂", name.capitalize()))
        options.append({"name": name, "icon": icon, "label": label})
    return options


templates.env.globals["library_options"] = library_options

router = APIRouter(tags=["views"])


# Internal maintenance jobs are not shown in the download queue
_INTERNAL_JOB_TYPES = ("poll_channels", "retention")

JOB_STATUS_LABELS = {
    "pending": "Wartend",
    "running": "Läuft",
    "completed": "Fertig",
    "failed": "Fehlgeschlagen",
    "cancelled": "Abgebrochen",
}
templates.env.globals["job_status_labels"] = JOB_STATUS_LABELS


def _queue_context(db_conn: Any, lib: str | None) -> dict[str, Any]:
    """Recent user-facing jobs (optionally filtered by library) for the queue partial."""
    query = f"SELECT * FROM jobs WHERE type NOT IN ({', '.join('?' * len(_INTERNAL_JOB_TYPES))})"
    params: list[Any] = list(_INTERNAL_JOB_TYPES)
    if lib and lib != "all":
        query += " AND COALESCE(json_extract(payload, '$.library'), 'default') = ?"
        params.append(lib)
    query += " ORDER BY created_at DESC LIMIT 20"
    jobs = [Job.from_row(r) for r in db_conn.execute(query, params).fetchall()]
    return {
        "current_lib": lib or "all",
        "jobs": jobs,
        "queue_active": any(job.status in ("pending", "running") for job in jobs),
    }


@router.get("/", response_class=HTMLResponse)
async def view_home(
    request: Request,
    lib: str | None = None,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Quick Download and active queue view."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"active_page": "home", **_queue_context(db_conn, lib)},
    )


@router.get("/partials/queue", response_class=HTMLResponse)
async def view_queue_partial(
    request: Request,
    lib: str | None = None,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Queue fragment for HTMX polling and swaps after retry/cancel."""
    return templates.TemplateResponse(
        request=request, name="components/queue.html", context=_queue_context(db_conn, lib)
    )


@router.get("/browser", response_class=HTMLResponse)
async def view_browser(
    request: Request,
    channel_id: str | None = None,
    lib: str | None = None,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Two-tier library browser: channel grid or channel's video grid."""
    cursor = db_conn.cursor()

    active_channel = None
    videos: list[dict] = []
    channels: list[dict] = []

    if channel_id:
        cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        active_channel = cursor.fetchone()

        cursor.execute(
            """
            SELECT * FROM videos
            WHERE channel_id = ? AND status = 'completed'
            ORDER BY published_at DESC, created_at DESC
            """,
            (channel_id,),
        )
        videos = [dict(r) for r in cursor.fetchall()]
    else:
        # Channel Grid with video count
        if lib and lib != "all":
            cursor.execute(
                """
                SELECT c.*, COUNT(v.id) AS video_count
                FROM channels c
                LEFT JOIN videos v ON v.channel_id = c.id AND v.status = 'completed'
                WHERE c.library = ?
                GROUP BY c.id
                ORDER BY c.name ASC
                """,
                (lib,),
            )
        else:
            cursor.execute(
                """
                SELECT c.*, COUNT(v.id) AS video_count
                FROM channels c
                LEFT JOIN videos v ON v.channel_id = c.id AND v.status = 'completed'
                GROUP BY c.id
                ORDER BY c.name ASC
                """
            )
        channels = [dict(r) for r in cursor.fetchall()]
        for ch in channels:
            ch["has_poster"] = channel_poster_path(ch) is not None

    return templates.TemplateResponse(
        request=request,
        name="browser.html",
        context={
            "active_page": "browser",
            "current_lib": lib or "all",
            "active_channel": dict(active_channel) if active_channel else None,
            "videos": videos,
            "channels": channels,
        },
    )


@router.get("/subscriptions", response_class=HTMLResponse)
async def view_subscriptions(
    request: Request,
    lib: str | None = None,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Channel subscriptions and queue management view."""
    cursor = db_conn.cursor()
    if lib and lib != "all":
        cursor.execute(
            "SELECT * FROM channels WHERE library = ? ORDER BY name ASC",
            (lib,),
        )
    else:
        cursor.execute("SELECT * FROM channels ORDER BY name ASC")

    channels = [dict(r) for r in cursor.fetchall()]

    return templates.TemplateResponse(
        request=request,
        name="subscriptions.html",
        context={
            "active_page": "subscriptions",
            "current_lib": lib or "all",
            "channels": channels,
        },
    )
