"""Async HTTP client for the Whipscribe public API.

Wraps ``https://whipscribe.com/api/v1/*`` for the MCP server. The server
is the only caller — MCP tool handlers instantiate :class:`WhipscribeClient`
per invocation (or share one via the server lifetime) and translate
:class:`~whipscribe_mcp.errors.ToolError` into structured ``ErrorObject``
results at the tool boundary.

Design notes:

* Single ``httpx.AsyncClient`` per instance. Use as an async context
  manager to guarantee the connection pool is closed.
* Retries with exponential backoff on ``429``, ``502``, ``503``, ``504``
  (capped at 3 attempts). ``Retry-After`` is honored when present.
* Logging goes exclusively to stderr via the pre-configured ``structlog``
  logger. **URLs, local file paths, API keys, and transcript text are
  never logged** — only ``{endpoint, status_code, duration_ms, error_code}``.
* File uploads stream from disk; the full payload is never loaded into
  memory. The file handle is closed when the request completes.
* Errors from the API are mapped to :class:`ToolError` codes that match
  the enum in :mod:`whipscribe_mcp.errors`. Unknown backend codes fall
  back to ``server_error`` (5xx) or ``unknown_error`` (4xx without
  mapping).

Beta disclaimer: the Whipscribe API is in beta; endpoint shapes and
quotas may change without notice. This client defends against that with
defensive parsing, retries, and graceful error fallbacks — but downstream
tooling should still treat every call as potentially lossy.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import uuid
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import httpx
import structlog

from .errors import ErrorCode, ToolError

log = structlog.get_logger()

DEFAULT_API_BASE = "https://whipscribe.com/api/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
RETRY_AFTER_CAP_SECONDS = 30.0

_RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

# Per the API docs, Idempotency-Key values must match this character set,
# be at most 255 characters, and contain no whitespace.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,255}$")

TranscriptFormat = Literal["txt", "json", "srt", "vtt", "docx"]
SourceKind = Literal["upload", "url", "recording", "api"]
Tier = Literal["guest", "free", "paid"]


# Mapping from documented backend error codes (machine enum in the JSON
# ``code`` field) to the MCP-facing error codes in
# :mod:`whipscribe_mcp.errors`. Anything not listed here falls through to
# HTTP-status-based classification.
_BACKEND_CODE_MAP: dict[str, ErrorCode] = {
    "BAD_ID": "invalid_input",
    "BAD_URL": "url_unreachable",
    "BAD_SOURCE": "invalid_input",
    "BAD_MIME": "unsupported_format",
    "BAD_IDEMPOTENCY_KEY": "invalid_input",
    "MISSING_API_KEY": "auth_missing",
    "NO_CREDITS": "quota_exceeded",
    "NOT_FOUND": "job_not_found",
    "AUDIO_EXPIRED": "transcript_unavailable",
    "AUDIO_MISSING": "transcript_unavailable",
    "FILE_TOO_LARGE": "file_too_large",
    "RATE_LIMITED": "rate_limited",
    "BACKEND_ERROR": "server_error",
    "BACKEND_UNREACHABLE": "server_error",
}


def generate_idempotency_key() -> str:
    """Return a fresh UUIDv4-derived idempotency key.

    The hyphenated UUID form fits the documented character set
    (``[A-Za-z0-9_.:/-]``) and is well under the 255-character cap.
    """
    return str(uuid.uuid4())


def _validate_idempotency_key(key: str) -> None:
    if not _IDEMPOTENCY_KEY_RE.match(key):
        raise ToolError(
            code="invalid_input",
            message=(
                "idempotency_key must match [A-Za-z0-9_.:/-]{1,255} with no whitespace."
            ),
            retryable=False,
        )


def _status_to_error_code(status: int) -> ErrorCode:
    """Classify an HTTP status into a structured :class:`ErrorCode`."""
    if status == 400 or status == 422:
        return "invalid_input"
    if status == 401:
        return "auth_invalid"
    if status == 402:
        return "quota_exceeded"
    if status == 404:
        return "job_not_found"
    if status == 410:
        return "transcript_unavailable"
    if status == 413:
        return "file_too_large"
    if status == 415:
        return "unsupported_format"
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600:
        return "server_error"
    return "unknown_error"


def _status_is_retryable(status: int) -> bool:
    return status in _RETRYABLE_STATUSES


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header value in seconds, clamped and safe."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        # HTTP-date format is not required by the backend; fall back to
        # the backoff schedule rather than parse it here.
        return None
    if seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for a retry ``attempt`` (0-indexed)."""
    exp = BACKOFF_BASE_SECONDS * (2 ** attempt)
    capped = min(exp, BACKOFF_CAP_SECONDS)
    jitter = random.uniform(0.0, capped * 0.25)
    return capped + jitter


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


class WhipscribeClient:
    """Async client for the Whipscribe public v1 API.

    Reads configuration from environment variables:

    * ``WHIPSCRIBE_API_BASE`` — override base URL (default
      ``https://whipscribe.com/api/v1``).
    * ``WHIPSCRIBE_API_KEY`` — optional API key. The anonymous free tier
      works without one; a key unlocks paid quota.

    Use as an async context manager to guarantee the underlying
    :class:`httpx.AsyncClient` is closed::

        async with WhipscribeClient() as client:
            job = await client.submit_url("https://...")
    """

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        resolved_base = api_base or os.environ.get("WHIPSCRIBE_API_BASE") or DEFAULT_API_BASE
        self._base_url: str = resolved_base.rstrip("/")
        self._api_key: str | None = api_key or os.environ.get("WHIPSCRIBE_API_KEY") or None
        self._max_retries = max(0, int(max_retries))

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "whipscribe-mcp",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            headers=headers,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> WhipscribeClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Core request + retry plumbing
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        endpoint_name: str,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        """Issue an HTTP request with retry and structured error mapping.

        Args:
            method: HTTP method (``GET``, ``POST``, ``DELETE``, ...).
            path: Path under the API base, e.g. ``/jobs/{id}``.
            endpoint_name: Short label used for logging. Must not contain
                any user-supplied identifiers (URLs, paths, keys).
            json: JSON body to send, if any.
            params: Query string parameters.
            files: Streaming multipart payload. The mapping is passed
                directly to :meth:`httpx.AsyncClient.post`; use
                ``open(path, "rb")`` values so httpx streams them.
            data: Non-file multipart form fields.
            expected_statuses: Status codes treated as success. Anything
                else is converted to a :class:`ToolError`.
            idempotency_key: When set, sent as the ``Idempotency-Key``
                header. The same key is used across all retry attempts so
                the backend can deduplicate. Required for safe retries on
                non-idempotent POSTs (``/transcribe``, ``/transcribe/url``).

        Returns:
            The successful :class:`httpx.Response`.

        Raises:
            ToolError: On non-retryable failure or retries exhausted.
        """
        start = time.monotonic()
        last_error: ToolError | None = None

        request_headers: dict[str, str] | None = None
        if idempotency_key is not None:
            _validate_idempotency_key(idempotency_key)
            request_headers = {"Idempotency-Key": idempotency_key}

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    files=files,
                    data=data,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                last_error = ToolError(
                    code="network_error",
                    message=f"Request to Whipscribe API timed out: {exc.__class__.__name__}",
                    retryable=True,
                )
                log.warning(
                    "whipscribe_request_timeout",
                    endpoint=endpoint_name,
                    attempt=attempt,
                    duration_ms=_elapsed_ms(start),
                    error_code=last_error.code,
                )
            except httpx.TransportError as exc:
                last_error = ToolError(
                    code="network_error",
                    message=f"Network error contacting Whipscribe API: {exc.__class__.__name__}",
                    retryable=True,
                )
                log.warning(
                    "whipscribe_request_transport_error",
                    endpoint=endpoint_name,
                    attempt=attempt,
                    duration_ms=_elapsed_ms(start),
                    error_code=last_error.code,
                )
            else:
                if response.status_code in expected_statuses:
                    replay = response.headers.get("X-Idempotent-Replay") == "true"
                    log.info(
                        "whipscribe_request_ok",
                        endpoint=endpoint_name,
                        status_code=response.status_code,
                        duration_ms=_elapsed_ms(start),
                        replay=replay,
                    )
                    return response

                if _status_is_retryable(response.status_code) and attempt < self._max_retries:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                    log.warning(
                        "whipscribe_request_retry",
                        endpoint=endpoint_name,
                        status_code=response.status_code,
                        attempt=attempt,
                        duration_ms=_elapsed_ms(start),
                    )
                    await asyncio.sleep(delay)
                    continue

                # Non-retryable, or retries exhausted: map and raise.
                raise self._response_to_error(response, endpoint_name=endpoint_name, start=start)

            # Retry path for connection errors / timeouts.
            if attempt < self._max_retries:
                await asyncio.sleep(_backoff_delay(attempt))
                continue

            assert last_error is not None  # set in every except branch above
            log.warning(
                "whipscribe_request_failed",
                endpoint=endpoint_name,
                duration_ms=_elapsed_ms(start),
                error_code=last_error.code,
            )
            raise last_error

        # Defensive: loop above either returns, raises, or continues.
        # Re-raise the last seen transport error if we somehow fall through.
        if last_error is not None:
            raise last_error
        raise ToolError(
            code="unknown_error",
            message="Request loop exhausted without a response.",
            retryable=False,
        )

    def _response_to_error(
        self,
        response: httpx.Response,
        *,
        endpoint_name: str,
        start: float,
    ) -> ToolError:
        """Map an unsuccessful :class:`httpx.Response` to a :class:`ToolError`."""
        status = response.status_code
        backend_code: str | None = None
        message = f"Whipscribe API returned HTTP {status}."

        # Parse the documented error envelope: ``{"error": "...", "code": "..."}``.
        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            raw_code = body.get("code")
            raw_error = body.get("error")
            if isinstance(raw_code, str):
                backend_code = raw_code
            if isinstance(raw_error, str) and raw_error.strip():
                message = raw_error.strip()

        mapped: ErrorCode | None = None
        if backend_code is not None:
            mapped = _BACKEND_CODE_MAP.get(backend_code)
        error_code: ErrorCode = mapped or _status_to_error_code(status)
        retryable = _status_is_retryable(status)

        log.warning(
            "whipscribe_request_error",
            endpoint=endpoint_name,
            status_code=status,
            duration_ms=_elapsed_ms(start),
            error_code=error_code,
        )

        return ToolError(code=error_code, message=message, retryable=retryable)

    @staticmethod
    def _parse_json(response: httpx.Response, *, endpoint_name: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            log.warning(
                "whipscribe_response_decode_error",
                endpoint=endpoint_name,
                status_code=response.status_code,
                error_code="server_error",
            )
            raise ToolError(
                code="server_error",
                message="Whipscribe API returned a non-JSON response.",
                retryable=True,
            ) from exc

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    async def submit_url(
        self,
        url: str,
        *,
        language: str | None = None,
        diarize: bool = True,
        word_timestamps: bool = True,
        source: SourceKind = "url",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit a URL for transcription (``POST /transcribe/url``).

        Args:
            url: Source audio/video URL. Currently limited to
                Creative-Commons-licensed YouTube URLs for the URL path;
                direct media links are accepted where the backend can
                fetch them.
            language: Optional ISO 639-1 code. Auto-detect when ``None``.
            diarize: Include speaker labels.
            word_timestamps: Include per-word offsets.
            source: ``source`` field sent to the backend. Defaults to
                ``url``.
            idempotency_key: Sent as the ``Idempotency-Key`` header so a
                lost-response retry returns the original ``job_id``
                instead of double-charging. Auto-generates a fresh UUIDv4
                when omitted (protects in-call retries only). Pass a
                deterministic value (e.g. derived from ``url``) to
                deduplicate across separate tool invocations.

        Returns:
            Parsed JSON body — at minimum ``{"job_id": str,
            "status": "queued", "tier": int}``. For anonymous callers a
            ``claim_token`` field is also included.

        Raises:
            ToolError: On submission failure. ``url_unreachable`` when
                the backend rejects the URL, ``quota_exceeded`` on 402,
                ``rate_limited`` on 429, etc.
        """
        payload: dict[str, Any] = {
            "url": url,
            "diarize": diarize,
            "word_timestamps": word_timestamps,
            "source": source,
        }
        if language is not None:
            payload["language"] = language

        response = await self._request(
            "POST",
            "/transcribe/url",
            endpoint_name="submit_url",
            json=payload,
            expected_statuses=(200, 202),
            idempotency_key=idempotency_key or generate_idempotency_key(),
        )
        result = self._parse_json(response, endpoint_name="submit_url")
        if not isinstance(result, dict):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /transcribe/url.",
                retryable=True,
            )
        return result

    async def submit_file(
        self,
        path: str | Path,
        *,
        language: str | None = None,
        diarize: bool = True,
        word_timestamps: bool = True,
        source: SourceKind = "upload",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit a local media file for transcription (``POST /transcribe``).

        The file is streamed from disk; the full payload is never loaded
        into memory. The caller's ``path`` is not logged — only the
        endpoint name and outcome.

        Args:
            path: Absolute or relative path to a local media file.
                Supported container formats include mp3, m4a, wav, mp4,
                mov, ogg, webm, flac. Duration limit is set by the
                backend.
            language: Optional ISO 639-1 code. Auto-detect when ``None``.
            diarize: Include speaker labels.
            word_timestamps: Include per-word offsets.
            source: ``source`` field sent to the backend. Defaults to
                ``upload``.
            idempotency_key: Sent as the ``Idempotency-Key`` header so a
                lost-response retry returns the original ``job_id``.
                Auto-generates a fresh UUIDv4 when omitted. Pass a
                deterministic value (e.g. ``sha256(path|size|mtime)``)
                to deduplicate across separate tool invocations.

        Returns:
            Parsed JSON body with ``job_id``, ``status`` and tier info.

        Raises:
            ToolError: ``file_not_found`` if the path does not exist;
                ``file_too_large`` on 413; ``unsupported_format`` on 415;
                ``upload_failed`` on other transport errors.
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise ToolError(
                code="file_not_found",
                message="Local media file does not exist or is not a regular file.",
                retryable=False,
            )

        form_data: dict[str, Any] = {
            "diarize": "true" if diarize else "false",
            "word_timestamps": "true" if word_timestamps else "false",
            "source": source,
        }
        if language is not None:
            form_data["language"] = language

        # Stream the file handle; httpx closes it after the request.
        try:
            file_handle = file_path.open("rb")
        except OSError as exc:
            raise ToolError(
                code="file_not_found",
                message=f"Unable to open local media file: {exc.__class__.__name__}",
                retryable=False,
            ) from exc

        try:
            files = {"file": (file_path.name, file_handle, "application/octet-stream")}
            try:
                response = await self._request(
                    "POST",
                    "/transcribe",
                    endpoint_name="submit_file",
                    data=form_data,
                    files=files,
                    expected_statuses=(200, 202),
                    idempotency_key=idempotency_key or generate_idempotency_key(),
                )
            except ToolError as exc:
                # Re-classify generic transport/server errors on upload to
                # the more specific ``upload_failed`` contract for this
                # endpoint.
                if exc.code == "network_error":
                    raise ToolError(
                        code="upload_failed",
                        message=exc.message,
                        retryable=exc.retryable,
                    ) from exc
                raise
        finally:
            file_handle.close()

        result = self._parse_json(response, endpoint_name="submit_file")
        if not isinstance(result, dict):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /transcribe.",
                retryable=True,
            )
        return result

    async def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Fetch current status of a job (``GET /jobs/{job_id}``).

        Args:
            job_id: Identifier returned by :meth:`submit_url` or
                :meth:`submit_file`.

        Returns:
            Parsed JSON body with ``status`` (one of ``queued``,
            ``processing``, ``done``, ``failed``), optional ``progress``
            (0.0 – 1.0), ``audio_duration_seconds``, ``language``,
            ``source``, and ``error`` fields. Recommended polling
            cadence is 3 seconds while status is ``queued`` or
            ``processing``.

        Raises:
            ToolError: ``job_not_found`` on 404; ``invalid_input`` for
                malformed identifiers.
        """
        if not job_id or not isinstance(job_id, str):
            raise ToolError(
                code="invalid_input",
                message="job_id must be a non-empty string.",
                retryable=False,
            )
        response = await self._request(
            "GET",
            f"/jobs/{job_id}",
            endpoint_name="get_job_status",
        )
        result = self._parse_json(response, endpoint_name="get_job_status")
        if not isinstance(result, dict):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /jobs/{id}.",
                retryable=True,
            )
        return result

    async def get_transcript(
        self,
        job_id: str,
        *,
        format: TranscriptFormat = "txt",
    ) -> dict[str, Any] | str:
        """Download a transcript (``GET /jobs/{job_id}/result``).

        Args:
            job_id: Identifier of a completed job.
            format: One of ``txt``, ``json``, ``srt``, ``vtt``, ``docx``.
                ``json`` returns the richest payload (segments, speakers,
                word-level timing when enabled on the job). Other formats
                return plain text.

        Returns:
            For ``format="json"`` a decoded ``dict``. For all other
            formats the raw transcript text as a ``str``. The transcript
            body is returned to the caller but is never logged.

        Raises:
            ToolError: ``job_not_found`` on 404; ``transcript_unavailable``
                on 410 (retention window elapsed or artifact missing).
        """
        if not job_id or not isinstance(job_id, str):
            raise ToolError(
                code="invalid_input",
                message="job_id must be a non-empty string.",
                retryable=False,
            )
        response = await self._request(
            "GET",
            f"/jobs/{job_id}/result",
            endpoint_name="get_transcript",
            params={"format": format},
        )
        if format == "json":
            result = self._parse_json(response, endpoint_name="get_transcript")
            if not isinstance(result, dict):
                raise ToolError(
                    code="server_error",
                    message="Unexpected JSON transcript shape.",
                    retryable=True,
                )
            return result
        # For txt/srt/vtt/docx the endpoint returns plain/binary text.
        return response.text

    async def list_jobs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """List recent jobs for the caller (``GET /jobs``).

        Args:
            limit: Maximum number of jobs to return. Clamped to the
                backend's documented range of 1–100.

        Returns:
            A list of job summary dicts, newest first. Each entry
            includes ``job_id``, ``status``, ``filename``,
            ``audio_duration_seconds``, ``language``, ``source``, and
            ``created_at``.

        Raises:
            ToolError: On non-2xx responses.
        """
        clamped = max(1, min(100, int(limit)))
        response = await self._request(
            "GET",
            "/jobs",
            endpoint_name="list_jobs",
            params={"limit": clamped},
        )
        result = self._parse_json(response, endpoint_name="list_jobs")
        if not isinstance(result, list):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /jobs.",
                retryable=True,
            )
        # Drop any non-dict entries defensively — beta contract is
        # additive but a malformed row should not abort the whole list.
        return [row for row in result if isinstance(row, dict)]

    async def delete_job(self, job_id: str) -> None:
        """Cancel an in-flight job or delete a completed one (``DELETE /jobs/{id}``).

        Args:
            job_id: Identifier of the job to cancel or delete.

        Raises:
            ToolError: ``job_not_found`` on 404; ``invalid_input`` for
                malformed identifiers. Credit is not refunded for
                completed jobs.
        """
        if not job_id or not isinstance(job_id, str):
            raise ToolError(
                code="invalid_input",
                message="job_id must be a non-empty string.",
                retryable=False,
            )
        await self._request(
            "DELETE",
            f"/jobs/{job_id}",
            endpoint_name="delete_job",
            expected_statuses=(200, 202, 204),
        )

    async def whoami(self) -> dict[str, Any]:
        """Return caller identity and tier info (``GET /me``).

        Returns:
            Parsed JSON body with ``email``, ``tier`` (one of ``guest``,
            ``free``, ``paid``), ``retention_days``, and ``signed_in``.

        Raises:
            ToolError: ``auth_missing`` / ``auth_invalid`` when the
                backend rejects the caller's credentials.
        """
        response = await self._request(
            "GET",
            "/me",
            endpoint_name="whoami",
        )
        result = self._parse_json(response, endpoint_name="whoami")
        if not isinstance(result, dict):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /me.",
                retryable=True,
            )
        return result

    async def claim_jobs(self, claim_tokens: list[str]) -> dict[str, Any]:
        """Claim anonymous guest jobs for the current identity (``POST /jobs/claim``).

        Args:
            claim_tokens: Claim tokens previously returned by anonymous
                ``submit_url`` / ``submit_file`` calls.

        Returns:
            Parsed JSON body, typically ``{"claimed": <int>}``.

        Raises:
            ToolError: ``invalid_input`` for an empty token list;
                transport / server errors propagate normally.
        """
        if not claim_tokens or not all(isinstance(t, str) and t for t in claim_tokens):
            raise ToolError(
                code="invalid_input",
                message="claim_tokens must be a non-empty list of strings.",
                retryable=False,
            )
        response = await self._request(
            "POST",
            "/jobs/claim",
            endpoint_name="claim_jobs",
            json={"claim_tokens": list(claim_tokens)},
        )
        result = self._parse_json(response, endpoint_name="claim_jobs")
        if not isinstance(result, dict):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /jobs/claim.",
                retryable=True,
            )
        return result

    async def get_audio_url(self, job_id: str) -> dict[str, Any]:
        """Fetch a time-limited playback URL for a job's original audio.

        Calls ``GET /jobs/{job_id}/audio/url``.

        Args:
            job_id: Identifier of the job.

        Returns:
            Parsed JSON body with ``url``, ``storage``, ``expires_in``,
            and ``retention_days``. The ``url`` is time-limited — do not
            cache past ``expires_in`` seconds.

        Raises:
            ToolError: ``transcript_unavailable`` on 410 (audio retention
                window elapsed).
        """
        if not job_id or not isinstance(job_id, str):
            raise ToolError(
                code="invalid_input",
                message="job_id must be a non-empty string.",
                retryable=False,
            )
        response = await self._request(
            "GET",
            f"/jobs/{job_id}/audio/url",
            endpoint_name="get_audio_url",
        )
        result = self._parse_json(response, endpoint_name="get_audio_url")
        if not isinstance(result, dict):
            raise ToolError(
                code="server_error",
                message="Unexpected response shape from /jobs/{id}/audio/url.",
                retryable=True,
            )
        return result


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "SourceKind",
    "Tier",
    "TranscriptFormat",
    "WhipscribeClient",
    "generate_idempotency_key",
]
