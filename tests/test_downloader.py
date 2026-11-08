"""Tests for yt-dlp execution service and download pipeline."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from yt_dlp.utils import DownloadError, ExtractorError

from app.downloader import (
    CookieError,
    DownloadFailedError,
    DownloadOptions,
    HttpPOTokenProvider,
    POTokenConfig,
    ProgressUpdate,
    VideoUnavailableError,
    YtDlpService,
    parse_metadata,
    wrap_progress_hook,
)


@pytest.fixture
def service() -> YtDlpService:
    return YtDlpService()


def test_build_ydl_opts_defaults(service: YtDlpService) -> None:
    """Verify default ydl_opts construction."""
    options = DownloadOptions(output_dir="/media/youtube")
    opts = service.build_ydl_opts(options)

    assert "/media/youtube" in opts["outtmpl"]["default"]
    assert "-thumb" in opts["outtmpl"]["thumbnail"]
    assert opts["writesubtitles"] is True
    assert opts["subtitlesformat"] == "vtt"
    assert opts["subtitleslangs"] == ["en", "de"]
    assert opts["writethumbnail"] is True
    assert opts["format"] == "bestvideo*+bestaudio/best"
    assert opts["merge_output_format"] == "mp4"


def test_build_ydl_opts_rate_limit_and_sleep(service: YtDlpService) -> None:
    """Verify rate-limiting and sleep interval parameters."""
    options = DownloadOptions(
        rate_limit_bytes=2_000_000,
        sleep_interval=2.5,
        max_sleep_interval=5.0,
        sleep_requests=1.0,
    )
    opts = service.build_ydl_opts(options)

    assert opts["ratelimit"] == 2_000_000
    assert opts["sleep_interval"] == 2.5
    assert opts["max_sleep_interval"] == 5.0
    assert opts["sleep_interval_requests"] == 1.0


def test_build_ydl_opts_cookies(service: YtDlpService, tmp_path: Path) -> None:
    """Verify cookiefile integration when file exists."""
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t123\n"
    )

    options = DownloadOptions(cookies_path=cookies_file)
    opts = service.build_ydl_opts(options)
    assert opts["cookiefile"] == str(cookies_file)

    # When cookies file does not exist, it should not set cookiefile
    non_existent = tmp_path / "missing.txt"
    options_missing = DownloadOptions(cookies_path=non_existent)
    opts_missing = service.build_ydl_opts(options_missing)
    assert "cookiefile" not in opts_missing


def test_build_ydl_opts_po_token(service: YtDlpService) -> None:
    """Verify static and dynamic PO-token configuration."""
    # Static token
    options_static = DownloadOptions(
        po_token_config=POTokenConfig(
            token="static_token_xyz",
            visitor_data="visitor_123",
        )
    )
    opts = service.build_ydl_opts(options_static)
    yt_args = opts["extractor_args"]["youtube"]
    assert yt_args["po_token"] == ["web+static_token_xyz"]
    assert yt_args["visitor_data"] == ["visitor_123"]

    # Dynamic provider
    options_dynamic = DownloadOptions(
        po_token_config=POTokenConfig(
            provider=lambda: ("web+dynamic_token", "vis_dyn"),
        )
    )
    opts_dyn = service.build_ydl_opts(options_dynamic)
    yt_args_dyn = opts_dyn["extractor_args"]["youtube"]
    assert yt_args_dyn["po_token"] == ["web+dynamic_token"]
    assert yt_args_dyn["visitor_data"] == ["vis_dyn"]


def test_http_po_token_provider() -> None:
    """Test HTTP-based PO-token provider."""

    def custom_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={"po_token": "http_token_abc", "visitor_data": "visitor_xyz"},
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(custom_handler))
    provider = HttpPOTokenProvider("http://provider.local/token", client=client)

    result = provider.get_token()
    assert result == ("http_token_abc", "visitor_xyz")

    # Callable invocation
    assert provider() == ("http_token_abc", "visitor_xyz")

    # Error handling
    failing_client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Error"))
    )
    failing_provider = HttpPOTokenProvider("http://provider.local/token", client=failing_client)
    assert failing_provider.get_token() is None


def test_parse_metadata() -> None:
    """Test metadata normalization from yt-dlp raw info dict."""
    raw = {
        "id": "dQw4w9WgXcQ",
        "title": "Never Gonna Give You Up",
        "uploader": "Rick Astley",
        "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "upload_date": "20091025",
        "duration": 213,
        "description": "The official video",
        "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        "filesize_approx": 10485760,
        "tags": ["Rick Astley", "Pop"],
    }

    meta = parse_metadata(raw)
    assert meta.id == "dQw4w9WgXcQ"
    assert meta.title == "Never Gonna Give You Up"
    assert meta.channel == "Rick Astley"
    assert meta.channel_id == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert meta.upload_date == "2009-10-25"
    assert meta.duration == 213
    assert meta.description == "The official video"
    assert meta.filesize_approx == 10485760
    assert "Pop" in meta.tags


def test_wrap_progress_hook() -> None:
    """Test progress hook translation to ProgressUpdate."""
    updates: list[ProgressUpdate] = []
    hook = wrap_progress_hook(lambda u: updates.append(u))

    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 5000000,
            "total_bytes": 10000000,
            "speed": 1048576.0,
            "eta": 5,
            "filename": "/media/test.mp4",
        }
    )

    assert len(updates) == 1
    update = updates[0]
    assert update.status == "downloading"
    assert update.downloaded_bytes == 5000000
    assert update.total_bytes == 10000000
    assert update.percentage == 50.0
    assert update.speed_bytes_per_sec == 1048576.0
    assert update.eta_seconds == 5
    assert update.filename == "/media/test.mp4"


def test_extract_info_success(service: YtDlpService) -> None:
    """Test metadata extraction with mocked YoutubeDL."""
    mock_info = {
        "id": "vid123",
        "title": "Sample Video",
        "uploader": "Sample Uploader",
        "channel_id": "UCsample",
        "upload_date": "20240101",
        "duration": 60,
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl_cls.return_value = mock_ydl

        meta = service.extract_info("https://youtube.com/watch?v=vid123")
        assert meta.id == "vid123"
        assert meta.title == "Sample Video"
        assert meta.upload_date == "2024-01-01"


def test_extract_info_errors(service: YtDlpService) -> None:
    """Test error handling in extract_info."""
    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl_cls.return_value = mock_ydl

        # Video unavailable
        mock_ydl.extract_info.side_effect = DownloadError(
            "This video has been removed by the uploader"
        )
        with pytest.raises(VideoUnavailableError):
            service.extract_info("https://youtube.com/watch?v=deleted")

        # Cookie failure
        mock_ydl.extract_info.side_effect = DownloadError("Please pass --cookies to bypass login")
        with pytest.raises(CookieError):
            service.extract_info("https://youtube.com/watch?v=cookie_req")

        # General extractor error
        mock_ydl.extract_info.side_effect = ExtractorError("Failed to parse page")
        with pytest.raises(DownloadFailedError):
            service.extract_info("https://youtube.com/watch?v=broken")


def test_download_pipeline(service: YtDlpService, tmp_path: Path) -> None:
    """Test complete download pipeline artifact resolution."""
    video_dir = tmp_path / "Channel Name" / "Season 2026"
    video_dir.mkdir(parents=True)
    video_file = video_dir / "2026-11-08 - Test Video [test_id].mp4"
    video_file.write_bytes(b"dummy video data")

    thumb_file = video_dir / "2026-11-08 - Test Video [test_id]-thumb.jpg"
    thumb_file.write_bytes(b"dummy thumb data")

    sub_file_en = video_dir / "2026-11-08 - Test Video [test_id].en.vtt"
    sub_file_en.write_text("WEBVTT\n")
    sub_file_de = video_dir / "2026-11-08 - Test Video [test_id].de.vtt"
    sub_file_de.write_text("WEBVTT\n")

    mock_info = {
        "id": "test_id",
        "title": "Test Video",
        "uploader": "Channel Name",
        "upload_date": "20261108",
        "duration": 120,
        "requested_downloads": [{"filepath": str(video_file)}],
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl_cls.return_value = mock_ydl

        options = DownloadOptions(output_dir=tmp_path)
        result = service.download("https://youtube.com/watch?v=test_id", options=options)

        assert result.id == "test_id"
        assert result.video_path == video_file
        assert result.thumbnail_path == thumb_file
        assert len(result.subtitle_paths) == 2
        assert sub_file_en in result.subtitle_paths
        assert sub_file_de in result.subtitle_paths
        assert result.file_size == len(b"dummy video data")


def test_async_methods(service: YtDlpService) -> None:
    """Test async wrappers."""
    mock_info = {
        "id": "async123",
        "title": "Async Test",
        "uploader": "Async Channel",
        "upload_date": "20261108",
        "duration": 10,
    }

    with patch("yt_dlp.YoutubeDL") as mock_ydl_cls:
        mock_ydl = MagicMock()
        mock_ydl.__enter__.return_value = mock_ydl
        mock_ydl.extract_info.return_value = mock_info
        mock_ydl_cls.return_value = mock_ydl

        meta = asyncio.run(service.async_extract_info("https://youtube.com/watch?v=async123"))
        assert meta.id == "async123"
        assert meta.title == "Async Test"


def test_import_and_validate_cookies(tmp_path: Path) -> None:
    """Test cookies import and file validation."""
    dest = tmp_path / "data" / "cookies.txt"
    sample_content = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\taf6s7d8f\n"
    )

    path = YtDlpService.import_cookies(sample_content, dest_path=dest)
    assert path.is_file()
    assert YtDlpService.validate_cookies_file(path) is True

    # Empty content should fail
    with pytest.raises(CookieError):
        YtDlpService.import_cookies("   \n", dest_path=tmp_path / "empty.txt")

    # Non-existent file
    assert YtDlpService.validate_cookies_file(tmp_path / "does_not_exist.txt") is False
