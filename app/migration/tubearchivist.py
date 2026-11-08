"""TubeArchivist media migration utility into Jellyfin library structure and SQLite catalog."""

import argparse
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.db.database import Database, db
from app.jellyfin.layout import JellyfinLayout, sanitize_filename
from app.jellyfin.nfo import NfoGenerator

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class DiscoveredVideo:
    """Discovered video candidate from TubeArchivist archive."""

    video_path: Path
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    description: str = ""
    published_at: str = ""
    duration: int = 0
    thumbnail_path: Path | None = None
    info_json_path: Path | None = None


@dataclass
class MigrationResult:
    """Summary of migration operation."""

    scanned_count: int = 0
    migrated_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    channels_created: int = 0
    errors: list[str] = field(default_factory=list)


class TubeArchivistMigrator:
    """Migrates TubeArchivist video archives to Jellyfin structure and updates database."""

    def __init__(
        self,
        source_dir: Path | str,
        dest_media_root: Path | str = "/media",
        library: str = "default",
        mode: str = "copy",
        dry_run: bool = False,
        database: Database | None = None,
    ) -> None:
        self.source_dir = Path(source_dir)
        self.dest_media_root = Path(dest_media_root)
        self.library = library or "default"
        self.mode = mode.lower()
        if self.mode not in {"copy", "move", "hardlink", "symlink"}:
            msg = f"Unsupported transfer mode '{mode}'. Choose copy, move, hardlink, symlink."
            raise ValueError(msg)
        self.dry_run = dry_run
        self.db = database if database is not None else db
        self.layout = JellyfinLayout(self.dest_media_root)

    def scan_source(self) -> list[DiscoveredVideo]:
        """Scan source directory for videos and companion metadata."""
        if not self.source_dir.exists():
            logger.error("Source directory does not exist: %s", self.source_dir)
            return []

        candidates: list[DiscoveredVideo] = []

        # TubeArchivist typically lays out:
        # <source>/youtube/<channel_id>/<video_id>.<ext>
        # or <source>/<channel_id>/<video_id>.<ext>
        for root, _, files in os.walk(self.source_dir):
            root_path = Path(root)
            for f in files:
                file_path = root_path / f
                if file_path.suffix.lower() in VIDEO_EXTENSIONS:
                    item = self._extract_metadata(file_path)
                    if item:
                        candidates.append(item)

        return candidates

    def _extract_metadata(self, video_path: Path) -> DiscoveredVideo | None:
        """Extract metadata from file path and companion .info.json/.description files."""
        stem = video_path.stem

        # Try to identify video_id
        # In TubeArchivist, the file is often named <video_id>.mp4 or <title> [<video_id>].mp4
        match = re.search(r"\[([a-zA-Z0-9_-]{11})\]", stem)
        if match:
            video_id = match.group(1)
        elif len(stem) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", stem):
            video_id = stem
        else:
            video_id = stem

        parent_dir = video_path.parent
        # In TubeArchivist: parent is channel_id (e.g. UC...) or channel name
        # If parent is 'youtube', take parent's child
        channel_id = parent_dir.name if parent_dir.name != "youtube" else "unknown_channel"

        # Check for companion .info.json
        info_json_path = None
        for candidate_name in [
            f"{stem}.info.json",
            f"{video_id}.info.json",
        ]:
            cand = parent_dir / candidate_name
            if cand.is_file():
                info_json_path = cand
                break

        title = stem
        channel_name = channel_id
        description = ""
        published_at = ""
        duration = 0

        if info_json_path:
            try:
                data = json.loads(info_json_path.read_text(encoding="utf-8"))
                video_id = str(data.get("id") or video_id)
                title = str(data.get("title") or title)
                channel_name = str(
                    data.get("channel")
                    or data.get("uploader")
                    or data.get("channel_id")
                    or channel_name
                )
                channel_id = str(data.get("channel_id") or data.get("uploader_id") or channel_id)
                description = str(data.get("description") or "")
                duration = int(data.get("duration") or 0)

                upload_date = data.get("upload_date")
                if upload_date and len(str(upload_date)) == 8:
                    s = str(upload_date)
                    published_at = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
                elif data.get("timestamp"):
                    published_at = datetime.fromtimestamp(data["timestamp"], tz=UTC).strftime(
                        "%Y-%m-%d"
                    )
            except Exception as exc:
                logger.warning("Error reading info.json %s: %s", info_json_path, exc)

        # Check for companion channel metadata if channel_name is still default
        if channel_name == channel_id:
            for ch_cand in ["channel.json", f"{channel_id}.json"]:
                p = parent_dir / ch_cand
                if p.is_file():
                    try:
                        cdata = json.loads(p.read_text(encoding="utf-8"))
                        channel_name = str(
                            cdata.get("channel_name")
                            or cdata.get("name")
                            or cdata.get("title")
                            or channel_name
                        )
                        break
                    except Exception:
                        pass

        # Check companion description file
        if not description:
            desc_file = parent_dir / f"{stem}.description"
            if desc_file.is_file():
                try:
                    description = desc_file.read_text(encoding="utf-8")
                except Exception:
                    pass

        # Fallback date if not extracted: file mtime
        if not published_at:
            mtime = video_path.stat().st_mtime
            published_at = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%d")

        # Find thumbnail file
        thumbnail_path = None
        for img_ext in IMAGE_EXTENSIONS:
            for cand_stem in [stem, video_id]:
                t = parent_dir / f"{cand_stem}{img_ext}"
                if t.is_file():
                    thumbnail_path = t
                    break
            if thumbnail_path:
                break

        return DiscoveredVideo(
            video_path=video_path,
            video_id=video_id,
            channel_id=channel_id,
            channel_name=channel_name,
            title=title,
            description=description,
            published_at=published_at,
            duration=duration,
            thumbnail_path=thumbnail_path,
            info_json_path=info_json_path,
        )

    def _transfer_file(self, src: Path, dst: Path) -> None:
        """Transfer a file according to configured transfer mode."""
        if self.dry_run:
            logger.info("[DRY-RUN] Would %s %s -> %s", self.mode, src, dst)
            return

        dst.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "copy":
            shutil.copy2(src, dst)
        elif self.mode == "move":
            shutil.move(src, dst)
        elif self.mode == "hardlink":
            try:
                os.link(src, dst)
            except OSError:
                # Fallback to copy if cross-device link fails
                shutil.copy2(src, dst)
        elif self.mode == "symlink":
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.symlink(src.resolve(), dst)

    def migrate(self) -> MigrationResult:
        """Execute migration for all discovered videos."""
        result = MigrationResult()
        candidates = self.scan_source()
        result.scanned_count = len(candidates)

        known_channels: set[str] = set()

        for item in candidates:
            try:
                year = item.published_at.split("-")[0] if "-" in item.published_at else "Unknown"

                # Destination paths
                dest_video_path = self.layout.get_video_path(
                    channel_name=item.channel_name,
                    year=year,
                    date_str=item.published_at,
                    title=item.title,
                    video_id=item.video_id,
                    library=self.library,
                    extension=item.video_path.suffix.lstrip("."),
                )
                dest_nfo_path = self.layout.get_episode_nfo_path(dest_video_path)
                dest_thumb_path = (
                    self.layout.get_episode_thumb_path(dest_video_path)
                    if item.thumbnail_path
                    else None
                )
                dest_channel_nfo = self.layout.get_channel_nfo_path(item.channel_name, self.library)

                # Transfer video file
                self._transfer_file(item.video_path, dest_video_path)

                # Transfer thumbnail if present
                if item.thumbnail_path and dest_thumb_path:
                    self._transfer_file(item.thumbnail_path, dest_thumb_path)

                # Write XML NFOs
                if not self.dry_run:
                    # Write episode NFO
                    NfoGenerator.write_episode_nfo(
                        dest_path=dest_nfo_path,
                        title=item.title,
                        plot=item.description,
                        aired=item.published_at,
                        season=int(year) if year.isdigit() else None,
                        episode=1,
                        duration_seconds=item.duration,
                        video_id=item.video_id,
                        channel_name=item.channel_name,
                        webpage_url=f"https://www.youtube.com/watch?v={item.video_id}",
                    )

                    # Write TV show NFO if missing
                    if not dest_channel_nfo.exists():
                        NfoGenerator.write_tvshow_nfo(
                            dest_path=dest_channel_nfo,
                            title=item.channel_name,
                            channel_id=item.channel_id,
                            channel_url=f"https://www.youtube.com/channel/{item.channel_id}",
                        )

                    # Update SQLite database
                    now_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    with self.db.connection() as conn:
                        cursor = conn.cursor()

                        # Ensure channel exists in database
                        cursor.execute(
                            """
                            INSERT INTO channels (
                                id, name, url, folder_name, library,
                                auto_download, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                name = excluded.name,
                                folder_name = excluded.folder_name,
                                library = excluded.library,
                                updated_at = excluded.updated_at
                            """,
                            (
                                item.channel_id,
                                item.channel_name,
                                f"https://www.youtube.com/channel/{item.channel_id}",
                                sanitize_filename(item.channel_name),
                                self.library,
                                now_str,
                                now_str,
                            ),
                        )
                        if item.channel_id not in known_channels:
                            known_channels.add(item.channel_id)
                            result.channels_created += 1

                        file_size = (
                            dest_video_path.stat().st_size if dest_video_path.exists() else 0
                        )

                        # Insert or update video record
                        cursor.execute(
                            """
                            INSERT INTO videos (
                                id, channel_id, title, description, published_at, duration,
                                file_path, nfo_path, thumbnail_path, file_size, status,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                channel_id = excluded.channel_id,
                                title = excluded.title,
                                description = excluded.description,
                                published_at = excluded.published_at,
                                duration = excluded.duration,
                                file_path = excluded.file_path,
                                nfo_path = excluded.nfo_path,
                                thumbnail_path = excluded.thumbnail_path,
                                file_size = excluded.file_size,
                                status = 'completed',
                                updated_at = excluded.updated_at
                            """,
                            (
                                item.video_id,
                                item.channel_id,
                                item.title,
                                item.description,
                                item.published_at,
                                item.duration,
                                str(dest_video_path),
                                str(dest_nfo_path),
                                str(dest_thumb_path) if dest_thumb_path else None,
                                file_size,
                                now_str,
                                now_str,
                            ),
                        )

                result.migrated_count += 1
                logger.info(
                    "Migrated: %s (%s) -> %s", item.title, item.video_id, dest_video_path.name
                )
            except Exception as exc:
                err_msg = f"Failed to migrate video {item.video_id} ({item.video_path}): {exc}"
                logger.error(err_msg)
                result.errors.append(err_msg)
                result.failed_count += 1

        return result


def main() -> None:
    """CLI entrypoint for TubeArchivist migration."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="Migrate TubeArchivist media directory to Jellyfin layout and catalog."
    )
    parser.add_argument(
        "--source",
        "-s",
        required=True,
        help="Path to TubeArchivist media folder (e.g. /youtube or /path/to/archive)",
    )
    parser.add_argument(
        "--dest",
        "-d",
        default=os.environ.get("MEDIA_PATH", "/media"),
        help="Destination Jellyfin media root directory (default: MEDIA_PATH or /media)",
    )
    parser.add_argument(
        "--library",
        "-l",
        default="default",
        help="Target Jellyfin library folder name (e.g. default, kids, dokus)",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=["copy", "move", "hardlink", "symlink"],
        default="copy",
        help="File operation mode: copy (safe), move, hardlink, symlink (default: copy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview discovered videos and migration plan without modifying files or database",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Custom SQLite database file path (optional)",
    )

    args = parser.parse_args()

    database = Database(args.db) if args.db else db
    # Ensure database migrations are up to date
    database.run_migrations()

    migrator = TubeArchivistMigrator(
        source_dir=args.source,
        dest_media_root=args.dest,
        library=args.library,
        mode=args.mode,
        dry_run=args.dry_run,
        database=database,
    )

    print("--- TubeArchivist Migration Tool ---")
    print(f"Source:      {args.source}")
    print(f"Destination: {args.dest}")
    print(f"Library:     {args.library}")
    print(f"Mode:        {args.mode}")
    print(f"Dry Run:     {args.dry_run}")
    print("-------------------------------------")

    result = migrator.migrate()

    print("\n--- Migration Summary ---")
    print(f"Scanned Videos:   {result.scanned_count}")
    print(f"Migrated Videos:  {result.migrated_count}")
    print(f"Failed Videos:    {result.failed_count}")
    print(f"Channels Created: {result.channels_created}")
    if result.errors:
        print(f"Errors ({len(result.errors)}):")
        for err in result.errors[:10]:
            print(f"  - {err}")
    print("-------------------------")

    if result.failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
