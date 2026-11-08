"""YouTube RSS feed parser and channel update synchronizer."""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import defusedxml.ElementTree as DefusedET
import httpx

from app.core.config import settings
from app.core.settings_service import settings_service
from app.db.database import Database, db
from app.retention.engine import parse_published
from app.worker.models import MAX_VIDEO_ATTEMPTS, utc_now_str
from app.worker.queue import JobQueue

logger = logging.getLogger(__name__)

ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
MEDIA_NS = "http://search.yahoo.com/mrss/"


def _rule(channel_value: int | None, default: int | None) -> int:
    """Effective retention value; `0` means unlimited."""
    value = channel_value if channel_value is not None else default
    return value if value and value > 0 else 0


NAMESPACES = {
    "atom": ATOM_NS,
    "yt": YT_NS,
    "media": MEDIA_NS,
}


@dataclass
class RssEntry:
    """Parsed video upload item from YouTube RSS feed."""

    video_id: str
    channel_id: str
    title: str
    link: str
    published: str
    author: str = ""
    description: str = ""
    thumbnail_url: str = ""
    is_short: bool = False
    is_live: bool = False


class RssFeedParser:
    """Parses YouTube Atom/RSS video feeds using defusedxml."""

    @staticmethod
    def parse_feed(xml_content: str | bytes) -> list[RssEntry]:
        """Parse YouTube channel RSS feed and extract video entries."""
        try:
            root = DefusedET.fromstring(xml_content)
        except Exception as exc:
            logger.error("Failed to parse RSS XML: %s", exc)
            return []

        entries: list[RssEntry] = []
        for entry_el in root.findall("atom:entry", NAMESPACES):
            vid_el = entry_el.find("yt:videoId", NAMESPACES)
            title_el = entry_el.find("atom:title", NAMESPACES)
            link_el = entry_el.find("atom:link", NAMESPACES)
            pub_el = entry_el.find("atom:published", NAMESPACES)
            ch_el = entry_el.find("yt:channelId", NAMESPACES)
            author_el = entry_el.find("atom:author/atom:name", NAMESPACES)

            if vid_el is None or not vid_el.text:
                continue

            video_id = vid_el.text.strip()
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            published = pub_el.text.strip() if pub_el is not None and pub_el.text else ""
            channel_id = ch_el.text.strip() if ch_el is not None and ch_el.text else ""
            author = author_el.text.strip() if author_el is not None and author_el.text else ""

            # Media group metadata
            desc = ""
            thumb_url = ""
            media_group = entry_el.find("media:group", NAMESPACES)
            if media_group is not None:
                desc_el = media_group.find("media:description", NAMESPACES)
                if desc_el is not None and desc_el.text:
                    desc = desc_el.text.strip()
                thumb_el = media_group.find("media:thumbnail", NAMESPACES)
                if thumb_el is not None:
                    thumb_url = thumb_el.attrib.get("url", "")

            # Heuristic detection for Shorts and Live Streams
            is_short = "/shorts/" in link or "#shorts" in title.lower() or "#short" in title.lower()
            is_live = "live" in title.lower() and "stream" in title.lower()

            entries.append(
                RssEntry(
                    video_id=video_id,
                    channel_id=channel_id,
                    title=title,
                    link=link or f"https://www.youtube.com/watch?v={video_id}",
                    published=published,
                    author=author,
                    description=desc,
                    thumbnail_url=thumb_url,
                    is_short=is_short,
                    is_live=is_live,
                )
            )

        return entries


class SubscriptionPoller:
    """Polls YouTube RSS feeds for active channel subscriptions and enqueues downloads."""

    def __init__(
        self,
        queue: JobQueue,
        database: Database = db,
        client: httpx.Client | None = None,
        ignore_shorts: bool | None = None,
        ignore_live: bool | None = None,
    ) -> None:
        self.queue = queue
        self.db = database
        self._client = client
        self.ignore_shorts = (
            ignore_shorts
            if ignore_shorts is not None
            else settings_service.get_setting_bool("ignore_shorts", default=True)
        )
        self.ignore_live = (
            ignore_live
            if ignore_live is not None
            else settings_service.get_setting_bool("ignore_live", default=True)
        )

    @staticmethod
    def get_feed_url(channel_id: str) -> str:
        """Construct standard YouTube RSS feed URL from channel ID."""
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    def fetch_feed_content(self, feed_url: str) -> str | None:
        """Fetch RSS feed XML over HTTP."""
        try:
            if self._client:
                resp = self._client.get(feed_url, timeout=15.0)
                resp.raise_for_status()
                return resp.text
            else:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.get(feed_url)
                    resp.raise_for_status()
                    return resp.text
        except Exception as exc:
            logger.warning("Error fetching RSS feed %s: %s", feed_url, exc)
            return None

    def poll_channel(self, channel_id: str) -> list[str]:
        """Poll single channel feed and enqueue pending videos. Returns enqueued video IDs."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, feed_url, auto_download, library,
                       retention_days, retention_count
                FROM channels WHERE id = ?
                """,
                (channel_id,),
            )
            ch = cursor.fetchone()
            if not ch:
                logger.warning("Channel %s not found in database", channel_id)
                return []

            if not ch["auto_download"]:
                logger.debug("Skipping channel %s: auto_download is disabled", channel_id)
                return []

            feed_url = ch["feed_url"] or self.get_feed_url(channel_id)
            library = ch["library"] or "default"
            channel_name = ch["name"]
            # Channel rule (NULL = not set) falls back to the global default from DB/settings
            default_days = settings_service.get_setting_int(
                "retention_days", settings.retention_days
            )
            default_count = settings_service.get_setting_int(
                "retention_count", settings.retention_count
            )
            ttl_days = _rule(ch["retention_days"], default_days)
            max_count = _rule(ch["retention_count"], default_count)

        content = self.fetch_feed_content(feed_url)
        if not content:
            return []

        entries = RssFeedParser.parse_feed(content)
        enqueued_ids: list[str] = []
        now = datetime.now(UTC)
        now_str = utc_now_str()

        with self.db.connection() as conn:
            cursor = conn.cursor()
            candidates: list[RssEntry] = []
            for entry in entries:
                if self.ignore_shorts and entry.is_short:
                    logger.debug("Skipping YouTube Short: %s (%s)", entry.title, entry.video_id)
                    continue

                if self.ignore_live and entry.is_live:
                    logger.debug("Skipping Live Stream: %s (%s)", entry.title, entry.video_id)
                    continue

                # Skip every known video (completed, deleted, skipped, failed) unless it
                # is a failed download whose retry backoff has elapsed
                cursor.execute(
                    """
                    SELECT 1 FROM videos
                    WHERE id = ? AND NOT (
                        status = 'failed' AND attempts < ?
                        AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
                    )
                    """,
                    (entry.video_id, MAX_VIDEO_ATTEMPTS, now_str),
                )
                if cursor.fetchone():
                    continue

                # Retention TTL: do not download what retention would delete right away
                published = parse_published(entry.published)
                if (
                    ttl_days > 0
                    and published
                    and (now - published).total_seconds() / 86400 > ttl_days
                ):
                    logger.debug("Skipping %s: older than %d days", entry.video_id, ttl_days)
                    continue

                candidates.append(entry)

            if max_count > 0 and candidates:
                candidates = self._newest_within_count(cursor, channel_id, candidates, max_count)

        for entry in candidates:
            payload: dict[str, Any] = {
                "video_id": entry.video_id,
                "url": entry.link,
                "title": entry.title,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "library": library,
                "published_at": entry.published,
            }
            if settings_service.get_setting_bool("sponsorblock_enabled", default=True):
                cats = settings_service.get_setting("sponsorblock_categories", "sponsor,intro")
                payload["sponsorblock_remove"] = [c.strip() for c in cats.split(",") if c.strip()]

            # Enqueue download job with video_id deduplication
            job = self.queue.enqueue(
                job_type="download_video",
                payload=payload,
                deduplicate_key="video_id",
            )
            if job:
                enqueued_ids.append(entry.video_id)

        return enqueued_ids

    @staticmethod
    def _newest_within_count(
        cursor: sqlite3.Cursor, channel_id: str, candidates: list[RssEntry], max_count: int
    ) -> list[RssEntry]:
        """Keep only candidates that rank among the `max_count` newest videos.

        Ranking pool: the channel's completed unpinned videos (what retention counts) plus
        the candidates. Anything outside the top N would be deleted by retention right away.
        Candidates without a parseable date are kept.
        """
        cursor.execute(
            """
            SELECT id, published_at FROM videos
            WHERE channel_id = ? AND status = 'completed' AND is_pinned = 0
            """,
            (channel_id,),
        )
        pool = {row["id"]: parse_published(row["published_at"]) for row in cursor.fetchall()}
        for entry in candidates:
            pool[entry.video_id] = parse_published(entry.published)
        dated = sorted(((d, vid) for vid, d in pool.items() if d), reverse=True)
        newest = {vid for _, vid in dated[:max_count]}
        return [
            e for e in candidates if e.video_id in newest or parse_published(e.published) is None
        ]

    def poll_all_channels(self) -> dict[str, list[str]]:
        """Poll all active channels marked for auto_download."""
        with self.db.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM channels WHERE auto_download = 1")
            channel_ids = [row["id"] for row in cursor.fetchall()]

        results: dict[str, list[str]] = {}
        for cid in channel_ids:
            enqueued = self.poll_channel(cid)
            results[cid] = enqueued
        return results
