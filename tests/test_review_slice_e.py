"""Review slice E: reproduce finding E1 (TubeArchivist migration without .info.json).

TubeArchivist stores media as ``<source>/<channel_id>/<video_id>.mp4`` without any
companion ``.info.json``; the metadata lives in Elasticsearch / the TA REST API.
The migrator must not invent ID-named folders/titles with an mtime date for such files.

All data is synthetic, everything runs in temp dirs, the TA API is mocked (no network).
"""

import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.db import Database
from app.migration.tubearchivist import TubeArchivistMigrator

CHANNEL_ID = "UCsynthE0000000000000001"
# 11 characters like a real YouTube id, so id validation in a fix stays possible.
VIDEO_ID = "synthE1vid1"
CHANNEL_NAME = "Synth E Channel"
TITLE = "Synthetic Episode"
TA_URL = "http://ta.invalid:8000"
TA_TOKEN = "synthetic-test-token"  # placeholder, not a real secret

EXPECTED_REL = (
    Path("default") / CHANNEL_NAME / "Season 2024" / f"2024-03-15 - {TITLE} [{VIDEO_ID}].mp4"
)


@pytest.fixture
def test_db(tmp_path: Path) -> Database:
    db_instance = Database(tmp_path / "slice_e.db")
    db_instance.run_migrations()
    return db_instance


@pytest.fixture
def ta_source(tmp_path: Path) -> Path:
    """TubeArchivist-typical tree: <source>/youtube/<channel_id>/<video_id>.mp4, no companions."""
    source = tmp_path / "youtube"
    channel_dir = source / CHANNEL_ID
    channel_dir.mkdir(parents=True)
    (channel_dir / f"{VIDEO_ID}.mp4").write_bytes(b"synthetic video bytes")
    return source


def _video_doc(published: str) -> dict:
    return {
        "youtube_id": VIDEO_ID,
        "title": TITLE,
        "published": published,
        "description": "Synthetic description for slice E.",
        "media_url": f"/media/{CHANNEL_ID}/{VIDEO_ID}.mp4",
        "channel": {"channel_id": CHANNEL_ID, "channel_name": CHANNEL_NAME},
        "player": {"duration": 321},
    }


@pytest.fixture
def mock_ta_api(request: pytest.FixtureRequest) -> Iterator[list[httpx.Request]]:
    """Mock the TubeArchivist REST API at the transport level.

    Patches ``httpx.Client.send`` (which ``Client.get``/``request`` and the module-level
    ``httpx.get`` all go through), so a fix is free to use any sync httpx call style.
    Serves TA's REST format:
      GET /api/video/?page=N   -> {"data": [...], "paginate": {"last_page": 1}}
      GET /api/video/<id>/     -> {"data": {...}}
      GET /api/channel/?page=N -> {"data": [...], "paginate": {"last_page": 1}}
      GET /api/channel/<id>/   -> {"data": {...}}
    """
    published = getattr(request, "param", "2024-03-15")
    video = _video_doc(published)
    channel = {
        "channel_id": CHANNEL_ID,
        "channel_name": CHANNEL_NAME,
        "channel_subscribed": False,
        "channel_description": "Synthetic channel.",
    }
    calls: list[httpx.Request] = []

    def fake_send(self: httpx.Client, req: httpx.Request, *args: object, **kwargs: object):
        calls.append(req)
        path = req.url.path.rstrip("/")
        if not str(req.url).startswith(TA_URL):
            raise AssertionError(f"unexpected network call: {req.url}")
        if req.headers.get("Authorization") != f"Token {TA_TOKEN}":
            return httpx.Response(403, json={"detail": "forbidden"}, request=req)
        if path == "/api/video":
            body = {"data": [video], "paginate": {"last_page": 1}}
        elif path == f"/api/video/{VIDEO_ID}":
            body = {"data": video}
        elif path == "/api/channel":
            body = {"data": [channel], "paginate": {"last_page": 1}}
        elif path == f"/api/channel/{CHANNEL_ID}":
            body = {"data": channel}
        else:
            return httpx.Response(404, json={"detail": "not found"}, request=req)
        return httpx.Response(200, json=body, request=req)

    with patch.object(httpx.Client, "send", fake_send):
        yield calls


def _all_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_e1_without_metadata_video_is_skipped_and_reported(
    ta_source: Path, tmp_path: Path, test_db: Database
) -> None:
    """Without .info.json and without TA API access the video must be skipped + reported,
    never migrated as ``<channel_id>/Season <mtime-year>/<date> - <id> [<id>].mp4``."""
    dest = tmp_path / "media"
    result = TubeArchivistMigrator(
        source_dir=ta_source,
        dest_media_root=dest,
        mode="copy",
        dry_run=False,
        database=test_db,
    ).migrate()

    files = _all_files(dest)
    assert not any(CHANNEL_ID in p.relative_to(dest).parts for p in files), files
    assert not any(f"- {VIDEO_ID} [{VIDEO_ID}]" in p.name for p in files), files
    assert not any(p.suffix in {".mp4", ".nfo"} for p in files), files

    assert result.migrated_count == 0
    assert result.skipped_count == 1
    assert any(VIDEO_ID in e for e in result.errors), result.errors

    with test_db.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE id = ? AND status = 'completed'", (VIDEO_ID,)
        ).fetchone()
        assert row[0] == 0
    # Source stays untouched in copy mode.
    assert (ta_source / CHANNEL_ID / f"{VIDEO_ID}.mp4").is_file()


def _assert_migrated_with_ta_metadata(dest: Path, test_db: Database) -> None:
    video = dest / EXPECTED_REL
    assert video.is_file(), sorted(str(p.relative_to(dest)) for p in _all_files(dest))
    assert video.read_bytes() == b"synthetic video bytes"

    nfo = video.with_suffix(".nfo")
    assert nfo.is_file()
    nfo_text = nfo.read_text(encoding="utf-8")
    assert f"<title>{TITLE}</title>" in nfo_text
    assert "<aired>2024-03-15</aired>" in nfo_text
    assert (dest / "default" / CHANNEL_NAME / "tvshow.nfo").is_file()
    assert not (dest / "default" / CHANNEL_ID).exists()

    with test_db.connection() as conn:
        v = conn.execute("SELECT * FROM videos WHERE id = ?", (VIDEO_ID,)).fetchone()
        assert v is not None
        assert v["title"] == TITLE
        assert v["channel_id"] == CHANNEL_ID
        assert str(v["published_at"]).startswith("2024-03-15")
        assert v["duration"] == 321
        assert v["file_path"] == str(video)
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (CHANNEL_ID,)).fetchone()
        assert ch is not None
        assert ch["name"] == CHANNEL_NAME


@pytest.mark.parametrize(
    "mock_ta_api",
    ["2024-03-15", "2024-03-15T12:34:56+00:00"],
    ids=["date", "iso-datetime"],
    indirect=True,
)
def test_e1_metadata_from_ta_api(
    ta_source: Path, tmp_path: Path, test_db: Database, mock_ta_api: list[httpx.Request]
) -> None:
    """With TA credentials the migrator takes title/channel/date from the TA REST API.

    API assumption: ``TubeArchivistMigrator`` accepts the keyword arguments ``ta_url`` and
    ``ta_token`` and queries ``<ta_url>/api/video/`` (paginated) and/or
    ``<ta_url>/api/video/<id>/`` with header ``Authorization: Token <ta_token>``.
    ``published`` may be a date or an ISO datetime depending on the TA version.
    """
    dest = tmp_path / "media"
    result = TubeArchivistMigrator(
        source_dir=ta_source,
        dest_media_root=dest,
        mode="copy",
        dry_run=False,
        database=test_db,
        ta_url=TA_URL,
        ta_token=TA_TOKEN,
    ).migrate()

    assert result.migrated_count == 1, result.errors
    assert result.failed_count == 0
    assert mock_ta_api, "TA API was never queried"
    _assert_migrated_with_ta_metadata(dest, test_db)


def test_e1_cli_uses_ta_credentials_for_media(
    ta_source: Path,
    tmp_path: Path,
    test_db: Database,
    mock_ta_api: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--source`` together with ``--ta-url/--ta-token`` uses the API for media metadata too."""
    from app.migration import tubearchivist

    dest = tmp_path / "media"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate-tubearchivist",
            "--source",
            str(ta_source),
            "--dest",
            str(dest),
            "--db",
            str(test_db.db_path),
            "--ta-url",
            TA_URL,
            "--ta-token",
            TA_TOKEN,
            "--mode",
            "copy",
        ],
    )
    tubearchivist.main()

    assert any(r.url.path.startswith("/api/video") for r in mock_ta_api), [
        str(r.url) for r in mock_ta_api
    ]
    _assert_migrated_with_ta_metadata(dest, test_db)
