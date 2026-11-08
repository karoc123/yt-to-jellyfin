"""Safe filesystem deletion utilities for videos and companion artifacts."""

import glob
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def delete_video_artifacts(
    video_path: Path | str | None,
    nfo_path: Path | str | None = None,
    thumbnail_path: Path | str | None = None,
) -> list[Path]:
    """Safely delete video file and all its companion artifacts.

    Companion artifacts include:
    - The video media file (.mp4, .mkv, etc.)
    - The episode metadata file (.nfo)
    - The episode thumbnail (-thumb.jpg or similar)
    - Subtitle files (.vtt, .srt, etc.) matching the video file stem
    - Empty parent season directory (if no other videos or files remain)

    Returns a list of all deleted file paths.
    """
    deleted_paths: list[Path] = []

    # 1. Gather primary video path
    v_path = Path(video_path) if video_path else None
    if v_path and v_path.exists():
        stem = v_path.stem
        parent_dir = v_path.parent

        # Find and delete subtitles and other companion files with matching stem
        try:
            escaped_stem = glob.escape(stem)
            for companion in parent_dir.glob(f"{escaped_stem}*"):
                if companion.is_file():
                    try:
                        companion.unlink()
                        deleted_paths.append(companion)
                        logger.info("Deleted companion file: %s", companion)
                    except OSError as exc:
                        logger.warning("Failed to delete companion file %s: %s", companion, exc)
        except Exception as exc:
            logger.warning("Error finding companions for %s: %s", v_path, exc)

        # Ensure video file itself is unlinked if not caught in glob
        if v_path.exists():
            try:
                v_path.unlink()
                if v_path not in deleted_paths:
                    deleted_paths.append(v_path)
            except OSError as exc:
                logger.warning("Failed to delete video file %s: %s", v_path, exc)

        # 2. Check and clean up empty parent Season directory
        try:
            if parent_dir.exists() and parent_dir.is_dir():
                remaining = list(parent_dir.iterdir())
                if not remaining:
                    parent_dir.rmdir()
                    logger.info("Removed empty season folder: %s", parent_dir)
        except OSError as exc:
            logger.debug("Season directory %s not empty or cannot be removed: %s", parent_dir, exc)

    # 3. Explicitly clean up nfo_path if provided and still exists
    if nfo_path:
        n_path = Path(nfo_path)
        if n_path.exists() and n_path.is_file():
            try:
                n_path.unlink()
                if n_path not in deleted_paths:
                    deleted_paths.append(n_path)
            except OSError as exc:
                logger.warning("Failed to delete explicit nfo %s: %s", n_path, exc)

    # 4. Explicitly clean up thumbnail_path if provided and still exists
    if thumbnail_path:
        t_path = Path(thumbnail_path)
        if t_path.exists() and t_path.is_file():
            try:
                t_path.unlink()
                if t_path not in deleted_paths:
                    deleted_paths.append(t_path)
            except OSError as exc:
                logger.warning("Failed to delete explicit thumbnail %s: %s", t_path, exc)

    return deleted_paths
