"""YouTube RSS feed parser and channel update synchronizer."""

import logging
from dataclasses import dataclass

import defusedxml.ElementTree as DefusedET
import httpx

from app.db.database import Database, db
from app.worker.models import MAX_VIDEO_ATTEMPTS, utc_now_str
from app.worker.queue import JobQueue

logger = logging.getLogger(__name__)

ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
MEDIA_NS = "http://search.yahoo.com/mrss/"

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
        ignore_shorts: bool = True,
        ignore_live: bool = True,
    ) -> None:
        self.queue = queue
        self.db = database
        self._client = client
        self.ignore_shorts = ignore_shorts
        self.ignore_live = ignore_live

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
                SELECT id, name, feed_url, auto_download, library
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

        content = self.fetch_feed_content(feed_url)
        if not content:
            return []

        entries = RssFeedParser.parse_feed(content)
        enqueued_ids: list[str] = []
        now = utc_now_str()

        with self.db.connection() as conn:
            cursor = conn.cursor()
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
                    (entry.video_id, MAX_VIDEO_ATTEMPTS, now),
                )
                if cursor.fetchone():
                    continue

                # Enqueue download job with video_id deduplication
                job = self.queue.enqueue(
                    job_type="download_video",
                    payload={
                        "video_id": entry.video_id,
                        "url": entry.link,
                        "title": entry.title,
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "library": library,
                        "published_at": entry.published,
                    },
                    deduplicate_key="video_id",
                )
                if job:
                    enqueued_ids.append(entry.video_id)

        return enqueued_ids

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
