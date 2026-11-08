"""Reproductions for review findings of Slices F (UI) and G (backend odds and ends).

Every test is a strict xfail until the corresponding fix lands. Assumed (new) endpoints
and markup, kept as small as possible:

- F1: `POST /api/videos/pin` / `/api/videos/delete` redirect back to
  `/browser?channel_id=<id>[&lib=<lib>]`; the channel is derived from the video (or sent as
  hidden field); `lib` is an optional form field.
- F2: nav links (`<a class="nav-link">`) carry the active `?lib=`; `app.js` does not use
  `localStorage` at all (the library lives in the URL only).
- F3: `POST /api/jobs/{job_id}/retry` -> job `pending`; `POST /api/jobs/{job_id}/cancel`
  -> `cancelled`; `GET /partials/queue` returns an HTML fragment (no `<html>`); the
  failed job's `error_message` is rendered on `/`; internal job types `poll_channels` and
  `retention` are not listed in the queue.
- F4: feedback notices are elements with class `flash` or `role="status"`/`role="alert"`
  on the page reached after following the redirect (flash via query or cookie).
- F5: `POST /api/subscriptions/update` with `channel_id`, `library`, `retention_count`,
  `retention_days`, `auto_download` ("0"/absent = off); 0/empty retention -> NULL.
- F7: icon-only `<button>`s (no letter/digit in their text) have `aria-label`; templates
  contain no `style="` attributes except `style="width:` (dynamic progress bar width).
- F8: `/browser?channel_id=..&q=..` filters titles; page size 24, `page=2`; channel grid
  shows a human readable total size per channel (e.g. "150 MB").
- G3: endpoints declare `db_conn=Depends(get_db)` only where they use it (AST check).
- G4: the worker escapes `%` in the filename stem it hands to yt-dlp as `outtmpl`; checked
  with the real `yt_dlp.YoutubeDL.prepare_filename` semantics.
"""

import ast
import asyncio
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
from tests.test_review_slice_d import FAKE_JPEG, FakeYoutubeDL, _FakeResponse, _video_info

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"
APP_JS = ROOT / "app" / "static" / "js" / "app.js"

CHANNEL_ID = "UCsynthFG0000000000001"


# --- fixtures & helpers ---------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Database:
    db = Database(db_path=tmp_path / "slice_fg.db")
    db.run_migrations()
    return db


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, temp_db: Database) -> TestClient:
    import app.main as main_module

    for target in (
        "app.db.database.db",
        "app.db.db",
        "app.main.db",
        "app.retention.retention_engine.db",
        "app.worker.queue.db",
        "app.api.endpoints.actions.db",
    ):
        monkeypatch.setattr(target, temp_db)
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


def _add_channel(db: Database, channel_id: str = CHANNEL_ID, library: str = "default") -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO channels (id, name, url, folder_name, library) VALUES (?, ?, ?, ?, ?)",
            (channel_id, f"Synth {channel_id[-4:]}", "https://youtube.invalid/c", "Synth", library),
        )


def _add_video(
    db: Database,
    video_id: str,
    title: str = "",
    channel_id: str = CHANNEL_ID,
    published_at: str = "2024-03-15T00:00:00Z",
    file_size: int = 0,
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO videos (id, channel_id, title, published_at, duration, file_size, status)
            VALUES (?, ?, ?, ?, 60, ?, 'completed')
            """,
            (video_id, channel_id, title or f"Synthetic {video_id}", published_at, file_size),
        )


def _add_job(
    db: Database,
    job_id: str,
    job_type: str = "download_video",
    payload: str = "{}",
    status: str = "pending",
    error_message: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, type, payload, status, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, job_type, payload, status, error_message, created_at),
        )


def _job_status(db: Database, job_id: str) -> str:
    with db.connection() as conn:
        return conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


class _Collector(HTMLParser):
    """Collects nav links, buttons and notice texts from rendered HTML."""

    VOID = {"input", "img", "br", "hr", "meta", "link", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nav_links: list[dict[str, str]] = []
        self.buttons: list[dict[str, Any]] = []
        self.notices: list[str] = []
        self._button: dict[str, Any] | None = None
        self._nav: dict[str, str] | None = None
        self._notice_depth = 0
        self._notice_text: list[str] = []
        self.has_html_tag = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "html":
            self.has_html_tag = True
        classes = a.get("class", "").split()
        if tag == "a" and "nav-link" in classes:
            self._nav = {"href": a.get("href", ""), "text": ""}
        if tag == "button":
            self._button = {"attrs": a, "text": ""}
        if tag in self.VOID:
            return
        if self._notice_depth:
            self._notice_depth += 1
        elif "flash" in classes or a.get("role") in ("status", "alert"):
            self._notice_depth = 1
            self._notice_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._nav is not None:
            self.nav_links.append(self._nav)
            self._nav = None
        if tag == "button" and self._button is not None:
            self.buttons.append(self._button)
            self._button = None
        if tag in self.VOID:
            return
        if self._notice_depth:
            self._notice_depth -= 1
            if not self._notice_depth:
                self.notices.append(" ".join("".join(self._notice_text).split()))

    def handle_data(self, data: str) -> None:
        if self._nav is not None:
            self._nav["text"] += data
        if self._button is not None:
            self._button["text"] += data
        if self._notice_depth:
            self._notice_text.append(data)


def _parse(html: str) -> _Collector:
    parser = _Collector()
    parser.feed(html)
    return parser


async def _max_loop_gap(coro_factory: Any) -> float:
    loop = asyncio.get_running_loop()
    max_gap = 0.0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal max_gap
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            current = loop.time()
            max_gap = max(max_gap, current - last)
            last = current

    tick_task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    await coro_factory()
    stop.set()
    await tick_task
    return max_gap


# --- F1: pin/delete keep the channel context ------------------------------------------


@pytest.mark.xfail(strict=True, reason="F1: pin/delete redirect loses channel context")
@pytest.mark.parametrize("action", ["pin", "delete"])
@pytest.mark.parametrize("lib", [None, "kids"])
def test_f1_video_action_redirects_back_to_channel(
    client: TestClient, temp_db: Database, action: str, lib: str | None
) -> None:
    _add_channel(temp_db, library=lib or "default")
    _add_video(temp_db, "vidF1000001")

    data = {"video_id": "vidF1000001"}
    if action == "pin":
        data["is_pinned"] = "1"
    if lib:
        data["lib"] = lib
    response = client.post(f"/api/videos/{action}", data=data, follow_redirects=False)

    assert response.status_code in (302, 303)
    location = response.headers["location"]
    assert f"channel_id={CHANNEL_ID}" in location
    if lib:
        assert f"lib={lib}" in location


# --- F2: one source of truth for the library ------------------------------------------


@pytest.mark.xfail(strict=True, reason="F2: nav links drop the active lib")
@pytest.mark.parametrize("path", ["/", "/browser", "/subscriptions"])
def test_f2_nav_links_carry_active_library(client: TestClient, path: str) -> None:
    response = client.get(f"{path}?lib=kids")
    assert response.status_code == 200
    links = {link["href"].split("?")[0]: link["href"] for link in _parse(response.text).nav_links}
    assert {"/", "/browser", "/subscriptions"} <= set(links)
    for href in links.values():
        assert "lib=kids" in href, href


@pytest.mark.xfail(strict=True, reason="F2: app.js keeps the library in localStorage")
def test_f2_app_js_does_not_use_local_storage() -> None:
    assert "localStorage" not in APP_JS.read_text(encoding="utf-8")


# --- F3: queue with errors, retry/cancel, polling partial, internal jobs hidden -------


@pytest.mark.xfail(strict=True, reason="F3: failed job error_message not shown")
def test_f3_failed_job_shows_error_message(client: TestClient, temp_db: Database) -> None:
    _add_job(
        temp_db,
        "job_f3failed",
        payload='{"url": "https://www.youtube.com/watch?v=synthF3fail"}',
        status="failed",
        error_message="Synthetic failure F3-XYZ",
    )
    response = client.get("/")
    assert response.status_code == 200
    assert "Synthetic failure F3-XYZ" in response.text


@pytest.mark.xfail(strict=True, reason="F3: no retry endpoint")
def test_f3_retry_sets_failed_job_pending(client: TestClient, temp_db: Database) -> None:
    _add_job(temp_db, "job_f3retry", status="failed", error_message="boom")
    response = client.post("/api/jobs/job_f3retry/retry", follow_redirects=False)
    assert response.status_code < 400, response.status_code
    assert _job_status(temp_db, "job_f3retry") == "pending"


@pytest.mark.xfail(strict=True, reason="F3: no cancel endpoint")
def test_f3_cancel_pending_job(client: TestClient, temp_db: Database) -> None:
    _add_job(temp_db, "job_f3cancel", status="pending")
    response = client.post("/api/jobs/job_f3cancel/cancel", follow_redirects=False)
    assert response.status_code < 400, response.status_code
    assert _job_status(temp_db, "job_f3cancel") == "cancelled"


@pytest.mark.xfail(strict=True, reason="F3: no queue partial for HTMX polling")
def test_f3_queue_partial_is_fragment(client: TestClient, temp_db: Database) -> None:
    _add_job(
        temp_db, "job_f3partial", payload='{"url": "https://www.youtube.com/watch?v=synthF3p"}'
    )
    response = client.get("/partials/queue")
    assert response.status_code == 200, response.status_code
    assert "synthF3p" in response.text
    assert not _parse(response.text).has_html_tag


@pytest.mark.xfail(strict=True, reason="F3: internal jobs listed as downloads")
def test_f3_internal_jobs_not_listed(client: TestClient, temp_db: Database) -> None:
    _add_job(temp_db, "job_f3pollchan", job_type="poll_channels")
    _add_job(temp_db, "job_f3retention", job_type="retention")
    _add_job(
        temp_db, "job_f3download", payload='{"url": "https://www.youtube.com/watch?v=synthF3d"}'
    )
    response = client.get("/")
    assert response.status_code == 200
    assert "synthF3d" in response.text
    assert "job_f3pollchan" not in response.text
    assert "job_f3retention" not in response.text


# --- F4: feedback after actions -------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="F4: no notice after import with 0 channels")
def test_f4_import_without_channels_shows_notice(client: TestClient) -> None:
    response = client.post(
        "/api/subscriptions/import",
        data={"library": "default"},
        files={"subscriptions_file": ("empty.opml", b"<opml><body></body></opml>", "text/xml")},
    )
    assert response.status_code == 200
    notices = _parse(response.text).notices
    assert any("0" in n for n in notices), notices


@pytest.mark.xfail(strict=True, reason="F4: unresolvable subscription fails silently")
def test_f4_unresolvable_subscription_shows_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(channel_input: str) -> tuple[str, str]:
        raise ValueError("synthetic: channel not found")

    monkeypatch.setattr("app.api.endpoints.actions.resolve_channel", fail)
    response = client.post(
        "/api/subscriptions", data={"channel_input": "@synthNoSuchChannel", "library": "default"}
    )
    assert response.status_code == 200
    notices = [n for n in _parse(response.text).notices if n]
    assert notices, "no error notice shown"


@pytest.mark.xfail(strict=True, reason="F4: TA-API import blocks the event loop")
def test_f4_ta_import_does_not_block_event_loop(
    temp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main as main_module

    monkeypatch.setattr("app.api.endpoints.actions.db", temp_db)

    def slow_get(self: httpx.Client, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        time.sleep(0.3)  # simulate a slow TubeArchivist instance
        return httpx.Response(
            200, json={"data": [], "paginate": {}}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.Client, "get", slow_get)

    def override_get_db():
        with temp_db.connection() as conn:
            yield conn

    main_module.app.dependency_overrides[get_db] = override_get_db

    async def do_import() -> None:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post(
                "/api/subscriptions/import",
                data={"ta_url": "http://ta.invalid", "ta_token": "synthetic", "library": "default"},
            )

    try:
        max_gap = asyncio.run(_max_loop_gap(do_import))
    finally:
        main_module.app.dependency_overrides.clear()
    assert max_gap < 0.15, f"event loop was blocked for {max_gap:.2f}s"


# --- F5: subscriptions are editable ---------------------------------------------------


@pytest.mark.xfail(strict=True, reason="F5: no subscription edit endpoint")
def test_f5_update_subscription(client: TestClient, temp_db: Database) -> None:
    _add_channel(temp_db)
    with temp_db.connection() as conn:
        conn.execute(
            "UPDATE channels SET retention_count = 3, retention_days = 7 WHERE id = ?",
            (CHANNEL_ID,),
        )

    response = client.post(
        "/api/subscriptions/update",
        data={
            "channel_id": CHANNEL_ID,
            "library": "kids",
            "retention_count": "5",
            "retention_days": "0",
            "auto_download": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code < 400, response.status_code
    with temp_db.connection() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (CHANNEL_ID,)).fetchone()
    assert row["library"] == "kids"
    assert row["retention_count"] == 5
    assert row["retention_days"] is None
    assert row["auto_download"] == 0


# --- F6: nav label matches the structure ----------------------------------------------


@pytest.mark.xfail(strict=True, reason="F6: nav says 'Abos & Queue', queue is on /")
def test_f6_nav_label_without_queue(client: TestClient) -> None:
    response = client.get("/")
    texts = [" ".join(link["text"].split()) for link in _parse(response.text).nav_links]
    assert texts
    assert not any("Queue" in t for t in texts), texts


# --- F7: accessibility & no inline styles ---------------------------------------------


@pytest.mark.xfail(strict=True, reason="F7: icon-only buttons without aria-label")
@pytest.mark.parametrize("path", [f"/browser?channel_id={CHANNEL_ID}", "/subscriptions"])
def test_f7_icon_buttons_have_aria_label(client: TestClient, temp_db: Database, path: str) -> None:
    _add_channel(temp_db)
    _add_video(temp_db, "vidF7000001")
    response = client.get(path)
    assert response.status_code == 200
    buttons = _parse(response.text).buttons
    icon_only = [b for b in buttons if not any(c.isalnum() for c in b["text"])]
    assert icon_only, "fixture should render at least one icon-only button"
    missing = [b["text"].strip() for b in icon_only if not b["attrs"].get("aria-label")]
    assert not missing, missing


@pytest.mark.xfail(strict=True, reason="F7: inline styles in templates")
def test_f7_no_inline_styles_in_templates() -> None:
    offenders = {}
    for template in sorted(TEMPLATES.rglob("*.html")):
        count = len(re.findall(r'style="(?!width:)', template.read_text(encoding="utf-8")))
        if count:
            offenders[template.name] = count
    assert not offenders, offenders


# --- F8: browser search, pagination, size ---------------------------------------------


@pytest.mark.xfail(strict=True, reason="F8: no title search in browser")
def test_f8_browser_search_filters_titles(client: TestClient, temp_db: Database) -> None:
    _add_channel(temp_db)
    _add_video(temp_db, "vidF8s00001", title="Alpha foo episode")
    _add_video(temp_db, "vidF8s00002", title="Beta bar episode")
    response = client.get(f"/browser?channel_id={CHANNEL_ID}&q=foo")
    assert response.status_code == 200
    assert "Alpha foo episode" in response.text
    assert "Beta bar episode" not in response.text


@pytest.mark.xfail(strict=True, reason="F8: no pagination in browser")
def test_f8_browser_pagination_page_two(client: TestClient, temp_db: Database) -> None:
    _add_channel(temp_db)
    for i in range(1, 31):  # vidF8p01 oldest ... vidF8p30 newest
        _add_video(temp_db, f"vidF8p{i:02d}", published_at=f"2024-01-{i:02d}T00:00:00Z")
    response = client.get(f"/browser?channel_id={CHANNEL_ID}&page=2")
    assert response.status_code == 200
    shown = set(re.findall(r"vidF8p\d\d", response.text))
    assert shown == {f"vidF8p{i:02d}" for i in range(1, 7)}, sorted(shown)


@pytest.mark.xfail(strict=True, reason="F8: no size per channel")
def test_f8_channel_grid_shows_size(client: TestClient, temp_db: Database) -> None:
    _add_channel(temp_db)
    _add_video(temp_db, "vidF8z00001", file_size=100 * 1024 * 1024)
    _add_video(temp_db, "vidF8z00002", file_size=50 * 1024 * 1024)
    response = client.get("/browser")
    assert response.status_code == 200
    assert re.search(r"\d+(?:[.,]\d+)?\s*(?:MB|MiB|GB|GiB)", response.text)


# --- G1/G2: home view queries -----------------------------------------------------------


def test_g1_home_does_not_reload_each_job(
    client: TestClient, temp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        _add_job(temp_db, f"job_g1_{i}", payload='{"url": "https://youtube.invalid/g1"}')
    calls: list[str] = []
    original = JobQueue.get_job

    def counting(self: JobQueue, job_id: str):
        calls.append(job_id)
        return original(self, job_id)

    monkeypatch.setattr(JobQueue, "get_job", counting)
    response = client.get("/")
    assert response.status_code == 200
    assert calls == []


@pytest.mark.xfail(strict=True, reason="G2: library filter applied after LIMIT 20")
def test_g2_library_filter_before_limit(client: TestClient, temp_db: Database) -> None:
    _add_job(
        temp_db,
        "job_g2_kids",
        payload='{"url": "https://www.youtube.com/watch?v=synthG2kids", "library": "kids"}',
        created_at="2026-01-01T00:00:00Z",
    )
    for i in range(25):
        _add_job(
            temp_db,
            f"job_g2_def_{i:02d}",
            payload='{"url": "https://www.youtube.com/watch?v=synthG2def", "library": "default"}',
            created_at=f"2026-02-01T00:00:{i:02d}Z",
        )
    response = client.get("/?lib=kids")
    assert response.status_code == 200
    assert "synthG2kids" in response.text


# --- G3: unused get_db dependencies ---------------------------------------------------


@pytest.mark.xfail(strict=True, reason="G3: endpoints open unused DB connections")
def test_g3_no_unused_get_db_dependencies() -> None:
    unused = []
    for module in ("actions.py", "views.py"):
        tree = ast.parse((ROOT / "app" / "api" / "endpoints" / module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            if "db_conn" not in [a.arg for a in node.args.args + node.args.kwonlyargs]:
                continue
            used = any(
                isinstance(n, ast.Name) and n.id == "db_conn"
                for stmt in node.body
                for n in ast.walk(stmt)
            )
            if not used:
                unused.append(f"{module}:{node.name}")
    assert not unused, unused


# --- G4: % in titles must not be interpolated by yt-dlp -------------------------------

_RealYoutubeDL = yt_dlp.YoutubeDL


class _RealFilenameYDL(FakeYoutubeDL):
    """Fake YoutubeDL whose prepare_filename uses real yt-dlp outtmpl semantics."""

    def prepare_filename(self, info: dict, *args: Any, **kwargs: Any) -> str:
        real = _RealYoutubeDL({"outtmpl": self.params.get("outtmpl"), "quiet": True})
        return real.prepare_filename(info)


@pytest.mark.xfail(strict=True, reason="G4: % in title interpolated by yt-dlp outtmpl")
def test_g4_percent_in_title_kept_literally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_db: Database
) -> None:
    fake = type(
        "FakeYDLG4", (_RealFilenameYDL,), {"infos": {}, "extract_calls": [], "process_calls": []}
    )
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(FAKE_JPEG))
    monkeypatch.setattr(httpx.Client, "get", lambda self, *a, **k: _FakeResponse(FAKE_JPEG))
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _FakeResponse(FAKE_JPEG))
    media_root = tmp_path / "media"
    monkeypatch.setattr(config.settings, "media_dir", media_root)
    monkeypatch.setattr(config.settings, "cookies_path", tmp_path / "no-cookies.txt")

    title = "a%%b %(id)s"
    fake.infos["synthG4vid1"] = _video_info("synthG4vid1", title=title)

    queue = JobQueue(database=temp_db)
    worker = BackgroundWorker(
        queue=queue,
        downloader_service=YtDlpService(),
        layout=JellyfinLayout(media_root=media_root),
        nfo=NfoGenerator(),
        database=temp_db,
    )
    job = queue.enqueue("download_video", {"video_id": "synthG4vid1"})
    assert job is not None
    asyncio.run(worker.process_next_job())

    names = [p.name for p in media_root.rglob("*.mp4")]
    assert any(title in n for n in names), names
