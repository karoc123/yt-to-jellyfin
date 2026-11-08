"""Reproductions for review findings of Slice D (see review plan, D1-D7).

All tests fake yt-dlp at the library level: `yt_dlp.YoutubeDL` is replaced by
`FakeYoutubeDL` (counts `extract_info`, supports `process_ie_result`, `prepare_filename`,
`urlopen`, context manager, invokes `progress_hooks` and writes a dummy media file).
The worker runs with the real `YtDlpService`, so the fix may change the service API freely.
No network: HTTP image downloads (D5) are faked via `YoutubeDL.urlopen`, `httpx.get`,
`httpx.Client.get` and `urllib.request.urlopen`.

Assumed API (new entry points only):
- D4: episode number = int(f"{month}{day:02d}{n:02d}") with n = per-day counter of the
  channel, written as `<episode>` in the episode NFO by the worker.
- D5: the worker fetches channel artwork on a channel's first download via a yt-dlp
  channel extraction (URL containing `/channel/<UC…>` or `/@handle`, `thumbnails` list
  with ids such as `avatar_uncropped`/`banner_uncropped`) and writes `poster.jpg` into the
  channel folder; the image bytes are fetched through one of the faked HTTP paths above.
- D7: `POST /api/subscriptions` resolves `@handle`/URLs via `yt_dlp.YoutubeDL.extract_info`
  (`channel_id`, `channel`/`uploader`); the `name` form field is optional.
"""

import asyncio
import copy
import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import httpx
import pytest
import yt_dlp
from fastapi.testclient import TestClient

from app.core import config
from app.db import Database, get_db
from app.downloader.service import YtDlpService
from app.jellyfin.layout import JellyfinLayout
from app.jellyfin.nfo import NfoGenerator
from app.worker.queue import JobQueue
from app.worker.worker import BackgroundWorker

CHANNEL_ID = "UCsynthD000000000000001"
CHANNEL_NAME = "Synth D Channel"
FAKE_JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg\xff\xd9"


def _video_info(video_id: str, upload_date: str = "20240315", title: str = "") -> dict:
    return {
        "_type": "video",
        "id": video_id,
        "title": title or f"Synthetic {video_id}",
        "channel": CHANNEL_NAME,
        "uploader": CHANNEL_NAME,
        "channel_id": CHANNEL_ID,
        "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}",
        "upload_date": upload_date,
        "duration": 60,
        "ext": "mp4",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
    }


def _channel_info() -> dict:
    return {
        "_type": "playlist",
        "id": CHANNEL_ID,
        "channel_id": CHANNEL_ID,
        "channel": CHANNEL_NAME,
        "uploader": CHANNEL_NAME,
        "title": CHANNEL_NAME,
        "channel_url": f"https://www.youtube.com/channel/{CHANNEL_ID}",
        "webpage_url": f"https://www.youtube.com/channel/{CHANNEL_ID}",
        "thumbnails": [
            {
                "id": "banner_uncropped",
                "url": "https://synthetic.invalid/banner.jpg",
                "preference": -10,
            },
            {
                "id": "avatar_uncropped",
                "url": "https://synthetic.invalid/avatar.jpg",
                "preference": 1,
            },
        ],
        "entries": [],
    }


class _FakeResponse(io.BytesIO):
    status_code = 200
    status = 200
    headers: dict[str, str] = {"content-type": "image/jpeg"}

    @property
    def content(self) -> bytes:
        return self.getvalue()

    def raise_for_status(self) -> None:
        return None


class FakeYoutubeDL:
    """Minimal stand-in for `yt_dlp.YoutubeDL`; per-test registry in class attributes."""

    infos: dict[str, dict] = {}  # video id / playlist id / channel id -> info dict
    extract_calls: list[str] = []
    process_calls: list[str] = []

    def __init__(self, params: dict[str, Any] | None = None, *args: Any, **kwargs: Any):
        self.params = params or {}

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    # --- lookup -------------------------------------------------------------------
    def _lookup(self, url: str) -> dict:
        m = re.search(r"[?&]v=([\w-]+)", url) or re.search(r"youtu\.be/([\w-]+)", url)
        if m:
            return copy.deepcopy(self.infos[m.group(1)])
        m = re.search(r"[?&]list=([\w-]+)", url)
        if m:
            return copy.deepcopy(self.infos[m.group(1)])
        if "/channel/" in url or "@" in url:
            return _channel_info()
        if url in self.infos:  # bare video id
            return copy.deepcopy(self.infos[url])
        raise AssertionError(f"FakeYoutubeDL: unexpected URL {url!r}")

    # --- yt-dlp API ---------------------------------------------------------------
    def extract_info(self, url: str, download: bool = True, *args: Any, **kwargs: Any) -> dict:
        type(self).extract_calls.append(url)
        info = self._lookup(url)
        if info.get("_type") == "playlist" and self.params.get("extract_flat"):
            for entry in info.get("entries") or []:
                entry.setdefault("_type", "url")
        if download and info.get("_type", "video") == "video":
            self._download(info)
        return info

    def process_ie_result(
        self, ie_result: dict, download: bool = True, *args: Any, **kwargs: Any
    ) -> dict:
        type(self).process_calls.append(str(ie_result.get("id")))
        if download and ie_result.get("_type", "video") == "video":
            self._download(ie_result)
        return ie_result

    def prepare_filename(self, info: dict, *args: Any, **kwargs: Any) -> str:
        tmpl = self.params.get("outtmpl") or "%(title)s [%(id)s].%(ext)s"
        if isinstance(tmpl, dict):
            tmpl = tmpl.get("default", "%(title)s [%(id)s].%(ext)s")
        return (
            str(tmpl)
            .replace("%(ext)s", str(info.get("ext") or "mp4"))
            .replace("%(id)s", str(info.get("id")))
            .replace("%(title)s", str(info.get("title")))
        )

    def sanitize_info(self, info: dict, *args: Any, **kwargs: Any) -> dict:
        return info

    def urlopen(self, req: Any, *args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(FAKE_JPEG)

    def _download(self, info: dict) -> None:
        for hook in self.params.get("progress_hooks") or []:
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 50,
                    "total_bytes": 100,
                    "filename": "synthetic.mp4",
                    "info_dict": info,
                }
            )
        path = Path(self.prepare_filename(info))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"dummy mp4")
        info["requested_downloads"] = [{"filepath": str(path)}]
        info["filepath"] = str(path)


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYoutubeDL]:
    fake = type(
        "FakeYDL", (FakeYoutubeDL,), {"infos": {}, "extract_calls": [], "process_calls": []}
    )
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)

    # No network for artwork downloads
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(FAKE_JPEG))
    monkeypatch.setattr(httpx.Client, "get", lambda self, *a, **k: _FakeResponse(FAKE_JPEG))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(FAKE_JPEG))
    return fake


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "slice_d.db")
    db.run_migrations()
    return db


@pytest.fixture
def media_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "media"
    monkeypatch.setattr(config.settings, "media_dir", root)
    monkeypatch.setattr(config.settings, "cookies_path", tmp_path / "no-cookies.txt")
    return root


def _worker(temp_db: Database, media_root: Path) -> tuple[BackgroundWorker, JobQueue]:
    queue = JobQueue(database=temp_db)
    worker = BackgroundWorker(
        queue=queue,
        downloader_service=YtDlpService(),
        layout=JellyfinLayout(media_root=media_root),
        nfo=NfoGenerator(),
        database=temp_db,
    )
    return worker, queue


def _run_job(worker: BackgroundWorker, queue: JobQueue, payload: dict) -> str:
    job = queue.enqueue("download_video", payload)
    assert job is not None
    assert asyncio.run(worker.process_next_job()) is True
    final = queue.get_job(job.id)
    assert final.status == "completed", final.error_message
    return job.id


def _video_extractions(fake: type[FakeYoutubeDL], video_id: str) -> list[str]:
    return [u for u in fake.extract_calls if video_id in u]


# --- D1: metadata fetched only once ---------------------------------------------------


def test_d1_single_extraction_per_download(
    fake_ydl: type[FakeYoutubeDL], temp_db: Database, media_root: Path
) -> None:
    fake_ydl.infos["synthD1vid1"] = _video_info("synthD1vid1")
    worker, queue = _worker(temp_db, media_root)

    _run_job(worker, queue, {"video_id": "synthD1vid1"})

    assert list(media_root.rglob("*[[]synthD1vid1[]].mp4"))
    # Channel-artwork extraction (D5) is not counted, only extractions of the video URL
    assert len(_video_extractions(fake_ydl, "synthD1vid1")) == 1


# --- D2: download progress reaches the job row ----------------------------------------


def test_d2_progress_written_to_job(
    fake_ydl: type[FakeYoutubeDL], temp_db: Database, media_root: Path
) -> None:
    fake_ydl.infos["synthD2vid1"] = _video_info("synthD2vid1")
    # Record every progress write (mark_completed sets 100 at the end)
    with temp_db.connection() as conn:
        conn.execute("CREATE TABLE progress_log (job_id TEXT, progress INTEGER)")
        conn.execute(
            """
            CREATE TRIGGER log_progress AFTER UPDATE OF progress ON jobs
            WHEN NEW.status = 'running'
            BEGIN
                INSERT INTO progress_log VALUES (NEW.id, NEW.progress);
            END
            """
        )
    worker, queue = _worker(temp_db, media_root)

    job_id = _run_job(worker, queue, {"video_id": "synthD2vid1"})

    with temp_db.connection() as conn:
        values = [
            r[0]
            for r in conn.execute(
                "SELECT progress FROM progress_log WHERE job_id = ?", (job_id,)
            ).fetchall()
        ]
    assert any(49 <= v <= 51 for v in values), values


# --- D3: playlist URLs fan out into one job per video ---------------------------------


def test_d3_playlist_enqueues_one_job_per_video(
    fake_ydl: type[FakeYoutubeDL], temp_db: Database, media_root: Path
) -> None:
    ids = ["synthD3vid1", "synthD3vid2", "synthD3vid3"]
    for vid in ids:
        fake_ydl.infos[vid] = _video_info(vid)
    fake_ydl.infos["PLsynthD3"] = {
        "_type": "playlist",
        "id": "PLsynthD3",
        "title": "Synthetic Playlist",
        "entries": [
            {
                "_type": "url",
                "ie_key": "Youtube",
                "id": vid,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "title": f"Synthetic {vid}",
                "channel_id": CHANNEL_ID,
                "channel": CHANNEL_NAME,
            }
            for vid in ids
        ],
    }
    worker, queue = _worker(temp_db, media_root)

    playlist_job_id = _run_job(
        worker,
        queue,
        {"url": "https://www.youtube.com/playlist?list=PLsynthD3", "library": "default"},
    )

    with temp_db.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM jobs WHERE type = 'download_video' AND id != ?",
            (playlist_job_id,),
        ).fetchall()
    video_ids = sorted(json.loads(r["payload"]).get("video_id") for r in rows)
    assert video_ids == ids
    assert not list(media_root.rglob("*.mp4"))


# --- D4: episode number from date (MMDD + per-day counter) ----------------------------


def _episode_of(media_root: Path, video_id: str) -> int:
    nfos = list(media_root.rglob(f"*[[]{video_id}[]].nfo"))
    assert len(nfos) == 1, nfos
    return int(ET.parse(nfos[0]).getroot().findtext("episode"))


def test_d4_episode_numbers_from_date(
    fake_ydl: type[FakeYoutubeDL], temp_db: Database, media_root: Path
) -> None:
    fake_ydl.infos["synthD4jul1"] = _video_info("synthD4jul1", "20240704")
    fake_ydl.infos["synthD4mar1"] = _video_info("synthD4mar1", "20240315")
    fake_ydl.infos["synthD4jul2"] = _video_info("synthD4jul2", "20240704")
    worker, queue = _worker(temp_db, media_root)

    # Downloaded out of chronological order on purpose
    for vid in ("synthD4jul1", "synthD4mar1", "synthD4jul2"):
        _run_job(worker, queue, {"video_id": vid})

    mar = _episode_of(media_root, "synthD4mar1")
    jul1 = _episode_of(media_root, "synthD4jul1")
    jul2 = _episode_of(media_root, "synthD4jul2")
    assert mar == 31501
    assert jul1 == 70401
    assert jul2 == 70402
    assert mar < jul1 < jul2


# --- D5: channel artwork on first download --------------------------------------------


@pytest.mark.xfail(strict=True, reason="D5: no channel poster.jpg")
def test_d5_channel_poster_written(
    fake_ydl: type[FakeYoutubeDL], temp_db: Database, media_root: Path
) -> None:
    fake_ydl.infos["synthD5vid1"] = _video_info("synthD5vid1")
    worker, queue = _worker(temp_db, media_root)

    _run_job(worker, queue, {"video_id": "synthD5vid1"})

    poster = media_root / "default" / CHANNEL_NAME / "poster.jpg"
    assert poster.is_file(), sorted(p.name for p in poster.parent.iterdir())
    assert poster.read_bytes() == FAKE_JPEG


# --- D6: filenames stay within 255 bytes ----------------------------------------------

COMPANION_SUFFIXES = [".mp4", ".nfo", "-thumb.jpg", ".de.vtt", ".zh-Hans.vtt", ".f399.mp4.part"]


@pytest.mark.xfail(strict=True, reason="D6: filename may exceed 255 bytes")
@pytest.mark.parametrize("char", ["字", "\U0001f600"], ids=["cjk", "emoji"])
def test_d6_filename_fits_255_bytes(char: str) -> None:
    layout = JellyfinLayout(media_root="/nonexistent")
    name = layout.get_video_filename(
        date_str="2024-03-15", title=char * 100, video_id="synthD6id01", extension="mp4"
    )
    stem = Path(name).stem
    assert name.endswith(" [synthD6id01].mp4")
    assert stem.startswith("2024-03-15 - " + char)
    for suffix in COMPANION_SUFFIXES:
        assert len((stem + suffix).encode("utf-8")) <= 255, suffix


# --- D7: subscribe by @handle / URL, name optional ------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, temp_db: Database) -> TestClient:
    import app.main as main_module

    monkeypatch.setattr("app.main.db", temp_db)
    monkeypatch.setattr("app.worker.queue.db", temp_db)
    monkeypatch.setattr(main_module.worker, "start", lambda: None)
    monkeypatch.setattr(main_module.worker, "stop", AsyncMock())
    monkeypatch.setattr(main_module.scheduler, "start", lambda: None)
    monkeypatch.setattr(main_module.scheduler, "stop", AsyncMock())

    def override_get_db():
        with temp_db.connection() as conn:
            yield conn

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app) as c:
        yield c
    main_module.app.dependency_overrides.clear()


@pytest.mark.xfail(strict=True, reason="D7: @handle/URL not resolved, name required")
@pytest.mark.parametrize("channel_input", ["@synthhandle", "https://www.youtube.com/@synthhandle"])
def test_d7_subscribe_by_handle(
    fake_ydl: type[FakeYoutubeDL],
    client: TestClient,
    temp_db: Database,
    channel_input: str,
) -> None:
    resp = client.post(
        "/api/subscriptions",
        data={"channel_input": channel_input, "library": "default"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    with temp_db.connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, name, feed_url FROM channels").fetchall()
    assert [r["id"] for r in rows] == [CHANNEL_ID]
    assert rows[0]["name"] == CHANNEL_NAME
    assert rows[0]["feed_url"] == (
        f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    )
