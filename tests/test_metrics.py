"""Tests for the MetricsStore."""

from __future__ import annotations

import pytest

from ottonate.metrics import IssueMetrics, MetricsStore


@pytest.fixture
async def store(tmp_path) -> MetricsStore:
    db_path = tmp_path / "test.db"
    s = MetricsStore(db_path)
    await s.init_db()
    yield s
    await s.close()


class TestInitDb:
    @pytest.mark.asyncio
    async def test_creates_table(self, store):
        import aiosqlite

        async with aiosqlite.connect(store._db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stage_events'"
            )
            row = await cursor.fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_idempotent(self, store):
        await store.init_db()
        await store.init_db()


class TestRecordStage:
    @pytest.mark.asyncio
    async def test_inserts_row(self, store):
        await store.record_stage(
            issue_ref="org/repo#1",
            stage="planning",
            agent="otto-planner",
            cost_usd=0.05,
            turns_used=10,
            is_error=False,
            retry_number=0,
        )
        import aiosqlite

        async with aiosqlite.connect(store._db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM stage_events")
            (count,) = await cursor.fetchone()
        assert count == 1

    @pytest.mark.asyncio
    async def test_records_stuck(self, store):
        await store.record_stage(
            issue_ref="org/repo#2",
            stage="implementing",
            agent="otto-implementer",
            cost_usd=1.0,
            turns_used=50,
            is_error=True,
            retry_number=1,
            was_stuck=True,
            stuck_reason="CI fix blocked",
        )
        import aiosqlite

        async with aiosqlite.connect(store._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM stage_events WHERE issue_ref = 'org/repo#2'")
            row = await cursor.fetchone()
        assert row["was_stuck"] == 1
        assert row["stuck_reason"] == "CI fix blocked"


class TestGetIssueSummary:
    @pytest.mark.asyncio
    async def test_aggregates(self, store):
        await store.record_stage("org/repo#3", "planning", "otto-planner", 0.05, 10, False, 0)
        await store.record_stage("org/repo#3", "planning", "otto-planner", 0.03, 8, False, 1)
        await store.record_stage(
            "org/repo#3",
            "implementing",
            "otto-implementer",
            1.0,
            50,
            True,
            0,
            was_stuck=True,
            stuck_reason="blocked",
        )

        summary = await store.get_issue_summary("org/repo#3")

        assert isinstance(summary, IssueMetrics)
        assert summary.issue_ref == "org/repo#3"
        assert summary.total_stages == 3
        assert summary.total_retries == 1
        assert summary.total_cost_usd == pytest.approx(1.08)
        assert summary.was_stuck is True
        assert summary.stuck_reasons == ["blocked"]
        assert len(summary.stages) == 3

    @pytest.mark.asyncio
    async def test_empty_issue(self, store):
        summary = await store.get_issue_summary("org/repo#999")

        assert summary.total_stages == 0
        assert summary.total_retries == 0
        assert summary.total_cost_usd == 0.0
        assert summary.was_stuck is False
        assert summary.stuck_reasons == []

    @pytest.mark.asyncio
    async def test_no_retries_no_stuck(self, store):
        await store.record_stage("org/repo#4", "planning", "otto-planner", 0.05, 10, False, 0)
        await store.record_stage(
            "org/repo#4",
            "implementing",
            "otto-implementer",
            0.5,
            30,
            False,
            0,
        )

        summary = await store.get_issue_summary("org/repo#4")

        assert summary.total_retries == 0
        assert summary.was_stuck is False
        assert summary.needs_retro is False

    @pytest.mark.asyncio
    async def test_needs_retro_with_retries(self, store):
        await store.record_stage("org/repo#5", "planning", "otto-planner", 0.05, 10, False, 0)
        await store.record_stage("org/repo#5", "planning", "otto-planner", 0.03, 8, False, 1)

        summary = await store.get_issue_summary("org/repo#5")
        assert summary.needs_retro is True

    @pytest.mark.asyncio
    async def test_needs_retro_with_stuck(self, store):
        await store.record_stage(
            "org/repo#6",
            "implementing",
            "otto-implementer",
            0.5,
            30,
            True,
            0,
            was_stuck=True,
            stuck_reason="blocked",
        )

        summary = await store.get_issue_summary("org/repo#6")
        assert summary.needs_retro is True
