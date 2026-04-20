"""Local SQLite cache for recently submitted jobs.

Backs the ``list_recent_jobs`` MCP tool and gives the user a memory of
jobs they've started from this machine. The cache is local-only — its
contents never leave the user's filesystem.

Privacy contract — the cache stores only:

* ``job_id`` — opaque server-generated identifier
* ``source`` — ``"url"`` or ``"file"`` (kind, not value)
* ``status`` — ``queued | running | done | failed``
* ``duration_sec`` — audio length in seconds, when known
* ``created_at`` — ISO-8601 UTC timestamp of the local submission
* ``claim_token`` — opaque token returned by anonymous submissions; required
  to authorize subsequent ``GET /jobs/{id}`` lookups against jobs the caller
  submitted without an API key. Never surfaced through ``list_recent_jobs``.

It explicitly does **not** store source URLs, local file paths,
filenames, transcripts, API keys, or any per-job content.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Literal, TypedDict

import structlog

log = structlog.get_logger()

DEFAULT_DB_PATH = Path.home() / ".whipscribe-mcp" / "jobs.db"

JobSource = Literal["url", "file"]
JobStatus = Literal["queued", "running", "done", "failed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_sec REAL,
    created_at TEXT NOT NULL,
    claim_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
"""

# Idempotent column additions for databases created by older whipscribe-mcp
# versions. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we probe PRAGMA
# table_info first. The CREATE TABLE above covers fresh installs; this list
# covers in-place upgrades where the row existed before a column did.
_COLUMN_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("claim_token", "TEXT"),
)


def _migrate_jobs_table(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, ddl_type in _COLUMN_ADDITIONS:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl_type}")


class RecentJob(TypedDict):
    job_id: str
    source: JobSource
    status: JobStatus
    duration_sec: float | None
    created_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobCache:
    """Async-friendly wrapper over a small SQLite jobs table.

    SQLite calls run on a thread via :func:`asyncio.to_thread` so the
    MCP event loop is never blocked. The connection is opened lazily on
    first use and closed in :meth:`aclose` / on context exit.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path: Path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> JobCache:
        await self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _ensure_open(self) -> None:
        if self._conn is not None:
            return
        async with self._lock:
            if self._conn is not None:
                return

            def _open() -> sqlite3.Connection:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                # check_same_thread=False is safe here because Python's
                # sqlite3 ships in serialized threading mode, and every
                # query runs on a single asyncio.to_thread executor.
                # Without this, the connection opened in one worker
                # thread cannot be reused in another worker thread.
                conn = sqlite3.connect(
                    self._db_path,
                    isolation_level=None,
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.executescript(_SCHEMA)
                _migrate_jobs_table(conn)
                return conn

            try:
                self._conn = await asyncio.to_thread(_open)
            except OSError as exc:
                log.warning("job_cache_open_failed", error_class=exc.__class__.__name__)
                raise

    async def aclose(self) -> None:
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        await asyncio.to_thread(conn.close)

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    async def record_job(
        self,
        *,
        job_id: str,
        source: JobSource,
        status: JobStatus = "queued",
        duration_sec: float | None = None,
        created_at: str | None = None,
        claim_token: str | None = None,
    ) -> None:
        """Insert or replace a job row."""
        await self._ensure_open()
        timestamp = created_at or _now_iso()

        def _write() -> None:
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO jobs (job_id, source, status, duration_sec, created_at, claim_token)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    source = excluded.source,
                    status = excluded.status,
                    duration_sec = COALESCE(excluded.duration_sec, jobs.duration_sec),
                    created_at = jobs.created_at,
                    claim_token = COALESCE(excluded.claim_token, jobs.claim_token)
                """,
                (job_id, source, status, duration_sec, timestamp, claim_token),
            )

        await asyncio.to_thread(_write)

    async def get_claim_token(self, job_id: str) -> str | None:
        """Return the stored ``claim_token`` for ``job_id`` or ``None``.

        Used by tool handlers to authorize subsequent ``GET /jobs/{id}`` /
        ``GET /jobs/{id}/result`` calls against anonymous-submitted jobs.
        Never returned through any public MCP-facing surface — claim_token
        is treated as a per-job secret.
        """
        await self._ensure_open()

        def _read() -> str | None:
            assert self._conn is not None
            cursor = self._conn.execute(
                "SELECT claim_token FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            value = row["claim_token"]
            return value if isinstance(value, str) else None

        return await asyncio.to_thread(_read)

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        duration_sec: float | None = None,
    ) -> None:
        """Update status (and optionally duration) for a known job."""
        await self._ensure_open()

        def _write() -> None:
            assert self._conn is not None
            if duration_sec is None:
                self._conn.execute(
                    "UPDATE jobs SET status = ? WHERE job_id = ?",
                    (status, job_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, duration_sec = COALESCE(?, duration_sec)
                    WHERE job_id = ?
                    """,
                    (status, duration_sec, job_id),
                )

        await asyncio.to_thread(_write)

    async def list_recent(self, limit: int = 10) -> list[RecentJob]:
        """Return the most recent jobs, newest first."""
        await self._ensure_open()
        clamped = max(1, min(100, int(limit)))

        def _read() -> list[RecentJob]:
            assert self._conn is not None
            cursor = self._conn.execute(
                """
                SELECT job_id, source, status, duration_sec, created_at
                FROM jobs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (clamped,),
            )
            return [_row_to_recent_job(row) for row in cursor.fetchall()]

        return await asyncio.to_thread(_read)


def _row_to_recent_job(row: sqlite3.Row) -> RecentJob:
    return {
        "job_id": row["job_id"],
        "source": row["source"],
        "status": row["status"],
        "duration_sec": row["duration_sec"],
        "created_at": row["created_at"],
    }


__all__ = [
    "DEFAULT_DB_PATH",
    "JobCache",
    "JobSource",
    "JobStatus",
    "RecentJob",
]
