"""Database-backed settings and library management."""

import logging
import os
import re
from typing import Any

import httpx

from app.core.config import settings
from app.db.database import Database, db

logger = logging.getLogger(__name__)

LIBRARY_NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")

# Default label/icon mappings for initial seeding and fallback
_DEFAULT_LIBRARY_METADATA = {
    "default": ("📁", "Default"),
    "kids": ("🧒", "Kids"),
    "dokus": ("📚", "Documentaries"),
    "tech": ("💻", "Tech"),
}


class SettingsService:
    """Manages application settings and libraries stored in SQLite."""

    def __init__(self, database: Database | None = None) -> None:
        self._db = database

    @property
    def db(self) -> Database:
        return self._db if self._db is not None else db

    @db.setter
    def db(self, database: Database) -> None:
        self._db = database

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Get a string setting value by key."""
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
                row = cursor.fetchone()
                if row and row[0] is not None:
                    return str(row[0])
        except Exception as exc:
            logger.debug("Failed to read setting %s: %s", key, exc)
        return default

    def get_setting_int(self, key: str, default: int | None = None) -> int | None:
        """Get an integer setting value by key."""
        val = self.get_setting(key)
        if val is not None and val.strip():
            try:
                return int(val.strip())
            except ValueError:
                pass
        return default

    def get_setting_bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean setting value by key."""
        val = self.get_setting(key)
        if val is None:
            return default
        return val.strip().lower() in ("1", "true", "yes", "on")

    def set_setting(self, key: str, value: Any) -> None:
        """Set a single setting value."""
        str_val = "" if value is None else str(value).strip()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, str_val),
            )

    def set_settings(self, items: dict[str, Any]) -> None:
        """Set multiple setting values in a single transaction."""
        with self.db.connection() as conn:
            for key, value in items.items():
                str_val = "" if value is None else str(value).strip()
                conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, str_val),
                )

    def get_all_settings(self) -> dict[str, str]:
        """Fetch all settings as a key-value dictionary."""
        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM settings")
                return {row[0]: row[1] for row in cursor.fetchall()}
        except Exception as exc:
            logger.debug("Failed to read all settings: %s", exc)
            return {}

    # --- Libraries Management ---

    def get_libraries(self) -> list[dict[str, Any]]:
        """Return all libraries with their labels, icons, and channel/video counts.

        If the `LIBRARIES` environment variable was explicitly configured, it acts as an
        override for backwards compatibility and test isolation.
        """
        env_libs = os.environ.get("LIBRARIES")
        if env_libs is not None:
            result = []
            for name in settings.libraries:
                icon, label = _DEFAULT_LIBRARY_METADATA.get(name, ("📂", name.capitalize()))
                result.append(
                    {
                        "name": name,
                        "label": label,
                        "icon": icon,
                        "channel_count": 0,
                        "video_count": 0,
                    }
                )
            return result

        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT l.name, l.label, l.icon,
                           COUNT(DISTINCT c.id) as channel_count,
                           COUNT(DISTINCT v.id) as video_count
                    FROM libraries l
                    LEFT JOIN channels c ON c.library = l.name
                    LEFT JOIN videos v ON v.channel_id = c.id
                    GROUP BY l.name
                    ORDER BY CASE WHEN l.name = 'default' THEN 0 ELSE 1 END, l.name ASC
                    """
                )
                rows = cursor.fetchall()
                if rows:
                    return [
                        {
                            "name": row[0],
                            "label": row[1],
                            "icon": row[2] or "📁",
                            "channel_count": row[3],
                            "video_count": row[4],
                        }
                        for row in rows
                    ]
        except Exception as exc:
            logger.debug("Failed to query libraries from database: %s", exc)

        # Fallback to config settings.libraries
        fallback = []
        for name in settings.libraries:
            icon, label = _DEFAULT_LIBRARY_METADATA.get(name, ("📁", name.capitalize()))
            fallback.append(
                {
                    "name": name,
                    "label": label,
                    "icon": icon,
                    "channel_count": 0,
                    "video_count": 0,
                }
            )
        return fallback

    def add_library(self, name: str, label: str, icon: str = "📁") -> tuple[bool, str]:
        """Add a new library to the database."""
        clean_name = name.strip().lower()
        clean_label = label.strip()
        clean_icon = icon.strip() or "📁"

        if not LIBRARY_NAME_PATTERN.match(clean_name):
            return (
                False,
                "Library name must contain only 1-32 lowercase letters, "
                "numbers, hyphens, or underscores.",
            )

        if clean_name in ("all", "api"):
            return False, f"The name '{clean_name}' is reserved."

        if not clean_label:
            clean_label = clean_name.capitalize()

        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM libraries WHERE name = ?", (clean_name,))
                if cursor.fetchone():
                    return False, f"Library '{clean_name}' already exists."

                conn.execute(
                    "INSERT INTO libraries (name, label, icon) VALUES (?, ?, ?)",
                    (clean_name, clean_label, clean_icon),
                )
            return True, f"Library '{clean_label}' created successfully."
        except Exception as exc:
            logger.error("Failed to add library: %s", exc)
            return False, f"Failed to create library: {exc}"

    def delete_library(self, name: str) -> tuple[bool, str]:
        """Delete a library if not in use and not the default library."""
        clean_name = name.strip().lower()
        if clean_name == "default":
            return False, "The default library ('default') cannot be deleted."

        try:
            with self.db.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM channels WHERE library = ?", (clean_name,))
                row = cursor.fetchone()
                if row and row[0] > 0:
                    return (
                        False,
                        f"Library '{clean_name}' still contains {row[0]} channel(s) "
                        "and cannot be deleted.",
                    )

                cursor.execute("DELETE FROM libraries WHERE name = ?", (clean_name,))
                if cursor.rowcount == 0:
                    return False, f"Library '{clean_name}' was not found."

            return True, f"Library '{clean_name}' deleted successfully."
        except Exception as exc:
            logger.error("Failed to delete library: %s", exc)
            return False, f"Failed to delete library: {exc}"


# Global settings service instance
settings_service = SettingsService()


# --- Jellyfin Integration Helper ---


async def test_jellyfin_connection(url: str, api_key: str) -> tuple[bool, str]:
    """Test connection and authentication to Jellyfin server."""
    clean_url = url.strip().rstrip("/")
    clean_key = api_key.strip()
    if not clean_url:
        return False, "No Jellyfin server URL provided."

    test_endpoint = f"{clean_url}/System/Info"
    headers = {"X-Emby-Token": clean_key} if clean_key else {}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(test_endpoint, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                server_name = data.get("ServerName", "Jellyfin")
                version = data.get("Version", "unknown")
                return True, f"Connection successful! Server: {server_name} (v{version})"
            elif resp.status_code in (401, 403):
                return (
                    False,
                    f"Authentication failed (HTTP {resp.status_code}). Check API key.",
                )
            else:
                return False, f"Jellyfin returned HTTP status {resp.status_code}."
    except Exception as exc:
        return False, f"Connection failed: {exc}"


async def notify_jellyfin_refresh(
    service: SettingsService | None = None,
) -> bool:
    """Trigger a Jellyfin library refresh if configured."""
    svc = service or settings_service
    jellyfin_url = svc.get_setting("jellyfin_url", "").strip().rstrip("/")
    api_key = svc.get_setting("jellyfin_api_key", "").strip()

    if not jellyfin_url:
        return False

    refresh_url = f"{jellyfin_url}/Library/Refresh"
    headers = {"X-Emby-Token": api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(refresh_url, headers=headers)
            if resp.status_code in (200, 204):
                logger.info("Triggered Jellyfin Library Refresh at %s", jellyfin_url)
                return True
            else:
                logger.warning(
                    "Jellyfin Library Refresh returned status %s: %s",
                    resp.status_code,
                    resp.text,
                )
                return False
    except Exception as exc:
        logger.warning("Failed to trigger Jellyfin Library Refresh: %s", exc)
        return False
