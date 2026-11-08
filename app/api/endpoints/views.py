"""HTML Web Views for Quick Download, Mediathek Browser, and Subscriptions."""

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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


def filesize(num_bytes: int | None) -> str:
    """Human readable size with binary steps, e.g. "350 MB" or "1.2 GB"."""
    size = float(num_bytes or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    step = 0
    while size >= 1024 and step < len(units) - 1:
        size /= 1024
        step += 1
    unit = units[step]
    if unit == "B" or size >= 10:
        return f"{size:.0f} {unit}"
    return f"{size:.1f} {unit}"


templates.env.filters["filesize"] = filesize

BROWSER_PAGE_SIZE = 24


def _like_pattern(term: str) -> str:
    """`%term%` for `LIKE ... ESCAPE '\\'` with the term's own wildcards escaped."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# Flash messages selected by `?msg=<key>[&n=<count>]` after a redirect. Only these fixed
# texts are rendered (never text from the URL), `{n}` is an integer.
FLASH_MESSAGES = {
    "download_queued": ("status", "Download wurde in die Warteschlange gestellt."),
    "job_retried": ("status", "Auftrag wird erneut versucht."),
    "job_cancelled": ("status", "Auftrag wurde abgebrochen."),
    "job_not_retryable": ("alert", "Nur fehlgeschlagene oder abgebrochene Aufträge."),
    "job_not_cancellable": ("alert", "Laufende Aufträge können nicht abgebrochen werden."),
    "video_pinned": ("status", "Video ist angepinnt und vor dem Aufräumen geschützt."),
    "video_unpinned": ("status", "Video ist nicht mehr angepinnt."),
    "video_deleted": ("status", "Video wurde gelöscht."),
    "video_delete_failed": ("alert", "Video konnte nicht gelöscht werden."),
    "sub_added": ("status", "Kanal wurde abonniert."),
    "sub_failed": ("alert", "Kanal wurde nicht gefunden. Bitte URL, @Handle oder ID prüfen."),
    "sub_updated": ("status", "Abo wurde gespeichert."),
    "sub_deleted": ("status", "Abo wurde beendet."),
    "synced": ("status", "Synchronisiert: {n} neue(s) Video(s) in der Warteschlange."),
    "imported": ("status", "Import abgeschlossen: {n} Kanal/Kanäle importiert."),
    "import_missing": ("alert", "Bitte eine Datei oder TubeArchivist-URL und Token angeben."),
    "ta_failed": ("alert", "TubeArchivist nicht erreichbar oder Token ungültig."),
}


def flash_message(request: Request) -> dict[str, str] | None:
    """Flash notice for the current request's `msg`/`n` query parameters, if any."""
    entry = FLASH_MESSAGES.get(request.query_params.get("msg", ""))
    if not entry:
        return None
    role, text = entry
    count = request.query_params.get("n", "")
    return {"role": role, "text": text.format(n=int(count) if count.isdigit() else 0)}


templates.env.globals["flash_message"] = flash_message

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
    q: str = "",
    page: int = 1,
    db_conn=Depends(get_db),
) -> HTMLResponse:
    """Two-tier library browser: channel grid or a channel's (searchable, paged) videos."""
    cursor = db_conn.cursor()

    active_channel = None
    videos: list[dict] = []
    channels: list[dict] = []
    pagination: dict[str, Any] = {}
    q = q.strip()

    if channel_id:
        cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        active_channel = cursor.fetchone()

        where = "WHERE channel_id = ? AND status = 'completed'"
        params: list[Any] = [channel_id]
        if q:
            where += " AND title LIKE ? ESCAPE '\\'"
            params.append(_like_pattern(q))
        total = cursor.execute(f"SELECT COUNT(*) FROM videos {where}", params).fetchone()[0]
        pages = max(1, -(-total // BROWSER_PAGE_SIZE))
        page = min(max(page, 1), pages)
        cursor.execute(
            f"""
            SELECT * FROM videos {where}
            ORDER BY published_at DESC, created_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, BROWSER_PAGE_SIZE, (page - 1) * BROWSER_PAGE_SIZE],
        )
        videos = [dict(r) for r in cursor.fetchall()]

        def page_url(number: int) -> str:
            query: dict[str, Any] = {"channel_id": channel_id}
            if lib and lib != "all":
                query["lib"] = lib
            if q:
                query["q"] = q
            if number > 1:
                query["page"] = number
            return f"/browser?{urlencode(query)}"

        pagination = {
            "page": page,
            "pages": pages,
            "total": total,
            "prev_url": page_url(page - 1) if page > 1 else None,
            "next_url": page_url(page + 1) if page < pages else None,
        }
    else:
        # Channel grid with count and total size of completed videos
        query = """
            SELECT c.*, COUNT(v.id) AS video_count, COALESCE(SUM(v.file_size), 0) AS total_size
            FROM channels c
            LEFT JOIN videos v ON v.channel_id = c.id AND v.status = 'completed'
        """
        params = []
        if lib and lib != "all":
            query += " WHERE c.library = ?"
            params.append(lib)
        query += " GROUP BY c.id ORDER BY c.name ASC"
        cursor.execute(query, params)
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
            "q": q,
            "pagination": pagination,
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
