"""yt-dlp execution service and video download pipeline."""

import asyncio
import glob
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from app.core.config import settings
from app.downloader.exceptions import (
    CookieError,
    DownloadFailedError,
    VideoUnavailableError,
    YtDlpError,
)
from app.downloader.options import (
    DownloadOptions,
    DownloadResult,
    ProgressUpdate,
    VideoMetadata,
)

logger = logging.getLogger(__name__)


def parse_metadata(info_dict: dict[str, Any]) -> VideoMetadata:
    """Extract and normalize VideoMetadata from a yt-dlp info dictionary."""
    upload_date_raw = str(info_dict.get("upload_date") or "")
    if len(upload_date_raw) == 8 and upload_date_raw.isdigit():
        formatted_date = f"{upload_date_raw[:4]}-{upload_date_raw[4:6]}-{upload_date_raw[6:8]}"
    else:
        formatted_date = upload_date_raw

    # Extract channel information
    channel_name = (
        info_dict.get("uploader")
        or info_dict.get("channel")
        or info_dict.get("uploader_id")
        or "Unknown Channel"
    )
    channel_id = info_dict.get("channel_id") or info_dict.get("uploader_id") or channel_name
    channel_url = (
        info_dict.get("channel_url")
        or info_dict.get("uploader_url")
        or f"https://www.youtube.com/channel/{channel_id}"
    )

    return VideoMetadata(
        id=str(info_dict.get("id") or ""),
        title=str(info_dict.get("title") or "Untitled Video"),
        channel=str(channel_name),
        channel_id=str(channel_id),
        channel_url=str(channel_url),
        upload_date=formatted_date,
        duration=int(info_dict.get("duration") or 0),
        description=str(info_dict.get("description") or ""),
        thumbnail_url=str(info_dict.get("thumbnail") or ""),
        webpage_url=str(
            info_dict.get("webpage_url") or f"https://www.youtube.com/watch?v={info_dict.get('id')}"
        ),
        filesize_approx=info_dict.get("filesize_approx") or info_dict.get("filesize"),
        tags=list(info_dict.get("tags") or []),
        raw_info=info_dict,
    )


def wrap_progress_hook(
    callback: Callable[[ProgressUpdate], None],
) -> Callable[[dict[str, Any]], None]:
    """Wrap a typed ProgressUpdate callback into yt-dlp's raw progress hook signature."""

    def _hook(d: dict[str, Any]) -> None:
        status = str(d.get("status", "unknown"))
        downloaded = int(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        total_int = int(total) if total is not None else None
        percentage = (downloaded / total_int * 100.0) if total_int and total_int > 0 else 0.0
        speed = float(d["speed"]) if d.get("speed") is not None else None
        eta = int(d["eta"]) if d.get("eta") is not None else None
        filename = d.get("filename")

        update = ProgressUpdate(
            status=status,
            downloaded_bytes=downloaded,
            total_bytes=total_int,
            percentage=round(percentage, 2),
            speed_bytes_per_sec=speed,
            eta_seconds=eta,
            filename=str(filename) if filename else None,
            raw=d,
        )
        callback(update)

    return _hook


class YtDlpService:
    """Service wrapping yt-dlp execution pipeline with robust configuration and error handling."""

    def __init__(self, default_options: DownloadOptions | None = None) -> None:
        self.default_options = default_options or DownloadOptions()

    def build_ydl_opts(
        self,
        options: DownloadOptions,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Translate high-level DownloadOptions into yt-dlp parameters."""
        # The directory is literal text (e.g. channel "100% Wolf"): escape % for outtmpl
        output_dir = Path(str(options.output_dir).replace("%", "%%"))
        base_template = options.output_template

        # Compute template names for video, thumbnail (-thumb.jpg), and subtitle
        if ".%(ext)s" in base_template:
            thumb_template = base_template.replace(
                ".%(ext)s", f"{options.thumbnail_suffix}.%(ext)s"
            )
            sub_template = base_template.replace(".%(ext)s", ".%(ext)s")
        else:
            thumb_template = f"{base_template}{options.thumbnail_suffix}.%(ext)s"
            sub_template = f"{base_template}.%(ext)s"

        outtmpl = {
            "default": str(output_dir / base_template),
            "thumbnail": str(output_dir / thumb_template),
            "subtitle": str(output_dir / sub_template),
        }

        # Format selector with optional max_height constraint
        format_str = options.format_selector
        if options.max_height:
            h = options.max_height
            format_str = f"bestvideo*[height<={h}]+bestaudio/best[height<={h}]/best"

        ydl_opts: dict[str, Any] = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "format": format_str,
            "merge_output_format": options.merge_output_format,
        }

        # Format Sort (--format-sort)
        if options.format_sort:
            if isinstance(options.format_sort, str):
                sort_list = [s.strip() for s in options.format_sort.split(",") if s.strip()]
                ydl_opts["format_sort"] = sort_list
            else:
                ydl_opts["format_sort"] = options.format_sort

        # SponsorBlock integration
        if options.sponsorblock_remove:
            ydl_opts["sponsorblock_remove"] = options.sponsorblock_remove
        if options.sponsorblock_mark:
            ydl_opts["sponsorblock_mark"] = options.sponsorblock_mark

        # Subtitle settings (.vtt)
        if options.download_subtitles:
            ydl_opts["writesubtitles"] = True
            ydl_opts["writeautomaticsub"] = options.write_auto_sub
            ydl_opts["subtitlesformat"] = options.subtitles_format
            ydl_opts["subtitleslangs"] = options.subtitles_langs

        # Thumbnail settings
        if options.write_thumbnail:
            ydl_opts["writethumbnail"] = True

        # FFmpeg postprocessors if ffmpeg is available
        ffmpeg_bin = options.ffmpeg_path or shutil.which("ffmpeg")
        if ffmpeg_bin:
            ydl_opts["ffmpeg_location"] = str(ffmpeg_bin)

        postprocessors: list[dict[str, Any]] = []
        if options.write_thumbnail and ffmpeg_bin and options.convert_thumbnail:
            postprocessors.append(
                {
                    "key": "FFmpegThumbnailsConvertor",
                    "format": options.convert_thumbnail,
                    "when": "before_dl",
                }
            )
        if options.download_subtitles and ffmpeg_bin and options.subtitles_format == "vtt":
            postprocessors.append(
                {
                    "key": "FFmpegSubtitlesConvertor",
                    "format": "vtt",
                }
            )
        if postprocessors:
            ydl_opts["postprocessors"] = postprocessors

        # Rate limiting & sleep intervals
        if options.rate_limit_bytes is not None:
            ydl_opts["ratelimit"] = options.rate_limit_bytes
        if options.throttled_rate_bytes is not None:
            ydl_opts["throttledratelimit"] = options.throttled_rate_bytes
        if options.sleep_interval is not None:
            ydl_opts["sleep_interval"] = options.sleep_interval
        if options.max_sleep_interval is not None:
            ydl_opts["max_sleep_interval"] = options.max_sleep_interval
        if options.sleep_requests is not None:
            ydl_opts["sleep_interval_requests"] = options.sleep_requests

        # Cookies
        if options.cookies_path:
            cookies_file = Path(options.cookies_path)
            if cookies_file.is_file():
                ydl_opts["cookiefile"] = str(cookies_file)
            else:
                logger.warning("Configured cookies path does not exist: %s", cookies_file)

        # PO-token provider / configuration
        if options.po_token_config:
            cfg = options.po_token_config
            token = cfg.token
            visitor_data = cfg.visitor_data

            if cfg.provider:
                try:
                    result = cfg.provider()
                    if isinstance(result, tuple):
                        token = result[0] or token
                        visitor_data = result[1] or visitor_data
                    elif isinstance(result, str):
                        token = result
                except Exception as exc:
                    logger.error("Error executing PO-token provider: %s", exc)

            if token or visitor_data:
                yt_args: dict[str, list[str]] = {}
                if token:
                    formatted_token = token if "+" in token else f"web+{token}"
                    yt_args["po_token"] = [formatted_token]
                if visitor_data:
                    yt_args["visitor_data"] = [visitor_data]
                ydl_opts["extractor_args"] = {"youtube": yt_args}

        # Progress hooks
        if progress_hook:
            ydl_opts["progress_hooks"] = [progress_hook]

        # Extra options overrides
        if options.extra_params:
            ydl_opts.update(options.extra_params)

        return ydl_opts

    def _classify_error(self, exc: Exception, url: str) -> YtDlpError:
        """Classify yt-dlp exceptions into domain-specific error types."""
        msg = str(exc).lower()
        unavailable_indicators = [
            "private video",
            "video unavailable",
            "this video has been removed",
            "sign in if you've been granted access",
            "blocked in your country",
            "copyright",
            "members-only",
            "not available",
            "has not granted access",
        ]
        if any(ind in msg for ind in unavailable_indicators):
            return VideoUnavailableError(f"Video unavailable ({url}): {exc}")

        if "cookie" in msg or "login" in msg:
            return CookieError(f"Authentication/Cookie failure for {url}: {exc}")

        return DownloadFailedError(f"Download pipeline failed for {url}: {exc}")

    def extract_info(
        self,
        url: str,
        options: DownloadOptions | None = None,
    ) -> VideoMetadata:
        """Extract video metadata without downloading media.

        Playlist entries are extracted flat (`_type: url`), so a playlist URL is
        enumerated cheaply; a single video is extracted fully.
        """
        opts = options or self.default_options
        ydl_params = self.build_ydl_opts(opts)
        ydl_params["extract_flat"] = "in_playlist"

        try:
            with yt_dlp.YoutubeDL(ydl_params) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise DownloadFailedError(f"No metadata returned for {url}")
                return parse_metadata(info)
        except (DownloadError, ExtractorError) as exc:
            raise self._classify_error(exc, url) from exc
        except Exception as exc:
            raise DownloadFailedError(
                f"Unexpected error extracting metadata from {url}: {exc}"
            ) from exc

    def download(
        self,
        url: str,
        options: DownloadOptions | None = None,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        info: dict[str, Any] | None = None,
    ) -> DownloadResult:
        """Execute full download pipeline (video, subtitles, and thumbnails).

        With `info` (the raw info dict from `extract_info`), the video is not extracted
        again: yt-dlp processes the given info via `process_ie_result`.
        """
        opts = options or self.default_options
        output_dir = Path(opts.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        ydl_params = self.build_ydl_opts(opts, progress_hook=progress_hook)

        try:
            with yt_dlp.YoutubeDL(ydl_params) as ydl:
                if info:
                    info = ydl.process_ie_result(info, download=True)
                else:
                    info = ydl.extract_info(url, download=True)
                if not info:
                    raise DownloadFailedError(f"Failed to extract info during download for {url}")

                metadata = parse_metadata(info)

                # Identify downloaded video file path
                video_path: Path | None = None
                req_downloads = info.get("requested_downloads")
                if req_downloads and isinstance(req_downloads, list):
                    for d in req_downloads:
                        filepath = d.get("filepath") or d.get("_filename")
                        if filepath and Path(filepath).exists():
                            video_path = Path(filepath)
                            break

                if not video_path:
                    # Fallback to prepare_filename
                    candidate = Path(ydl.prepare_filename(info))
                    if candidate.exists():
                        video_path = candidate
                    else:
                        # Scan output_dir for matching video id
                        target_glob = f"*{metadata.id}*.{opts.merge_output_format}"
                        matching = list(output_dir.rglob(target_glob))
                        if not matching:
                            matching = list(output_dir.rglob(f"*{metadata.id}*.mp4"))
                        if not matching:
                            matching = list(output_dir.rglob(f"*{metadata.id}*.mkv"))
                        if matching:
                            video_path = matching[0]

                if not video_path or not video_path.exists():
                    raise DownloadFailedError(
                        f"Download finished but video file was not found in {output_dir}"
                    )

                # Resolve thumbnail path with standard -thumb.jpg suffix
                parent_dir = video_path.parent
                stem = video_path.stem
                expected_thumb = parent_dir / f"{stem}{opts.thumbnail_suffix}.jpg"

                thumbnail_path: Path | None = None
                if expected_thumb.exists():
                    thumbnail_path = expected_thumb
                else:
                    # Look for any thumbnail produced for this stem
                    candidates = list(parent_dir.glob(f"{glob.escape(stem)}*.*"))
                    for cand in candidates:
                        if cand == video_path:
                            continue
                        if cand.suffix.lower() in [".jpg", ".jpeg", ".webp", ".png"]:
                            # If it's a thumbnail, ensure Jellyfin -thumb.jpg convention
                            if cand.name.endswith(f"{opts.thumbnail_suffix}{cand.suffix}"):
                                thumbnail_path = cand
                                break
                            # If no -thumb suffix exists yet, copy/rename
                            if not expected_thumb.exists() and cand.suffix.lower() == ".jpg":
                                cand.rename(expected_thumb)
                                thumbnail_path = expected_thumb
                                break
                            thumbnail_path = cand
                            break

                # Resolve downloaded subtitle paths (.vtt)
                subtitle_paths: list[Path] = []
                for sub_file in parent_dir.glob(f"{glob.escape(stem)}*.vtt"):
                    subtitle_paths.append(sub_file)

                file_size = video_path.stat().st_size if video_path.exists() else 0

                return DownloadResult(
                    id=metadata.id,
                    metadata=metadata,
                    video_path=video_path,
                    thumbnail_path=thumbnail_path,
                    subtitle_paths=sorted(subtitle_paths),
                    file_size=file_size,
                )

        except (DownloadError, ExtractorError) as exc:
            raise self._classify_error(exc, url) from exc
        except YtDlpError:
            raise
        except Exception as exc:
            raise DownloadFailedError(f"Unexpected error downloading {url}: {exc}") from exc

    def _channel_params(self, options: DownloadOptions | None = None) -> dict[str, Any]:
        """Minimal yt-dlp params for channel metadata (no entries, cookies if configured)."""
        params: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "playlist_items": "0",
        }
        cookies = options.cookies_path if options else None
        if cookies and Path(cookies).is_file():
            params["cookiefile"] = str(cookies)
        return params

    def extract_channel_info(
        self, channel_url: str, options: DownloadOptions | None = None
    ) -> dict[str, Any]:
        """Fetch channel metadata (id, name, thumbnails) without enumerating its videos."""
        try:
            with yt_dlp.YoutubeDL(self._channel_params(options)) as ydl:
                info = ydl.extract_info(channel_url, download=False)
        except (DownloadError, ExtractorError) as exc:
            raise self._classify_error(exc, channel_url) from exc
        if not info:
            raise DownloadFailedError(f"No channel metadata returned for {channel_url}")
        return info

    def fetch_channel_artwork(
        self,
        channel_url: str,
        channel_dir: Path | str,
        options: DownloadOptions | None = None,
    ) -> dict[str, Any]:
        """Write the channel avatar to `poster.jpg` and banner to `fanart.jpg` if missing.

        Images are fetched through yt-dlp's opener (honours cookies/proxy). Returns the
        channel info dict. Errors on individual images are logged, not raised.
        """
        target_dir = Path(channel_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(self._channel_params(options)) as ydl:
            info = ydl.extract_info(channel_url, download=False) or {}
            thumbs = {t.get("id"): t.get("url") for t in info.get("thumbnails") or [] if t}
            for filename, ids in (
                ("poster.jpg", ("avatar_uncropped", "avatar")),
                ("fanart.jpg", ("banner_uncropped", "banner")),
            ):
                dest = target_dir / filename
                url = next((thumbs[i] for i in ids if thumbs.get(i)), None)
                if dest.exists() or not url:
                    continue
                try:
                    data = ydl.urlopen(url).read()
                    if data:
                        dest.write_bytes(data)
                except Exception as exc:
                    logger.warning("Could not fetch %s for %s: %s", filename, channel_url, exc)
        return info

    async def async_extract_info(
        self,
        url: str,
        options: DownloadOptions | None = None,
    ) -> VideoMetadata:
        """Asynchronously extract video metadata in a background worker thread."""
        return await asyncio.to_thread(self.extract_info, url, options)

    async def async_download(
        self,
        url: str,
        options: DownloadOptions | None = None,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        info: dict[str, Any] | None = None,
    ) -> DownloadResult:
        """Asynchronously execute download pipeline in a background worker thread."""
        return await asyncio.to_thread(self.download, url, options, progress_hook, info)

    @staticmethod
    def import_cookies(
        content: str | bytes,
        dest_path: Path | str | None = None,
    ) -> Path:
        """Import and save cookies.txt file with secure permissions.

        Raises:
            CookieError: If cookies content is empty or invalid.
        """
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        stripped = text.strip()
        if not stripped:
            raise CookieError("Cookie content cannot be empty.")

        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        # Verify that content has at least one valid line
        if not lines:
            raise CookieError("No valid lines found in cookies content.")

        target = Path(dest_path if dest_path is not None else settings.cookies_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stripped + "\n", encoding="utf-8")

        # Set read/write only by owner for security
        try:
            target.chmod(0o600)
        except OSError as exc:
            logger.debug("Could not chmod cookies file %s: %s", target, exc)

        return target

    @staticmethod
    def validate_cookies_file(cookies_path: Path | str) -> bool:
        """Check if a cookies file exists and contains valid cookie lines."""
        path = Path(cookies_path)
        if not path.is_file():
            return False
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                return False
            # Check for standard Netscape cookies comments or tabbed entries
            return any(
                "\t" in line or line.startswith("#")
                for line in content.splitlines()
                if line.strip()
            )
        except Exception:
            return False


# Global default downloader service instance
downloader = YtDlpService()
