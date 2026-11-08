"""HTML Web Views for Quick Download, Mediathek Browser, and Subscriptions."""

from pathlib import Path

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


@router.get("/", response_class=HTMLResponse)
async def view_home(
    request: Request,
    lib: str | None = None,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Quick Download and active queue view."""
    # Fetch recent jobs
    if lib and lib != "all":
        rows = db_conn.execute(
            """
            SELECT * FROM jobs
            WHERE COALESCE(json_extract(payload, '$.library'), 'default') = ?
            ORDER BY created_at DESC LIMIT 20
            """,
            (lib,),
        ).fetchall()
    else:
        rows = db_conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20").fetchall()
    jobs = [Job.from_row(r) for r in rows]

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
