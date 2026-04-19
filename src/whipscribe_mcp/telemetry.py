"""Anonymous usage telemetry for whipscribe-mcp.

This module is the complete set of telemetry behavior in this package.
Inspect it, fork it, or disable it — nothing else in the package talks to
a telemetry endpoint.

What this module sends:
    - Anonymous install hash: ``sha256(random_per_install_uuid + public_salt)[:16]``
    - Package version, OS name, Python version (major.minor.patch)
    - Tool name, duration in milliseconds, error code (or ``None``)

What this module can never send — by construction, not policy:
    - URLs you transcribe
    - Local file paths
    - API keys
    - Transcript text
    - Email or any personally identifying information

The install ID is a random UUIDv4 written once to
``~/.whipscribe-mcp/install_id``. Delete that file to rotate the
anonymous identifier. The raw ID never leaves the machine — only the
salted hash does.

Opt out with ``WHIPSCRIBE_MCP_TELEMETRY=0``. All network calls are
best-effort with a short timeout and silent on failure.
"""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import uuid
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger()

TELEMETRY_ENDPOINT = "https://whipscribe.com/api/v1/telemetry"
TELEMETRY_TIMEOUT_SECONDS = 2.0

# Public, non-secret salt. Rotating it invalidates all existing install
# hashes, which is intentional if a salt rotation is ever needed. The
# hash is designed to be opaque, not confidential.
PUBLIC_SALT = "whipscribe-mcp.v1.telemetry"


def _install_id_path() -> Path:
    return Path.home() / ".whipscribe-mcp" / "install_id"


def _load_or_create_install_id() -> str:
    path = _install_id_path()
    if path.exists():
        try:
            stored = path.read_text().strip()
            if stored:
                return stored
        except OSError:
            pass

    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id)
    except OSError:
        # If the home directory is not writable, fall back to an ephemeral
        # UUID. Still anonymous, just not stable across runs — better than
        # failing a tool call over telemetry bookkeeping.
        return new_id
    return new_id


def install_hash() -> str:
    """Return the 16-character anonymous install hash.

    ``sha256(install_id + PUBLIC_SALT)[:16]``. Deterministic per machine
    while ``~/.whipscribe-mcp/install_id`` exists; rotate by deleting it.
    """
    install_id = _load_or_create_install_id()
    digest = hashlib.sha256(f"{install_id}{PUBLIC_SALT}".encode()).hexdigest()
    return digest[:16]


def is_enabled() -> bool:
    """Telemetry is on unless ``WHIPSCRIBE_MCP_TELEMETRY`` is ``0``/``false``/``no``/``off``."""
    value = os.environ.get("WHIPSCRIBE_MCP_TELEMETRY", "1").strip().lower()
    return value not in {"0", "false", "no", "off", ""}


def _environment_fields() -> dict[str, str]:
    return {
        "os": platform.system(),
        "python": (
            f"{sys.version_info.major}."
            f"{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        ),
    }


def emit(
    *,
    tool: str,
    duration_ms: int,
    error_code: str | None,
    version: str,
) -> None:
    """Fire-and-forget telemetry event.

    Contract:
        - Never raises. Never writes to stdout.
        - Blocks for at most ``TELEMETRY_TIMEOUT_SECONDS`` per call.
        - Silently returns when ``WHIPSCRIBE_MCP_TELEMETRY`` is disabled
          or the endpoint is unreachable.

    Args:
        tool: The tool name, e.g. ``"transcribe_url"``.
        duration_ms: Wall-clock duration of the tool call in milliseconds.
        error_code: The structured error code on failure, or ``None`` on success.
        version: The running package version (pulled from ``__version__``).
    """
    if not is_enabled():
        return

    payload = {
        "install_hash": install_hash(),
        "version": version,
        "tool": tool,
        "duration_ms": duration_ms,
        "error_code": error_code,
        **_environment_fields(),
    }

    try:
        httpx.post(
            TELEMETRY_ENDPOINT,
            json=payload,
            timeout=TELEMETRY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        log.debug("telemetry_send_failed", tool=tool)
    except Exception:
        log.debug("telemetry_unexpected", tool=tool)


__all__ = [
    "PUBLIC_SALT",
    "TELEMETRY_ENDPOINT",
    "TELEMETRY_TIMEOUT_SECONDS",
    "emit",
    "install_hash",
    "is_enabled",
]
