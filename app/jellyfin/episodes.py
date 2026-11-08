"""Stable episode numbers: MMDD + per-channel, per-day counter (e.g. 15 March #1 -> 31501)."""

import re
import sqlite3
from datetime import UTC, datetime

MAX_DAILY_COUNTER = 99


def episode_date(date_str: str | None) -> str:
    """Normalize `YYYY-MM-DD`, `YYYYMMDD` or ISO timestamps to `YYYY-MM-DD` (fallback: today)."""
    raw = str(date_str or "").strip()
    match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", raw)
    if match:
        return "-".join(match.groups())
    return datetime.now(UTC).strftime("%Y-%m-%d")


def episode_number(date_str: str, counter: int) -> int:
    """Episode number for the `counter`-th video (1-based) of a day."""
    day = episode_date(date_str)
    month, dom = int(day[5:7]), int(day[8:10])
    return int(f"{month}{dom:02d}{min(counter, MAX_DAILY_COUNTER):02d}")


def assign_episode(
    conn: sqlite3.Connection, video_id: str, channel_id: str, date_str: str | None
) -> int:
    """Return the video's stored episode number, or the next free one of its channel and day.

    The number is only persisted by the caller's video upsert; once stored it never changes,
    so deleting or re-downloading siblings does not renumber anything.
    """
    row = conn.execute("SELECT episode FROM videos WHERE id = ?", (video_id,)).fetchone()
    if row and row[0]:
        return int(row[0])

    day = episode_date(date_str)
    first = episode_number(day, 1)
    last = episode_number(day, MAX_DAILY_COUNTER)
    row = conn.execute(
        """
        SELECT MAX(episode) FROM videos
        WHERE channel_id = ? AND substr(published_at, 1, 10) = ?
          AND episode BETWEEN ? AND ? AND id != ?
        """,
        (channel_id, day, first, last, video_id),
    ).fetchone()
    highest = row[0] if row else None
    return min(highest + 1, last) if highest else first
