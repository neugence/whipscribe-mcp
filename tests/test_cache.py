"""Tests for the local SQLite job cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from whipscribe_mcp.cache import JobCache


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "jobs.db"


class TestJobCache:
    @pytest.mark.asyncio
    async def test_record_and_list(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(
                job_id="j1",
                source="url",
                status="queued",
                created_at="2026-04-19T10:00:00+00:00",
            )
            await cache.record_job(
                job_id="j2",
                source="file",
                status="done",
                duration_sec=120.5,
                created_at="2026-04-19T11:00:00+00:00",
            )
            rows = await cache.list_recent(10)

        assert [row["job_id"] for row in rows] == ["j2", "j1"]
        assert rows[0]["duration_sec"] == 120.5
        assert rows[1]["duration_sec"] is None

    @pytest.mark.asyncio
    async def test_record_replaces_existing(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(job_id="j1", source="url", status="queued")
            await cache.record_job(job_id="j1", source="url", status="done", duration_sec=42.0)
            rows = await cache.list_recent(10)
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["duration_sec"] == 42.0

    @pytest.mark.asyncio
    async def test_update_status(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(job_id="j1", source="url", status="queued")
            await cache.update_status("j1", "running")
            rows = await cache.list_recent(10)
        assert rows[0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_update_status_with_duration(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(job_id="j1", source="url", status="queued")
            await cache.update_status("j1", "done", duration_sec=99.9)
            rows = await cache.list_recent(10)
        assert rows[0]["duration_sec"] == 99.9

    @pytest.mark.asyncio
    async def test_limit_clamped(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            for i in range(5):
                await cache.record_job(
                    job_id=f"j{i}",
                    source="url",
                    status="queued",
                    created_at=f"2026-04-19T1{i}:00:00+00:00",
                )
            assert len(await cache.list_recent(0)) == 1
            assert len(await cache.list_recent(2)) == 2
            assert len(await cache.list_recent(999)) == 5

    @pytest.mark.asyncio
    async def test_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deeply" / "nested" / "jobs.db"
        async with JobCache(nested) as cache:
            await cache.record_job(job_id="j1", source="url")
        assert nested.exists()

    @pytest.mark.asyncio
    async def test_claim_token_round_trip(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(
                job_id="j1",
                source="url",
                claim_token="tok-abc-123",
            )
            assert await cache.get_claim_token("j1") == "tok-abc-123"

    @pytest.mark.asyncio
    async def test_claim_token_missing_returns_none(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(job_id="j1", source="url")
            assert await cache.get_claim_token("j1") is None
            assert await cache.get_claim_token("j-unknown") is None

    @pytest.mark.asyncio
    async def test_claim_token_preserved_on_status_update(self, cache_path: Path) -> None:
        async with JobCache(cache_path) as cache:
            await cache.record_job(
                job_id="j1",
                source="url",
                claim_token="tok-abc-123",
            )
            # Re-recording without a token must not clobber the existing one.
            await cache.record_job(job_id="j1", source="url", status="done")
            assert await cache.get_claim_token("j1") == "tok-abc-123"
