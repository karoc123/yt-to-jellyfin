"""HTML Web Views for Quick Download, Mediathek Browser, and Subscriptions."""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.worker.queue import JobQueue

templates_dir = Path(__file__).parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

router = APIRouter(tags=["views"])


@router.get("/", response_class=HTMLResponse)
async def view_home(
    request: Request,
    lib: str | None = None,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Quick Download and active queue view."""
    queue = JobQueue()
    # Fetch recent jobs
    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT * FROM jobs
        ORDER BY created_at DESC LIMIT 20
        """
    )
    raw_jobs = cursor.fetchall()
    jobs = [queue.get_job(r["id"]) for r in raw_jobs]
    jobs = [j for j in jobs if j is not None]

    if lib and lib != "all":
        jobs = [j for j in jobs if j.payload.get("library", "default") == lib]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "active_page": "home",
            "current_lib": lib or "all",
            "jobs": jobs,
        },
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
            WHERE channel_id = ?
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
                LEFT JOIN videos v ON v.channel_id = c.id
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
                LEFT JOIN videos v ON v.channel_id = c.id
                GROUP BY c.id
                ORDER BY c.name ASC
                """
            )
        channels = [dict(r) for r in cursor.fetchall()]

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
