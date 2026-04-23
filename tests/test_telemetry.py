"""Tests for the telemetry redaction and opt-out contract.

These tests defend the README privacy promise: telemetry never sends
URLs, local file paths, API keys, or transcript text. The contract is
enforced structurally by the ``emit`` signature — these tests check
that the signature stays narrow.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from whipscribe_mcp import telemetry

_INSTALL_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_ALLOWED_PARAM_NAMES = {"tool", "duration_ms", "error_code", "version"}


class TestEmitContract:
    """Structural guarantees on what telemetry can ever send."""

    def test_emit_only_accepts_safe_fields(self) -> None:
        sig = inspect.signature(telemetry.emit)
        param_names = set(sig.parameters) - {"self"}
        assert param_names == _ALLOWED_PARAM_NAMES, (
            "telemetry.emit grew a new parameter — confirm it cannot leak "
            "URLs, paths, API keys, or transcript content before allowlisting it."
        )

    def test_emit_silent_when_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WHIPSCRIBE_MCP_TELEMETRY", "0")
        called: list[object] = []

        def _spy(*args: object, **kwargs: object) -> None:
            called.append((args, kwargs))

        monkeypatch.setattr(telemetry.httpx, "post", _spy)
        telemetry.emit(tool="transcribe_url", duration_ms=10, error_code=None, version="0.1.0")
        assert called == [], "telemetry.emit must not network when disabled"

    def test_emit_swallows_network_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> None:
            raise telemetry.httpx.ConnectError("simulated offline")

        monkeypatch.setattr(telemetry.httpx, "post", _boom)
        telemetry.emit(tool="transcribe_url", duration_ms=10, error_code=None, version="0.1.0")


class TestInstallHash:
    def test_install_hash_format(self) -> None:
        result = telemetry.install_hash()
        assert _INSTALL_HASH_RE.match(result), result

    def test_install_hash_stable_across_calls(self) -> None:
        first = telemetry.install_hash()
        second = telemetry.install_hash()
        assert first == second

    def test_install_hash_rotates_on_id_deletion(self, tmp_path: Path) -> None:
        first = telemetry.install_hash()
        id_path = telemetry._install_id_path()
        if id_path.exists():
            id_path.unlink()
        second = telemetry.install_hash()
        assert first != second, "Deleting the install_id file must rotate the hash"


class TestEnabledFlag:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("anything-else", True),
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
            ("", False),
        ],
    )
    def test_env_value_parsing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
        expected: bool,
    ) -> None:
        monkeypatch.setenv("WHIPSCRIBE_MCP_TELEMETRY", value)
        assert telemetry.is_enabled() is expected

    def test_default_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WHIPSCRIBE_MCP_TELEMETRY", raising=False)
        assert telemetry.is_enabled() is True


class TestEndpointApproved:
    def test_endpoint_uses_approved_hostname(self) -> None:
        assert telemetry.TELEMETRY_ENDPOINT.startswith("https://whipscribe.com/")

    def test_short_timeout(self) -> None:
        assert telemetry.TELEMETRY_TIMEOUT_SECONDS <= 5.0


class TestEmitStartup:
    """Per-process startup ping: once-and-only-once, gated on opt-out."""

    def _reset_once_flag(self) -> None:
        # Module-level guard that keeps emit_startup from firing twice per
        # process. Tests reset it explicitly so each test starts fresh.
        telemetry._startup_emitted = False

    def test_disabled_emits_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._reset_once_flag()
        monkeypatch.setenv("WHIPSCRIBE_MCP_TELEMETRY", "0")
        calls: list[object] = []
        monkeypatch.setattr(
            telemetry.httpx,
            "post",
            lambda *a, **kw: calls.append((a, kw)),
        )
        telemetry.emit_startup(version="0.1.2")
        assert calls == [], "emit_startup must respect the opt-out env var"

    def test_enabled_emits_once_per_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._reset_once_flag()
        monkeypatch.setenv("WHIPSCRIBE_MCP_TELEMETRY", "1")
        calls: list[dict[str, object]] = []

        def _spy(*args: object, **kwargs: object) -> None:
            # httpx.post signature in emit() is (url, json=..., timeout=...).
            calls.append({"args": args, "json": kwargs.get("json")})

        monkeypatch.setattr(telemetry.httpx, "post", _spy)

        telemetry.emit_startup(version="0.1.2")
        telemetry.emit_startup(version="0.1.2")
        telemetry.emit_startup(version="0.1.2")

        assert len(calls) == 1, "emit_startup must fire exactly once per process"
        body = calls[0]["json"]
        assert isinstance(body, dict)
        assert body["tool"] == telemetry.STARTUP_TOOL_NAME
        assert body["duration_ms"] == 0
        assert body["error_code"] is None
        assert body["version"] == "0.1.2"
        # Structural: no rogue fields that could leak user data.
        assert set(body.keys()) == {
            "install_hash",
            "version",
            "tool",
            "duration_ms",
            "error_code",
            "os",
            "python",
        }

    def test_swallows_network_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._reset_once_flag()
        monkeypatch.setenv("WHIPSCRIBE_MCP_TELEMETRY", "1")

        def _boom(*args: object, **kwargs: object) -> None:
            raise telemetry.httpx.ConnectError("simulated offline")

        monkeypatch.setattr(telemetry.httpx, "post", _boom)
        # Must not raise, must not abort the server startup path.
        telemetry.emit_startup(version="0.1.2")
