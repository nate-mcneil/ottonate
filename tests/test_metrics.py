"""Tests for build_issue_metrics -- deriving metrics from GitHub timeline + structured comments."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ottonate.metrics import IssueMetrics, build_issue_metrics, parse_stage_comments

OTTO_COMMENT_TEMPLATE = "<!-- otto:{} -->"


class TestIssueMetricsNeedsRetro:
    def test_no_retries_no_stuck(self):
        m = IssueMetrics(issue_ref="o/r#1")
        assert m.needs_retro is False

    def test_needs_retro_with_retries(self):
        m = IssueMetrics(issue_ref="o/r#1", total_retries=1)
        assert m.needs_retro is True

    def test_needs_retro_with_stuck(self):
        m = IssueMetrics(issue_ref="o/r#1", was_stuck=True)
        assert m.needs_retro is True


class TestParseStageComments:
    def test_parses_valid_comment(self):
        comments = [
            '<!-- otto:{"stage":"planning","agent":"otto-planner","cost_usd":0.05,'
            '"turns_used":10,"is_error":false,"retry_number":0,"was_stuck":false} -->'
        ]
        stages = parse_stage_comments(comments)
        assert len(stages) == 1
        assert stages[0]["stage"] == "planning"
        assert stages[0]["agent"] == "otto-planner"
        assert stages[0]["cost_usd"] == 0.05

    def test_ignores_non_otto_comments(self):
        comments = [
            "Normal issue comment",
            "## Development Plan\n\nDo the thing",
            '<!-- otto:{"stage":"planning","agent":"otto-planner"} -->',
        ]
        stages = parse_stage_comments(comments)
        assert len(stages) == 1
        assert stages[0]["stage"] == "planning"

    def test_handles_malformed_json(self):
        comments = [
            "<!-- otto:{bad json} -->",
            '<!-- otto:{"stage":"planning","agent":"otto-planner"} -->',
        ]
        stages = parse_stage_comments(comments)
        assert len(stages) == 1

    def test_empty_comments(self):
        assert parse_stage_comments([]) == []

    def test_comment_with_visible_text_after_meta(self):
        comments = [
            '<!-- otto:{"stage":"stuck","agent":null,"was_stuck":true,'
            '"stuck_reason":"CI blocked"} -->\nOttonate agent stopped: CI blocked'
        ]
        stages = parse_stage_comments(comments)
        assert len(stages) == 1
        assert stages[0]["was_stuck"] is True
        assert stages[0]["stuck_reason"] == "CI blocked"


class TestBuildIssueMetrics:
    @pytest.mark.asyncio
    async def test_clean_issue(self):
        github = AsyncMock()
        github.get_issue_timeline = AsyncMock(
            return_value=[
                {
                    "event": "labeled",
                    "label": "agentPlanning",
                    "created_at": "2025-01-01T00:00:00Z",
                },
                {
                    "event": "unlabeled",
                    "label": "agentPlanning",
                    "created_at": "2025-01-01T01:00:00Z",
                },
                {
                    "event": "labeled",
                    "label": "agentPlanReview",
                    "created_at": "2025-01-01T01:00:00Z",
                },
            ]
        )
        github.get_comments = AsyncMock(
            return_value=[
                '<!-- otto:{"stage":"planning","agent":"otto-planner","cost_usd":0.05,'
                '"turns_used":10,"is_error":false,"retry_number":0,"was_stuck":false} -->',
            ]
        )

        m = await build_issue_metrics(github, "o", "r", 1)

        assert m.issue_ref == "o/r#1"
        assert m.was_stuck is False
        assert m.total_retries == 0
        assert m.needs_retro is False
        assert m.total_stages == 1
        assert m.total_cost_usd == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_stuck_issue(self):
        github = AsyncMock()
        github.get_issue_timeline = AsyncMock(
            return_value=[
                {
                    "event": "labeled",
                    "label": "agentPlanning",
                    "created_at": "2025-01-01T00:00:00Z",
                },
                {"event": "labeled", "label": "agentStuck", "created_at": "2025-01-01T01:00:00Z"},
            ]
        )
        github.get_comments = AsyncMock(
            return_value=[
                '<!-- otto:{"stage":"stuck","agent":null,'
                '"was_stuck":true,"stuck_reason":"blocked"} -->'
            ]
        )

        m = await build_issue_metrics(github, "o", "r", 1)

        assert m.was_stuck is True
        assert m.stuck_reasons == ["blocked"]
        assert m.needs_retro is True

    @pytest.mark.asyncio
    async def test_retried_issue(self):
        github = AsyncMock()
        github.get_issue_timeline = AsyncMock(
            return_value=[
                {
                    "event": "labeled",
                    "label": "agentPlanning",
                    "created_at": "2025-01-01T00:00:00Z",
                },
                {
                    "event": "unlabeled",
                    "label": "agentPlanning",
                    "created_at": "2025-01-01T01:00:00Z",
                },
                {
                    "event": "labeled",
                    "label": "agentPlanReview",
                    "created_at": "2025-01-01T01:00:00Z",
                },
                {
                    "event": "unlabeled",
                    "label": "agentPlanReview",
                    "created_at": "2025-01-01T02:00:00Z",
                },
                {
                    "event": "labeled",
                    "label": "agentPlanning",
                    "created_at": "2025-01-01T02:00:00Z",
                },
            ]
        )
        github.get_comments = AsyncMock(
            return_value=[
                '<!-- otto:{"stage":"planning","agent":"otto-planner","cost_usd":0.05,'
                '"turns_used":10,"is_error":false,"retry_number":0,"was_stuck":false} -->',
                '<!-- otto:{"stage":"planning","agent":"otto-planner","cost_usd":0.03,'
                '"turns_used":8,"is_error":false,"retry_number":1,"was_stuck":false} -->',
            ]
        )

        m = await build_issue_metrics(github, "o", "r", 1)

        assert m.total_retries == 1
        assert m.needs_retro is True
        assert m.total_cost_usd == pytest.approx(0.08)

    @pytest.mark.asyncio
    async def test_empty_timeline(self):
        github = AsyncMock()
        github.get_issue_timeline = AsyncMock(return_value=[])
        github.get_comments = AsyncMock(return_value=[])

        m = await build_issue_metrics(github, "o", "r", 1)

        assert m.total_stages == 0
        assert m.total_retries == 0
        assert m.was_stuck is False
        assert m.needs_retro is False
