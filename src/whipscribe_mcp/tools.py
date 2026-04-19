"""Tool handlers for the Whipscribe MCP server.

The signatures in this module are the stable public contract exposed to
MCP clients. Argument names, defaults, and return shapes are SemVer-bound:
breaking changes require a major version bump.

Implementations land in a follow-up change — these stubs raise
``NotImplementedError`` so that integration tests can assert registration
without exercising backend calls.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from .errors import ErrorObject

TranscriptFormat = Literal["txt", "json", "srt", "vtt", "docx"]
JobStatus = Literal["queued", "running", "done", "failed"]


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


class RecentJob(TypedDict):
    job_id: str
    created_at: str
    status: JobStatus
    source: Literal["url", "file"]
    duration_sec: float | None


class ListJobsSuccess(TypedDict):
    ok: Literal[True]
    jobs: list[RecentJob]
    beta_notice: str


ListJobsResult = ListJobsSuccess | ToolFailure


async def transcribe_url(
    url: str,
    language: str | None = None,
    diarize: bool = False,
    word_timestamps: bool = False,
) -> ToolResult:
    """Transcribe audio or video from a URL.

    Args:
        url: Direct file URL, YouTube URL, or podcast-episode URL.
        language: ISO 639-1 code (e.g. ``"en"``, ``"es"``). Auto-detect if omitted.
        diarize: Label speakers in the transcript when True.
        word_timestamps: Include per-word timing when True.

    Returns:
        On success, a ``ToolSuccess`` with ``job_id``, ``status``,
        ``transcript_preview`` (first 300 characters), and ``url_to_full``.
        Re-running the same URL within 24 hours returns the cached ``job_id``.

        On failure, a ``ToolFailure`` with a structured ``error`` object.
    """
    raise NotImplementedError


async def transcribe_file(
    path: str,
    language: str | None = None,
    diarize: bool = False,
    word_timestamps: bool = False,
) -> ToolResult:
    """Transcribe a local audio or video file.

    Args:
        path: Absolute path to a local media file. The path is never sent
            to telemetry — only an anonymous event count is recorded.
        language: ISO 639-1 code (e.g. ``"en"``, ``"es"``). Auto-detect if omitted.
        diarize: Label speakers in the transcript when True.
        word_timestamps: Include per-word timing when True.

    Returns:
        ``ToolSuccess`` with job metadata on success, ``ToolFailure`` with
        a structured error on failure.
    """
    raise NotImplementedError


async def get_job_status(job_id: str) -> ToolResult:
    """Poll progress of a transcription job.

    Args:
        job_id: Identifier returned by ``transcribe_url`` or ``transcribe_file``.

    Returns:
        ``ToolSuccess`` with the current ``status`` (one of ``queued``,
        ``running``, ``done``, ``failed``), or ``ToolFailure`` if the
        ``job_id`` is unknown.
    """
    raise NotImplementedError


async def get_transcript(
    job_id: str,
    format: TranscriptFormat = "txt",
) -> ToolResult:
    """Fetch a finished transcript in the requested format.

    Args:
        job_id: Identifier of a completed job.
        format: One of ``txt``, ``json``, ``srt``, ``vtt``, ``docx``.

    Returns:
        ``ToolSuccess`` with ``transcript_preview`` (first 300 characters)
        and ``url_to_full`` pointing to the hosted transcript. Large
        transcripts are not inlined — fetch the full artifact via the URL.
    """
    raise NotImplementedError


async def list_recent_jobs(limit: int = 10) -> ListJobsResult:
    """List recent jobs from the local SQLite cache.

    The cache lives under ``~/.whipscribe-mcp/jobs.db`` and never leaves
    the machine. It records only ``job_id``, timestamp, status, and source
    type — never URLs, file paths, or transcript text.

    Args:
        limit: Maximum number of jobs to return, newest first.
    """
    raise NotImplementedError


TOOLS = (
    transcribe_url,
    transcribe_file,
    get_job_status,
    get_transcript,
    list_recent_jobs,
)

__all__ = [
    "TOOLS",
    "TranscriptFormat",
    "JobStatus",
    "ToolResult",
    "ToolSuccess",
    "ToolFailure",
    "RecentJob",
    "ListJobsResult",
    "ListJobsSuccess",
    "transcribe_url",
    "transcribe_file",
    "get_job_status",
    "get_transcript",
    "list_recent_jobs",
]
