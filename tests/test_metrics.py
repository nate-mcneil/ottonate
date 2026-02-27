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


class TestGetStageStats:
    @pytest.mark.asyncio
    async def test_aggregates_by_stage(self, store):
        await store.record_stage("o/r#1", "planning", "otto-planner", 0.10, 5, False, 0)
        await store.record_stage("o/r#2", "planning", "otto-planner", 0.20, 10, False, 0)
        await store.record_stage("o/r#1", "planning", "otto-planner", 0.05, 3, False, 1)
        await store.record_stage(
            "o/r#1", "implementing", "otto-implementer", 1.0, 40, True, 0, was_stuck=True
        )

        stats = await store.get_stage_stats()

        assert len(stats) == 2
        planning = next(s for s in stats if s["stage"] == "planning")
        assert planning["runs"] == 3
        assert planning["retries"] == 1
        assert planning["stuck_count"] == 0
        assert planning["total_cost"] == pytest.approx(0.35)

        impl = next(s for s in stats if s["stage"] == "implementing")
        assert impl["runs"] == 1
        assert impl["stuck_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_db(self, store):
        stats = await store.get_stage_stats()
        assert stats == []

    @pytest.mark.asyncio
    async def test_days_filter(self, store):
        await store.record_stage("o/r#1", "planning", "otto-planner", 0.10, 5, False, 0)
        stats = await store.get_stage_stats(days=7)
        assert len(stats) == 1


class TestGetAllIssueSummaries:
    @pytest.mark.asyncio
    async def test_returns_all_issues(self, store):
        await store.record_stage("o/r#1", "planning", "p", 0.10, 5, False, 0)
        await store.record_stage("o/r#1", "implementing", "i", 0.50, 20, False, 0)
        await store.record_stage("o/r#2", "planning", "p", 0.20, 10, False, 0)

        summaries = await store.get_all_issue_summaries()

        assert len(summaries) == 2
        refs = {s.issue_ref for s in summaries}
        assert refs == {"o/r#1", "o/r#2"}

    @pytest.mark.asyncio
    async def test_empty_db(self, store):
        summaries = await store.get_all_issue_summaries()
        assert summaries == []


class TestGetRecentCompletions:
    @pytest.mark.asyncio
    async def test_returns_issues_with_merge_ready(self, store):
        await store.record_stage("o/r#1", "planning", "p", 0.10, 5, False, 0)
        await store.record_stage("o/r#1", "agentMergeReady", None, 0.0, 0, False, 0)
        await store.record_stage("o/r#2", "planning", "p", 0.20, 10, False, 0)

        completions = await store.get_recent_completions()

        assert len(completions) == 1
        assert completions[0]["issue_ref"] == "o/r#1"
        assert completions[0]["total_cost"] == pytest.approx(0.10)

    @pytest.mark.asyncio
    async def test_empty_db(self, store):
        completions = await store.get_recent_completions()
        assert completions == []


class TestGetThroughputStats:
    @pytest.mark.asyncio
    async def test_returns_stats(self, store):
        await store.record_stage("o/r#1", "planning", "p", 0.10, 5, False, 0)
        await store.record_stage("o/r#1", "agentMergeReady", None, 0.0, 0, False, 0)
        await store.record_stage("o/r#2", "planning", "p", 0.50, 10, False, 0)

        stats = await store.get_throughput_stats()

        assert stats["total_issues"] == 2
        assert stats["completed_issues"] == 1
        assert stats["total_cost"] == pytest.approx(0.60)
        assert stats["avg_cost_per_issue"] == pytest.approx(0.30)

    @pytest.mark.asyncio
    async def test_empty_db(self, store):
        stats = await store.get_throughput_stats()
        assert stats["total_issues"] == 0
        assert stats["completed_issues"] == 0
        assert stats["total_cost"] == 0.0
