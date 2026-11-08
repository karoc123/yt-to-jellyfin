"""HTTP endpoints for video actions, quick download enqueue, and media serving."""

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from app.core.config import settings
from app.db import db, get_db
from app.downloader.options import DownloadOptions
from app.downloader.service import downloader
from app.jellyfin import jellyfin_layout
from app.migration.subscriptions import SubscriptionsMigrator
from app.retention import retention_engine
from app.worker.poller import SubscriptionPoller
from app.worker.queue import JobQueue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["actions"])


class BulkDeleteRequest(BaseModel):
    video_ids: list[str]


class BulkPinRequest(BaseModel):
    video_ids: list[str]
    is_pinned: bool


@router.post("/downloads")
async def create_download_job(
    url: str = Form(...),
    library: str = Form("default"),
    target_library: str | None = Form(None),
    max_height: str | None = Form(None),
    sponsorblock: str | None = Form(None),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Enqueue a video/playlist download job."""
    queue = JobQueue()
    chosen_lib = target_library or library or "default"

    payload = {
        "url": url,
        "library": chosen_lib,
    }
    if max_height and max_height.strip().isdigit():
        payload["max_height"] = int(max_height)
    if sponsorblock:
        payload["sponsorblock_remove"] = ["sponsor", "intro"]

    queue.enqueue("download_video", payload)
    return RedirectResponse(f"/?lib={chosen_lib}", status_code=303)


@router.post("/videos/pin")
async def pin_video(
    video_id: str = Form(...),
    is_pinned: int = Form(...),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Toggle pin status on a single video."""
    retention_engine.set_pin_status(video_id, bool(is_pinned))
    return RedirectResponse("/browser", status_code=303)


@router.post("/videos/delete")
async def delete_single_video(
    video_id: str = Form(...),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Delete a video and all companion artifacts."""
    retention_engine.delete_video(video_id, force=True)
    return RedirectResponse("/browser", status_code=303)


@router.post("/videos/bulk-delete")
async def bulk_delete_videos(
    req: BulkDeleteRequest,
    db_conn=Depends(get_db),
) -> dict[str, int]:
    """Bulk delete selected videos."""
    deleted_count = 0
    for vid in req.video_ids:
        res = retention_engine.delete_video(vid, force=True)
        if res.success:
            deleted_count += 1
    return {"deleted": deleted_count}


@router.post("/videos/bulk-pin")
async def bulk_pin_videos(
    req: BulkPinRequest,
    db_conn=Depends(get_db),
) -> dict[str, int]:
    """Bulk pin or unpin selected videos."""
    updated_count = 0
    for vid in req.video_ids:
        if retention_engine.set_pin_status(vid, req.is_pinned):
            updated_count += 1
    return {"updated": updated_count}


@router.get("/media/thumb")
async def get_video_thumbnail(
    video_id: str,
    db_conn=Depends(get_db),
) -> FileResponse:
    """Serve local thumbnail image for a video."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT thumbnail_path FROM videos WHERE id = ?", (video_id,))
    row = cursor.fetchone()
    if not row or not row["thumbnail_path"]:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    path = Path(row["thumbnail_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(str(path))


def channel_poster_path(channel: dict) -> Path | None:
    """Existing poster.jpg of a channel folder (by name, then stored folder_name)."""
    library = channel.get("library") or "default"
    for folder in (channel.get("name"), channel.get("folder_name")):
        if folder:
            path = jellyfin_layout.get_channel_poster_path(folder, library=library)
            if path.is_file():
                return path
    return None


@router.get("/media/poster")
async def get_channel_poster(
    channel_id: str,
    db_conn=Depends(get_db),
) -> FileResponse:
    """Serve a channel's local poster.jpg."""
    row = db_conn.execute(
        "SELECT name, folder_name, library FROM channels WHERE id = ?", (channel_id,)
    ).fetchone()
    path = channel_poster_path(dict(row)) if row else None
    if not path:
        raise HTTPException(status_code=404, detail="Poster not found")
    return FileResponse(str(path))


def _positive_int_or_none(value: str | None) -> int | None:
    """Parse a retention form value; empty or 0 means unlimited (stored as NULL)."""
    value = (value or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


CHANNEL_ID_RE = re.compile(r"^UC[\w-]+$")
CHANNEL_URL_ID_RE = re.compile(r"/channel/(UC[\w-]+)")


def channel_lookup_url(channel_input: str) -> str:
    """Turn a UC id, @handle, channel path or URL into a YouTube URL for yt-dlp."""
    value = channel_input.strip()
    if value.startswith(("http://", "https://")):
        return value
    if CHANNEL_ID_RE.match(value):
        return f"https://www.youtube.com/channel/{value}"
    value = value.lstrip("/")
    if value.startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/")):
        return f"https://{value}"
    if value.startswith(("@", "channel/", "c/", "user/")):
        return f"https://www.youtube.com/{value}"
    return f"https://www.youtube.com/@{value}"


def resolve_channel(channel_input: str) -> tuple[str, str]:
    """Resolve any channel reference to (channel_id, name) via yt-dlp (blocking)."""
    options = DownloadOptions(cookies_path=settings.cookies_path)
    info = downloader.extract_channel_info(channel_lookup_url(channel_input), options)
    channel_id = str(info.get("channel_id") or info.get("id") or "")
    if not CHANNEL_ID_RE.match(channel_id):
        raise ValueError(f"No channel ID found for {channel_input!r}")
    name = str(info.get("channel") or info.get("uploader") or info.get("title") or "")
    return channel_id, name.strip()


@router.post("/subscriptions")
async def add_subscription(
    channel_input: str = Form(...),
    name: str = Form(""),
    library: str = Form("default"),
    retention_count: str | None = Form(None),
    retention_days: str | None = Form(None),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Add a YouTube channel subscription (UC id, @handle or channel URL; name optional)."""
    redirect = RedirectResponse(f"/subscriptions?lib={library}", status_code=303)
    channel_input = channel_input.strip()
    name = name.strip()

    known_id = CHANNEL_URL_ID_RE.search(channel_input)
    if CHANNEL_ID_RE.match(channel_input) and name:
        channel_id = channel_input
    elif known_id and name:
        channel_id = known_id.group(1)
    else:
        try:
            channel_id, resolved_name = await asyncio.to_thread(resolve_channel, channel_input)
        except Exception as exc:
            logger.warning("Could not resolve channel %r: %s", channel_input, exc)
            return redirect
        name = name or resolved_name or channel_id

    r_count = _positive_int_or_none(retention_count)
    r_days = _positive_int_or_none(retention_days)
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    cursor = db_conn.cursor()
    cursor.execute(
        """
        INSERT INTO channels (
            id, name, url, feed_url, folder_name, library,
            retention_count, retention_days, auto_download
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(NULLIF(excluded.name, ''), channels.name),
            url = COALESCE(NULLIF(excluded.url, ''), channels.url),
            feed_url = excluded.feed_url,
            library = excluded.library,
            retention_count = excluded.retention_count,
            retention_days = excluded.retention_days,
            auto_download = excluded.auto_download,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (
            channel_id,
            name,
            f"https://www.youtube.com/channel/{channel_id}",
            feed_url,
            name,
            library or "default",
            r_count,
            r_days,
        ),
    )
    return redirect


@router.post("/subscriptions/sync")
async def sync_single_subscription(
    channel_id: str = Form(...),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Trigger manual RSS sync for a channel."""
    queue = JobQueue()
    poller = SubscriptionPoller(queue=queue, database=db)
    await asyncio.to_thread(poller.poll_channel, channel_id)
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/sync-all")
async def sync_all_subscriptions(
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Trigger RSS sync for all channels."""
    queue = JobQueue()
    poller = SubscriptionPoller(queue=queue, database=db)
    await asyncio.to_thread(poller.poll_all_channels)
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/delete")
async def delete_subscription(
    channel_id: str = Form(...),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Delete a channel subscription."""
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    return RedirectResponse("/subscriptions", status_code=303)


@router.post("/subscriptions/import")
async def import_subscriptions(
    subscriptions_file: UploadFile | None = File(None),
    ta_url: str | None = Form(None),
    ta_token: str | None = Form(None),
    library: str = Form("default"),
    db_conn=Depends(get_db),
) -> RedirectResponse:
    """Import subscriptions from OPML/JSON file or TubeArchivist API."""
    migrator = SubscriptionsMigrator(database=db)
    if subscriptions_file and subscriptions_file.filename:
        content = await subscriptions_file.read()
        stripped = content.strip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            channels = migrator.parse_json(content)
        else:
            channels = migrator.parse_opml(content)
        migrator.import_channels(channels, library=library)
    elif ta_url and ta_token:
        channels = migrator.fetch_from_tubearchivist_api(ta_url, ta_token)
        migrator.import_channels(channels, library=library)

    return RedirectResponse(f"/subscriptions?lib={library}", status_code=303)
