"""Shared pytest fixtures and config for whipscribe-mcp tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate every test from the user's real env and home directory.

    Tests must never write to ``~/.whipscribe-mcp`` on the developer's
    actual machine, and must never inherit ``WHIPSCRIBE_*`` env vars
    from the surrounding shell.
    """
    for var in list(os.environ):
        if var.startswith("WHIPSCRIBE_"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


@pytest.fixture
def telemetry_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHIPSCRIBE_MCP_TELEMETRY", "0")
