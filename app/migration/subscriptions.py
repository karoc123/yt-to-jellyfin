"""TubeArchivist and OPML subscriptions import and migration."""

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import defusedxml.ElementTree as DefusedET
import httpx

from app.db.database import Database, db
from app.jellyfin.layout import sanitize_filename

logger = logging.getLogger(__name__)


@dataclass
class SubscribedChannel:
    """Represents a channel subscription to import."""

    channel_id: str
    name: str
    url: str = ""
    feed_url: str = ""


class SubscriptionsMigrator:
    """Parses and imports channel subscriptions from TubeArchivist API, JSON, or OPML."""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database if database is not None else db

    @staticmethod
    def extract_channel_id(text: str) -> str | None:
        """Extract a channel ID or handle from URL or text."""
        if not text:
            return None
        # Check channel_id= parameter in query string
        query_match = re.search(r"channel_id=([a-zA-Z0-9_-]+)", text)
        if query_match:
            return query_match.group(1)
        # Check /channel/<id>
        channel_match = re.search(r"/channel/([a-zA-Z0-9_-]+)", text)
        if channel_match:
            return channel_match.group(1)
        # Check standard YouTube UC channel ID
        uc_match = re.search(r"(UC[a-zA-Z0-9_-]+)", text)
        if uc_match:
            return uc_match.group(1)
        return None

    def parse_opml(self, xml_content: str | bytes) -> list[SubscribedChannel]:
        """Parse standard OPML XML (from YouTube Takeout, TubeArchivist, RSS readers)."""
        try:
            root = DefusedET.fromstring(xml_content)
        except Exception as exc:
            logger.error("Failed to parse OPML XML: %s", exc)
            return []

        channels: list[SubscribedChannel] = []
        for outline in root.findall(".//outline"):
            xml_url = outline.attrib.get("xmlUrl", "")
            html_url = outline.attrib.get("htmlUrl", "")
            title = outline.attrib.get("title") or outline.attrib.get("text") or ""

            channel_id = self.extract_channel_id(xml_url) or self.extract_channel_id(html_url)
            if not channel_id:
                channel_id = self.extract_channel_id(title)

            if channel_id:
                clean_title = html.unescape(title).strip() or channel_id
                channels.append(
                    SubscribedChannel(
                        channel_id=channel_id,
                        name=clean_title,
                        url=html_url or f"https://www.youtube.com/channel/{channel_id}",
                        feed_url=xml_url
                        or f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                    )
                )

        return channels

    def parse_json(self, json_content: str | bytes) -> list[SubscribedChannel]:
        """Parse TubeArchivist API JSON response or Elasticsearch ta_channel search dump."""
        try:
            raw_data = json.loads(json_content)
        except Exception as exc:
            logger.error("Failed to parse JSON: %s", exc)
            return []

        items: list[dict] = []
        if isinstance(raw_data, list):
            items = raw_data
        elif isinstance(raw_data, dict):
            if "data" in raw_data and isinstance(raw_data["data"], list):
                items = raw_data["data"]
            elif "hits" in raw_data and "hits" in raw_data["hits"]:
                # Elasticsearch search output
                items = [
                    hit["_source"]
                    for hit in raw_data["hits"]["hits"]
                    if isinstance(hit, dict) and "_source" in hit
                ]
            else:
                items = [raw_data]

        channels: list[SubscribedChannel] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # If explicit channel_subscribed flag exists, only import if subscribed
            if "channel_subscribed" in item and not item["channel_subscribed"]:
                continue

            channel_id = item.get("channel_id") or item.get("id")
            if not channel_id:
                continue

            name = item.get("channel_name") or item.get("name") or str(channel_id)
            channels.append(
                SubscribedChannel(
                    channel_id=str(channel_id),
                    name=str(name).strip(),
                    url=f"https://www.youtube.com/channel/{channel_id}",
                    feed_url=f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                )
            )

        return channels

    def fetch_from_tubearchivist_api(
        self,
        base_url: str,
        api_token: str,
    ) -> list[SubscribedChannel]:
        """Fetch all subscribed channels directly from running TubeArchivist instance."""
        clean_url = base_url.rstrip("/")
        endpoint = f"{clean_url}/api/channel/"
        headers = {
            "Authorization": f"Token {api_token.strip()}",
            "Content-Type": "application/json",
        }

        all_channels: list[SubscribedChannel] = []
        page = 1

        with httpx.Client(timeout=20.0) as client:
            while True:
                logger.info("Fetching TubeArchivist channels page %d from %s...", page, endpoint)
                try:
                    resp = client.get(endpoint, headers=headers, params={"page": page})
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.error("Error communicating with TubeArchivist API: %s", exc)
                    break

                items = data.get("data", [])
                if not items:
                    break

                for ch in items:
                    if ch.get("channel_subscribed"):
                        cid = ch.get("channel_id")
                        cname = ch.get("channel_name") or cid
                        if cid:
                            all_channels.append(
                                SubscribedChannel(
                                    channel_id=str(cid),
                                    name=str(cname),
                                    url=f"https://www.youtube.com/channel/{cid}",
                                    feed_url=f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                                )
                            )

                paginate = data.get("paginate", {})
                last_page = paginate.get("last_page", 1)
                if page >= last_page:
                    break
                page += 1

        return all_channels

    def import_channels(
        self,
        channels: list[SubscribedChannel],
        library: str = "default",
        retention_count: int | None = None,
        retention_days: int | None = None,
    ) -> int:
        """Persist channel subscriptions into SQLite catalog with auto_download=1."""
        if not channels:
            return 0

        imported = 0
        now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.db.connection() as conn:
            cursor = conn.cursor()
            for ch in channels:
                feed_url = (
                    ch.feed_url
                    or f"https://www.youtube.com/feeds/videos.xml?channel_id={ch.channel_id}"
                )
                url = ch.url or f"https://www.youtube.com/channel/{ch.channel_id}"
                folder_name = sanitize_filename(ch.name)

                cursor.execute(
                    """
                    INSERT INTO channels (
                        id, name, url, feed_url, folder_name, library,
                        retention_count, retention_days, auto_download, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        feed_url = excluded.feed_url,
                        folder_name = excluded.folder_name,
                        library = excluded.library,
                        retention_count = COALESCE(
                            excluded.retention_count, channels.retention_count
                        ),
                        retention_days = COALESCE(
                            excluded.retention_days, channels.retention_days
                        ),
                        auto_download = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        ch.channel_id,
                        ch.name,
                        url,
                        feed_url,
                        folder_name,
                        library or "default",
                        retention_count,
                        retention_days,
                        now_str,
                        now_str,
                    ),
                )
                imported += 1

        logger.info("Successfully imported %d channel subscription(s)", imported)
        return imported

    def import_from_file(
        self,
        file_path: Path | str,
        library: str = "default",
    ) -> int:
        """Detect file format (JSON or OPML/XML) and import subscriptions."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Subscriptions file not found: {path}")

        content = path.read_text(encoding="utf-8")
        # Detect if JSON or XML/OPML
        stripped = content.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            channels = self.parse_json(content)
        else:
            channels = self.parse_opml(content)

        return self.import_channels(channels, library=library)
