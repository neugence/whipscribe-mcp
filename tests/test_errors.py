"""Tests for the structured error contract."""

from __future__ import annotations

from whipscribe_mcp.errors import BETA_NOTICE, ToolError


class TestToolError:
    def test_to_object_shape(self) -> None:
        err = ToolError("rate_limited", "Too many requests.", retryable=True)
        obj = err.to_object()
        assert obj == {
            "code": "rate_limited",
            "message": "Too many requests.",
            "retryable": True,
        }

    def test_default_not_retryable(self) -> None:
        err = ToolError("invalid_input", "bad arg")
        assert err.to_object()["retryable"] is False

    def test_str_carries_message(self) -> None:
        err = ToolError("server_error", "boom")
        assert "boom" in str(err)


class TestBetaNotice:
    def test_present_and_mentions_beta(self) -> None:
        assert BETA_NOTICE
        assert "beta" in BETA_NOTICE.lower()

    def test_links_terms_page(self) -> None:
        assert "https://whipscribe.com/terms" in BETA_NOTICE
