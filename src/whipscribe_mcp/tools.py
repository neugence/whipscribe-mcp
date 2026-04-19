"""Tool handlers for the Whipscribe MCP server.

The signatures in this module are the stable public contract exposed to
MCP clients. Argument names, defaults, and return shapes are SemVer
bound: breaking changes require a major version bump.

Each handler:

* Validates input and returns a structured ``ToolFailure`` (never raises
  across the MCP boundary).
* Derives a deterministic ``Idempotency-Key`` for the two transcribe
  endpoints so re-running on the same source within the server's
  retention window returns the cached ``job_id`` instead of double-billing.
* Records the resulting job in the local SQLite cache so it surfaces in
  ``list_recent_jobs``.
* For ``transcribe_url`` / ``transcribe_file``, polls the backend until
  the job is ``done`` or ``failed`` (or until the configurable poll
  timeout elapses) and returns a transcript preview plus a ``url_to_full``
  link to the hosted transcript.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Literal, TypedDict

import structlog

from .cache import JobCache
from .client import WhipscribeClient
from .errors import BETA_NOTICE, ErrorObject, ToolError

log = structlog.get_logger()

TranscriptFormat = Literal["txt", "json", "srt", "vtt", "docx"]
JobStatus = Literal["queued", "running", "done", "failed"]

# Backend uses ``processing``; the public MCP contract uses ``running``.
_BACKEND_STATUS_MAP: dict[str, JobStatus] = {
    "queued": "queued",
    "processing": "running",
    "running": "running",
    "done": "done",
    "completed": "done",
    "failed": "failed",
    "error": "failed",
}

DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_POLL_TIMEOUT_SECONDS = 600.0
PREVIEW_LENGTH = 300
TRANSCRIPT_VIEW_URL = "https://whipscribe.com/view?id={job_id}"


class ToolSuccess(TypedDict, total=False):
    ok: Literal[True]
    job_id: str
    status: JobStatus
    transcript_preview: str
    url_to_full: str
    duration_sec: float
    beta_notice: str


class ToolFailure(TypedDict):
    ok: Literal[False]
    error: ErrorObject


ToolResult = ToolSuccess | ToolFailure


class RecentJobPublic(TypedDict):
    job_id: str
    created_at: str
    status: JobStatus
    source: Literal["url", "file"]
    duration_sec: float | None


class ListJobsSuccess(TypedDict):
    ok: Literal[True]
    jobs: list[RecentJobPublic]
    beta_notice: str


ListJobsResult = ListJobsSuccess | ToolFailure


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _normalize_status(raw: Any) -> JobStatus:
    if isinstance(raw, str):
        mapped = _BACKEND_STATUS_MAP.get(raw.lower())
        if mapped is not None:
            return mapped
    return "queued"


def _build_preview(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= PREVIEW_LENGTH:
        return collapsed
    return collapsed[: PREVIEW_LENGTH - 1].rstrip() + "…"


def _view_url(job_id: str) -> str:
    return TRANSCRIPT_VIEW_URL.format(job_id=job_id)


def _failure(error: ToolError) -> ToolFailure:
    return {"ok": False, "error": error.to_object()}


def _key_for_url(url: str, language: str | None) -> str:
    raw = f"url|{url}|{language or ''}"
    return f"u-{hashlib.sha256(raw.encode()).hexdigest()[:48]}"


def _key_for_file(path: Path, language: str | None) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    raw = f"file|{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}|{language or ''}"
    return f"f-{hashlib.sha256(raw.encode()).hexdigest()[:48]}"


def _poll_timeout_seconds() -> float:
    raw = os.environ.get("WHIPSCRIBE_MCP_POLL_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_POLL_TIMEOUT_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_POLL_TIMEOUT_SECONDS


def _poll_interval_seconds() -> float:
    raw = os.environ.get("WHIPSCRIBE_MCP_POLL_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_POLL_INTERVAL_SECONDS
    try:
        return max(0.5, float(raw))
    except ValueError:
        return DEFAULT_POLL_INTERVAL_SECONDS


async def _poll_until_done(
    client: WhipscribeClient,
    job_id: str,
    cache: JobCache | None,
) -> tuple[JobStatus, dict[str, Any]]:
    """Poll ``get_job_status`` until terminal or until the poll timeout elapses."""
    deadline = time.monotonic() + _poll_timeout_seconds()
    interval = _poll_interval_seconds()

    while True:
        payload = await client.get_job_status(job_id)
        status = _normalize_status(payload.get("status"))

        if cache is not None:
            duration = payload.get("audio_duration_seconds")
            try:
                duration_value = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration_value = None
            await cache.update_status(job_id, status, duration_sec=duration_value)

        if status in ("done", "failed"):
            return status, payload

        if time.monotonic() >= deadline:
            return status, payload

        await asyncio.sleep(interval)


async def _fetch_preview(client: WhipscribeClient, job_id: str) -> str:
    try:
        body = await client.get_transcript(job_id, format="txt")
    except ToolError as exc:
        log.warning("transcript_preview_unavailable", error_code=exc.code)
        return ""
    if isinstance(body, str):
        return _build_preview(body)
    return ""


# ---------------------------------------------------------------------
# Public tool handlers
# ---------------------------------------------------------------------


async def transcribe_url(
    url: str,
    *,
    client: WhipscribeClient,
    cache: JobCache | None = None,
    language: str | None = None,
    diarize: bool = False,
    word_timestamps: bool = False,
) -> ToolResult:
    """Transcribe audio or video from a URL.

    Returns once the backend reports ``done`` (or ``failed``) or the
    poll timeout elapses. On timeout, the response includes the
    ``job_id`` and a non-terminal status so the caller can poll later
    via :func:`get_job_status` / :func:`get_transcript`.
    """
    if not isinstance(url, str) or not url.strip():
        return _failure(
            ToolError("invalid_input", "url must be a non-empty string.", retryable=False)
        )

    idem_key = _key_for_url(url.strip(), language)

    try:
        submission = await client.submit_url(
            url.strip(),
            language=language,
            diarize=diarize,
            word_timestamps=word_timestamps,
            idempotency_key=idem_key,
        )
    except ToolError as exc:
        return _failure(exc)

    job_id = submission.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return _failure(
            ToolError("server_error", "Backend did not return a job_id.", retryable=True)
        )

    if cache is not None:
        try:
            await cache.record_job(job_id=job_id, source="url", status="queued")
        except OSError:
            pass

    try:
        status, status_payload = await _poll_until_done(client, job_id, cache)
    except ToolError as exc:
        return _failure(exc)

    duration_raw = status_payload.get("audio_duration_seconds")
    try:
        duration_sec = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration_sec = None

    if status == "failed":
        backend_error = status_payload.get("error")
        message = backend_error if isinstance(backend_error, str) else "Job failed."
        return _failure(ToolError("job_failed", message, retryable=False))

    preview = await _fetch_preview(client, job_id) if status == "done" else ""

    success: ToolSuccess = {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "url_to_full": _view_url(job_id),
        "beta_notice": BETA_NOTICE,
    }
    if preview:
        success["transcript_preview"] = preview
    if duration_sec is not None:
        success["duration_sec"] = duration_sec
    return success


async def transcribe_file(
    path: str,
    *,
    client: WhipscribeClient,
    cache: JobCache | None = None,
    language: str | None = None,
    diarize: bool = False,
    word_timestamps: bool = False,
) -> ToolResult:
    """Transcribe a local audio or video file.

    The local ``path`` is never sent to telemetry — only an anonymous
    event count is recorded. The idempotency key is derived from the
    resolved path plus file size and mtime, so editing the file
    invalidates the cache entry as expected.
    """
    if not isinstance(path, str) or not path.strip():
        return _failure(
            ToolError("invalid_input", "path must be a non-empty string.", retryable=False)
        )

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return _failure(
            ToolError(
                "file_not_found",
                "Local media file does not exist or is not a regular file.",
                retryable=False,
            )
        )

    idem_key = _key_for_file(file_path, language)

    try:
        submission = await client.submit_file(
            file_path,
            language=language,
            diarize=diarize,
            word_timestamps=word_timestamps,
            idempotency_key=idem_key,
        )
    except ToolError as exc:
        return _failure(exc)

    job_id = submission.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return _failure(
            ToolError("server_error", "Backend did not return a job_id.", retryable=True)
        )

    if cache is not None:
        try:
            await cache.record_job(job_id=job_id, source="file", status="queued")
        except OSError:
            pass

    try:
        status, status_payload = await _poll_until_done(client, job_id, cache)
    except ToolError as exc:
        return _failure(exc)

    duration_raw = status_payload.get("audio_duration_seconds")
    try:
        duration_sec = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration_sec = None

    if status == "failed":
        backend_error = status_payload.get("error")
        message = backend_error if isinstance(backend_error, str) else "Job failed."
        return _failure(ToolError("job_failed", message, retryable=False))

    preview = await _fetch_preview(client, job_id) if status == "done" else ""

    success: ToolSuccess = {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "url_to_full": _view_url(job_id),
        "beta_notice": BETA_NOTICE,
    }
    if preview:
        success["transcript_preview"] = preview
    if duration_sec is not None:
        success["duration_sec"] = duration_sec
    return success


async def get_job_status(
    job_id: str,
    *,
    client: WhipscribeClient,
    cache: JobCache | None = None,
) -> ToolResult:
    """Poll progress of a transcription job."""
    if not isinstance(job_id, str) or not job_id.strip():
        return _failure(
            ToolError("invalid_input", "job_id must be a non-empty string.", retryable=False)
        )

    try:
        payload = await client.get_job_status(job_id)
    except ToolError as exc:
        return _failure(exc)

    status = _normalize_status(payload.get("status"))
    duration_raw = payload.get("audio_duration_seconds")
    try:
        duration_sec = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration_sec = None

    if cache is not None:
        try:
            await cache.update_status(job_id, status, duration_sec=duration_sec)
        except OSError:
            pass

    success: ToolSuccess = {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "url_to_full": _view_url(job_id),
        "beta_notice": BETA_NOTICE,
    }
    if duration_sec is not None:
        success["duration_sec"] = duration_sec
    return success


async def get_transcript(
    job_id: str,
    *,
    client: WhipscribeClient,
    format: TranscriptFormat = "txt",
) -> ToolResult:
    """Fetch a finished transcript in the requested format.

    Returns a ``transcript_preview`` (first 300 characters of plain text)
    plus a ``url_to_full`` link. Large transcripts are not inlined in the
    MCP response — fetch the full artifact via ``url_to_full`` or call
    the backend directly with the desired format.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        return _failure(
            ToolError("invalid_input", "job_id must be a non-empty string.", retryable=False)
        )

    try:
        body = await client.get_transcript(job_id, format=format)
    except ToolError as exc:
        return _failure(exc)

    preview = ""
    if isinstance(body, str):
        preview = _build_preview(body)
    elif isinstance(body, dict):
        # JSON transcripts: pull the top-level ``text`` field if present,
        # otherwise stringify the segments collection.
        text_field = body.get("text")
        if isinstance(text_field, str):
            preview = _build_preview(text_field)

    success: ToolSuccess = {
        "ok": True,
        "job_id": job_id,
        "status": "done",
        "url_to_full": _view_url(job_id),
        "beta_notice": BETA_NOTICE,
    }
    if preview:
        success["transcript_preview"] = preview
    return success


async def list_recent_jobs(
    limit: int = 10,
    *,
    cache: JobCache,
) -> ListJobsResult:
    """List recent jobs from the local SQLite cache.

    The cache lives under ``~/.whipscribe-mcp/jobs.db`` and never leaves
    the machine. It records only ``job_id``, timestamp, status, and
    source kind — never URLs, file paths, or transcript text.
    """
    try:
        bounded = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        return _failure(
            ToolError("invalid_input", "limit must be an integer 1–100.", retryable=False)
        )

    try:
        rows = await cache.list_recent(bounded)
    except OSError as exc:
        return _failure(
            ToolError("server_error", f"Local cache unavailable: {exc.__class__.__name__}", retryable=True)
        )

    jobs: list[RecentJobPublic] = [
        {
            "job_id": row["job_id"],
            "created_at": row["created_at"],
            "status": row["status"],  # type: ignore[typeddict-item]
            "source": row["source"],  # type: ignore[typeddict-item]
            "duration_sec": row["duration_sec"],
        }
        for row in rows
    ]
    return {"ok": True, "jobs": jobs, "beta_notice": BETA_NOTICE}


__all__ = [
    "TranscriptFormat",
    "JobStatus",
    "ToolResult",
    "ToolSuccess",
    "ToolFailure",
    "RecentJobPublic",
    "ListJobsResult",
    "ListJobsSuccess",
    "transcribe_url",
    "transcribe_file",
    "get_job_status",
    "get_transcript",
    "list_recent_jobs",
]
