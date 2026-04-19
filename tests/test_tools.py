"""Tests for the tool layer.

Focus on the contracts that aren't visible from client.py alone:
* Status normalization (backend ``processing`` → public ``running``).
* Idempotency-key derivation is deterministic for the same input.
* Preview building (truncation, whitespace collapse).
* Tool handlers never raise across the MCP boundary — failures
  return a structured ``ToolFailure`` object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from whipscribe_mcp import tools
from whipscribe_mcp.tools import (
    _build_preview,
    _key_for_file,
    _key_for_url,
    _normalize_status,
)


class TestStatusNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("queued", "queued"),
            ("processing", "running"),
            ("running", "running"),
            ("done", "done"),
            ("completed", "done"),
            ("failed", "failed"),
            ("error", "failed"),
            ("PROCESSING", "running"),
            ("unknown-future-value", "queued"),
            (None, "queued"),
            (123, "queued"),
        ],
    )
    def test_normalize(self, raw: Any, expected: str) -> None:
        assert _normalize_status(raw) == expected


class TestKeyDerivation:
    def test_url_key_deterministic(self) -> None:
        a = _key_for_url("https://example.com/a.mp3", "en")
        b = _key_for_url("https://example.com/a.mp3", "en")
        assert a == b

    def test_url_key_changes_with_url(self) -> None:
        a = _key_for_url("https://example.com/a.mp3", "en")
        b = _key_for_url("https://example.com/b.mp3", "en")
        assert a != b

    def test_url_key_changes_with_language(self) -> None:
        a = _key_for_url("https://example.com/a.mp3", "en")
        b = _key_for_url("https://example.com/a.mp3", "es")
        assert a != b

    def test_url_key_format(self) -> None:
        key = _key_for_url("https://example.com/a.mp3", None)
        assert key.startswith("u-")
        assert len(key) == 50  # "u-" + 48 hex chars

    def test_file_key_changes_with_size(self, tmp_path: Path) -> None:
        path = tmp_path / "media.mp3"
        path.write_bytes(b"x" * 10)
        key_a = _key_for_file(path, "en")
        path.write_bytes(b"x" * 20)
        key_b = _key_for_file(path, "en")
        assert key_a != key_b

    def test_file_key_returns_none_for_missing(self, tmp_path: Path) -> None:
        assert _key_for_file(tmp_path / "missing.mp3", "en") is None


class TestPreview:
    def test_short_text_returned_verbatim(self) -> None:
        assert _build_preview("hello world") == "hello world"

    def test_whitespace_collapsed(self) -> None:
        assert _build_preview("  hello\n\n  world  ") == "hello world"

    def test_long_text_truncated(self) -> None:
        text = "a" * 500
        result = _build_preview(text)
        assert len(result) == 300
        assert result.endswith("…")


class TestTranscribeUrlValidation:
    @pytest.mark.asyncio
    async def test_empty_url_returns_failure(self) -> None:
        result = await tools.transcribe_url("", client=None)  # type: ignore[arg-type]
        assert result == {
            "ok": False,
            "error": {
                "code": "invalid_input",
                "message": "url must be a non-empty string.",
                "retryable": False,
            },
        }


class TestTranscribeFileValidation:
    @pytest.mark.asyncio
    async def test_missing_file_returns_failure(self, tmp_path: Path) -> None:
        result = await tools.transcribe_file(
            str(tmp_path / "nope.mp3"),
            client=None,  # type: ignore[arg-type]
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "file_not_found"  # type: ignore[index]


class TestGetJobStatusValidation:
    @pytest.mark.asyncio
    async def test_empty_job_id_returns_failure(self) -> None:
        result = await tools.get_job_status("", client=None)  # type: ignore[arg-type]
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_input"  # type: ignore[index]


class TestListRecentJobsValidation:
    @pytest.mark.asyncio
    async def test_invalid_limit_returns_failure(self) -> None:
        result = await tools.list_recent_jobs("not-an-int", cache=None)  # type: ignore[arg-type]
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_input"  # type: ignore[index]
