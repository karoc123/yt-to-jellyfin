"""SQLite database manager with WAL mode, foreign keys, and migration system."""

import logging
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    """Lightweight SQLite database wrapper with migration support."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path if db_path is not None else settings.database_path)

    def _ensure_dir(self) -> None:
        """Ensure parent directory for the database file exists."""
        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        """Create and configure a SQLite connection."""
        self._ensure_dir()
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # PRAGMA journal_mode = WAL must execute outside a transaction
        conn.isolation_level = None
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.isolation_level = ""
        return conn

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection]:
        """Context manager yielding a transactional database connection."""
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_user_version(self) -> int:
        """Fetch the current schema version from PRAGMA user_version."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def set_user_version(self, version: int) -> None:
        """Set the schema version using PRAGMA user_version."""
        with self.connection() as conn:
            conn.execute(f"PRAGMA user_version = {int(version)};")

    def run_migrations(self, migrations_dir: Path | None = None) -> list[int]:
        """Scan migration directory and apply all pending SQL migrations."""
        target_dir = migrations_dir or MIGRATIONS_DIR
        if not target_dir.exists():
            logger.warning("Migrations directory does not exist: %s", target_dir)
            return []

        # Find migration files matching pattern '001_name.sql'
        pattern = re.compile(r"^(\d{3,})_.*\.sql$")
        migration_files: list[tuple[int, Path]] = []
        for file in target_dir.glob("*.sql"):
            match = pattern.match(file.name)
            if match:
                version = int(match.group(1))
                migration_files.append((version, file))

        migration_files.sort(key=lambda item: item[0])

        current_version = self.get_user_version()
        applied_versions: list[int] = []

        for version, file_path in migration_files:
            if version > current_version:
                logger.info("Applying migration %03d from %s", version, file_path.name)
                sql_script = file_path.read_text(encoding="utf-8")
                with self.connection() as conn:
                    conn.executescript(sql_script)
                    conn.execute(f"PRAGMA user_version = {version};")
                applied_versions.append(version)
                current_version = version

        return applied_versions


# Global database instance
db = Database()


def get_db() -> Generator[sqlite3.Connection]:
    """Dependency / helper to get database connection."""
    with db.connection() as conn:
        yield conn
