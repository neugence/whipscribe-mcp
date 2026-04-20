"""MCP stdio server for Whipscribe.

Wires the five tool handlers in :mod:`whipscribe_mcp.tools` into a
Model Context Protocol server over stdio. The server owns one shared
:class:`~whipscribe_mcp.client.WhipscribeClient` and one shared
:class:`~whipscribe_mcp.cache.JobCache` for the lifetime of the process.

Stdio discipline: every log line goes to stderr (configured in
:mod:`whipscribe_mcp.__init__`). Nothing except MCP protocol frames
ever reaches stdout.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from . import tools as tool_handlers
from .cache import JobCache
from .client import WhipscribeClient
from .errors import BETA_NOTICE, ErrorObject, ToolError
from .telemetry import emit as emit_telemetry

log = structlog.get_logger()

SERVER_NAME = "whipscribe-mcp"

_TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="transcribe_url",
        description=(
            "Transcribe audio or video from a URL (direct media link, "
            "podcast episode, or Creative-Commons YouTube). Polls until "
            "the job is done or the poll timeout elapses, then returns "
            "a transcript preview plus a link to the full transcript."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct media URL or supported share URL.",
                },
                "language": {
                    "type": ["string", "null"],
                    "description": (
                        "ISO 639-1 language code (e.g. 'en', 'es'). "
                        "Auto-detect when omitted."
                    ),
                },
                "diarize": {
                    "type": "boolean",
                    "default": False,
                    "description": "Label speakers in the transcript.",
                },
                "word_timestamps": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include per-word timing.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="transcribe_file",
        description=(
            "Transcribe a local audio or video file. The path is never "
            "sent to telemetry. Polls until the job is done or the poll "
            "timeout elapses."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to a local media file.",
                },
                "language": {
                    "type": ["string", "null"],
                    "description": "ISO 639-1 language code. Auto-detect when omitted.",
                },
                "diarize": {
                    "type": "boolean",
                    "default": False,
                    "description": "Label speakers in the transcript.",
                },
                "word_timestamps": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include per-word timing.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_job_status",
        description="Poll the current status of a transcription job by job_id.",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Identifier returned by a previous transcribe_* call.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_transcript",
        description=(
            "Fetch a finished transcript in the requested format. "
            "Returns a 300-character preview plus a link to the full "
            "transcript; large transcripts are not inlined in the MCP "
            "response."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Identifier of a completed job.",
                },
                "format": {
                    "type": "string",
                    "enum": ["txt", "json", "srt", "vtt", "docx"],
                    "default": "txt",
                    "description": "Output format.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_recent_jobs",
        description=(
            "List jobs submitted from this machine (local SQLite cache). "
            "Stores only job_id, source kind, status, duration, and "
            "created-at timestamp — never URLs, file paths, or transcripts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                    "description": "Maximum number of jobs to return.",
                },
            },
            "additionalProperties": False,
        },
    ),
]


def _serialize(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


def _failure_payload(error: ToolError) -> dict[str, Any]:
    return {"ok": False, "error": error.to_object(), "beta_notice": BETA_NOTICE}


def _unknown_failure(message: str = "Internal error.") -> dict[str, Any]:
    obj: ErrorObject = {"code": "unknown_error", "message": message, "retryable": False}
    return {"ok": False, "error": obj, "beta_notice": BETA_NOTICE}


def build_server(
    *,
    client: WhipscribeClient,
    cache: JobCache,
) -> Server:
    """Construct an MCP :class:`Server` bound to the given client and cache."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[Tool]:
        return list(_TOOL_DEFINITIONS)

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        args = arguments or {}
        start = time.perf_counter()
        error_code: str | None = None
        try:
            payload = await _dispatch(name, args, client=client, cache=cache)
            if isinstance(payload, dict) and payload.get("ok") is False:
                err = payload.get("error")
                if isinstance(err, dict):
                    code = err.get("code")
                    if isinstance(code, str):
                        error_code = code
            return _serialize(payload)
        except ToolError as exc:
            error_code = exc.code
            return _serialize(_failure_payload(exc))
        except Exception as exc:
            error_code = "unknown_error"
            log.error(
                "tool_unhandled_exception",
                tool=name,
                exc_class=exc.__class__.__name__,
            )
            return _serialize(_unknown_failure())
        finally:
            emit_telemetry(
                tool=name,
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=error_code,
                version=__version__,
            )

    return server


async def _dispatch(
    name: str,
    args: dict[str, Any],
    *,
    client: WhipscribeClient,
    cache: JobCache,
) -> Any:
    if name == "transcribe_url":
        return await tool_handlers.transcribe_url(
            args["url"],
            client=client,
            cache=cache,
            language=args.get("language"),
            diarize=bool(args.get("diarize", False)),
            word_timestamps=bool(args.get("word_timestamps", False)),
        )
    if name == "transcribe_file":
        return await tool_handlers.transcribe_file(
            args["path"],
            client=client,
            cache=cache,
            language=args.get("language"),
            diarize=bool(args.get("diarize", False)),
            word_timestamps=bool(args.get("word_timestamps", False)),
        )
    if name == "get_job_status":
        return await tool_handlers.get_job_status(
            args["job_id"],
            client=client,
            cache=cache,
        )
    if name == "get_transcript":
        return await tool_handlers.get_transcript(
            args["job_id"],
            client=client,
            cache=cache,
            format=args.get("format", "txt"),
        )
    if name == "list_recent_jobs":
        return await tool_handlers.list_recent_jobs(
            int(args.get("limit", 10)),
            cache=cache,
        )
    raise ToolError(
        code="invalid_input",
        message=f"Unknown tool: {name}",
        retryable=False,
    )


async def _serve() -> None:
    log.info("whipscribe_mcp_start", version=__version__)
    async with WhipscribeClient() as client, JobCache() as cache:
        server = build_server(client=client, cache=cache)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )


def run_stdio() -> None:
    """Entry point used by :mod:`whipscribe_mcp.__main__`."""
    asyncio.run(_serve())


__all__ = ["SERVER_NAME", "build_server", "run_stdio"]
