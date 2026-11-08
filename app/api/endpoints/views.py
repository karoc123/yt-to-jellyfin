"""HTML Web Views for Quick Download, Mediathek Browser, and Subscriptions."""

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.endpoints.actions import channel_poster_path
from app.core.config import settings
from app.core.settings_service import settings_service
from app.db import get_db
from app.downloader.service import YtDlpService
from app.worker.models import Job

templates_dir = Path(__file__).parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def library_options() -> list[dict[str, str]]:
    """Libraries from database (or LIBRARIES override) with icon and label, read at render time."""
    libs = settings_service.get_libraries()
    return [{"name": item["name"], "icon": item["icon"], "label": item["label"]} for item in libs]


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
    "cookies_saved": ("status", "Cookies wurden erfolgreich gespeichert."),
    "cookies_deleted": ("status", "Gespeicherte Cookies wurden gelöscht."),
    "cookies_empty": ("alert", "Bitte eine Datei auswählen oder Cookie-Text einfügen."),
    "cookies_invalid": (
        "alert",
        "Ungültiges Cookie-Format. Erwartet wird ein Netscape HTTP Cookie File.",
    ),
    "library_added": ("status", "Bibliothek wurde erfolgreich angelegt."),
    "library_deleted": ("status", "Bibliothek wurde gelöscht."),
    "library_delete_forbidden": (
        "alert",
        "Bibliothek kann nicht gelöscht werden (enthält noch Kanäle oder ist 'default').",
    ),
    "library_invalid": ("alert", "Ungültiger Bibliotheksname oder -label."),
    "retention_saved": ("status", "Aufbewahrungsregeln wurden gespeichert."),
    "retention_triggered": ("status", "Aufbewahrungsbereinigung wurde gestartet."),
    "download_settings_saved": ("status", "Download- und Qualitätseinstellungen gespeichert."),
    "filter_settings_saved": ("status", "Filter- und SponsorBlock-Einstellungen gespeichert."),
    "poller_triggered": ("status", "Suche nach neuen Videos wurde gestartet."),
    "schedule_saved": ("status", "Such-Intervall wurde gespeichert."),
    "jellyfin_saved": ("status", "Jellyfin-Einstellungen wurden gespeichert."),
    "cookie_test_ok": ("status", "{info}"),
    "cookie_test_err": ("alert", "{info}"),
    "jellyfin_test_ok": ("status", "{info}"),
    "jellyfin_test_err": ("alert", "{info}"),
}


def flash_message(request: Request) -> dict[str, str] | None:
    """Flash notice for the current request's `msg`/`n`/`info` query parameters, if any."""
    entry = FLASH_MESSAGES.get(request.query_params.get("msg", ""))
    if not entry:
        return None
    role, text = entry
    count = request.query_params.get("n", "")
    info = request.query_params.get("info", "")
    try:
        formatted = text.format(n=int(count) if count.isdigit() else 0, info=info)
    except Exception:
        formatted = text
    return {"role": role, "text": formatted}


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


@router.get("/settings", response_class=HTMLResponse)
async def view_settings(
    request: Request,
    lib: str | None = None,
) -> HTMLResponse:
    """Settings view for libraries, YouTube cookies, download quality, and retention."""
    cookies_path = Path(settings.cookies_path)
    cookies_saved = cookies_path.is_file()
    cookies_valid = YtDlpService.validate_cookies_file(cookies_path) if cookies_saved else False

    libraries_list = settings_service.get_libraries()
    all_settings = settings_service.get_all_settings()

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "current_lib": lib or "all",
            "cookies_saved": cookies_saved,
            "cookies_valid": cookies_valid,
            "cookies_path": str(settings.cookies_path),
            "media_dir": str(settings.media_dir),
            "database_path": str(settings.database_path),
            "libraries": libraries_list,
            "env_libraries_active": os.environ.get("LIBRARIES") is not None,
            "retention_days": all_settings.get(
                "retention_days",
                str(settings.retention_days) if settings.retention_days is not None else "",
            ),
            "retention_count": all_settings.get(
                "retention_count",
                str(settings.retention_count) if settings.retention_count is not None else "",
            ),
            "default_max_height": all_settings.get(
                "default_max_height",
                str(settings.default_max_height) if settings.default_max_height is not None else "",
            ),
            "format_sort": all_settings.get(
                "format_sort",
                str(settings.format_sort) if settings.format_sort is not None else "",
            ),
            "subtitles_langs": all_settings.get("subtitles_langs", "de,en"),
            "throttled_rate_bytes": all_settings.get(
                "throttled_rate_bytes",
                str(settings.throttled_rate_bytes)
                if settings.throttled_rate_bytes is not None
                else "",
            ),
            "ignore_shorts": settings_service.get_setting_bool("ignore_shorts", default=True),
            "ignore_live": settings_service.get_setting_bool("ignore_live", default=True),
            "sponsorblock_enabled": settings_service.get_setting_bool(
                "sponsorblock_enabled", default=True
            ),
            "sponsorblock_categories": all_settings.get("sponsorblock_categories", "sponsor,intro"),
            "rss_poll_interval": settings_service.get_setting_int(
                "rss_poll_interval", settings.rss_poll_interval_seconds
            ),
            "jellyfin_url": all_settings.get("jellyfin_url", ""),
            "jellyfin_api_key": all_settings.get("jellyfin_api_key", ""),
        },
    )
