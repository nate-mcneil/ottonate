from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ottonate.models import CIStatus, Label, ReviewStatus, StageResult, Ticket
from ottonate.pipeline import (
    Pipeline,
    _extract_plan,
    _extract_pr_number,
    _parse_quality_verdict,
    _parse_review_verdict,
    _slugify_branch,
)
from ottonate.rules import ResolvedRules


@pytest.fixture
def pipeline(config, mock_github, mock_memory):
    return Pipeline(config, mock_github, memory=mock_memory)


def _agent_result(text: str = "", is_error: bool = False) -> StageResult:
    return StageResult(text=text, session_id="s1", cost_usd=0.01, turns_used=5, is_error=is_error)


# -- Parsing helpers --


class TestExtractPlan:
    def test_finds_summary_marker(self):
        text = "preamble\n**Summary**\nThe plan\n[PLAN_COMPLETE]"
        assert _extract_plan(text) == "**Summary**\nThe plan"

    def test_fallback_strips_marker(self):
        text = "Just some text [PLAN_COMPLETE]"
        assert "PLAN_COMPLETE" not in _extract_plan(text)


class TestParseQualityVerdict:
    def test_pass(self):
        assert _parse_quality_verdict('{"verdict": "pass"}') == "pass"

    def test_fail_retryable(self):
        assert _parse_quality_verdict('{"verdict": "fail_retryable"}') == "fail_retryable"

    def test_invalid_json(self):
        assert _parse_quality_verdict("not json") == "fail_escalate"


class TestParseReviewVerdict:
    def test_clean(self):
        assert _parse_review_verdict('{"verdict": "clean"}') == "clean"

    def test_default(self):
        assert _parse_review_verdict("broken") == "issues_found"


class TestExtractPrNumber:
    def test_from_url(self):
        assert _extract_pr_number("Created https://github.com/o/r/pull/42") == 42

    def test_from_hash(self):
        assert _extract_pr_number("PR #99 created") == 99

    def test_none(self):
        assert _extract_pr_number("no number here") is None


class TestSlugifyBranch:
    def test_basic(self):
        result = _slugify_branch(42, "Add user login flow")
        assert result.startswith("42/")
        assert "add-user-login-flow" in result

    def test_custom_pattern(self):
        result = _slugify_branch(7, "Fix bug", pattern="feature/{issue_number}-{description}")
        assert result.startswith("feature/7-")


# -- Pipeline handlers --


class TestHandleNew:
    @pytest.mark.asyncio
    async def test_engineering_repo_routes_to_spec(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"

        with patch.object(pipeline, "_handle_spec", new_callable=AsyncMock) as mock_spec, \
             patch.object(pipeline, "_handle_agent", new_callable=AsyncMock) as mock_agent:
            await pipeline.handle_new(sample_ticket, sample_rules)

        mock_spec.assert_called_once_with(sample_ticket, sample_rules)
        mock_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_repo_routes_to_agent(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "my-service"

        with patch.object(pipeline, "_handle_spec", new_callable=AsyncMock) as mock_spec, \
             patch.object(pipeline, "_handle_agent", new_callable=AsyncMock) as mock_agent:
            await pipeline.handle_new(sample_ticket, sample_rules)

        mock_agent.assert_called_once_with(sample_ticket, sample_rules)
        mock_spec.assert_not_called()


class TestHandleSpec:
    @pytest.mark.asyncio
    async def test_spec_opens_pr_comment(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"
        mock_github.get_comments = AsyncMock(return_value=[])

        with patch.object(pipeline, "_run", return_value=_agent_result("The spec content")):
            await pipeline._handle_spec(sample_ticket, sample_rules)

        mock_github.add_label.assert_any_call("testorg", "engineering", 42, Label.SPEC.value)
        mock_github.add_comment.assert_called()
        mock_github.swap_label.assert_called_with(
            "testorg", "engineering", 42, Label.SPEC, Label.SPEC_REVIEW
        )

    @pytest.mark.asyncio
    async def test_spec_skips_if_pr_comment_exists(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        mock_github.get_comments = AsyncMock(return_value=["Spec PR: already exists"])

        with patch.object(pipeline, "_run") as mock_run:
            await pipeline._handle_spec(sample_ticket, sample_rules)

        mock_run.assert_not_called()


class TestHandleAgent:
    @pytest.mark.asyncio
    async def test_planner_comments_and_transitions(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        plan_file = tmp_path / "PLAN.md"
        plan_file.write_text("## Summary\nDo the thing")

        with patch.object(pipeline, "_run", return_value=_agent_result("[PLAN_COMPLETE]")):
            await pipeline._handle_agent(sample_ticket, sample_rules)

        mock_github.add_label.assert_any_call("testorg", "test-repo", 42, Label.PLANNING.value)
        mock_github.add_comment.assert_called()
        comment_body = mock_github.add_comment.call_args[0][3]
        assert "Development Plan" in comment_body
        mock_github.swap_label.assert_called_with(
            "testorg", "test-repo", 42, Label.PLANNING, Label.PLAN_REVIEW
        )

    @pytest.mark.asyncio
    async def test_planner_needs_info(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)

        with patch.object(pipeline, "_run", return_value=_agent_result("[NEEDS_MORE_INFO]")):
            await pipeline._handle_agent(sample_ticket, sample_rules)

        mock_github.add_comment.assert_called()
        last_comment = mock_github.add_comment.call_args[0][3]
        assert "stopped" in last_comment.lower() or "stuck" in last_comment.lower()


class TestHandlePlanReview:
    @pytest.mark.asyncio
    async def test_pass(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.labels.add(Label.PLAN_REVIEW.value)
        sample_ticket.plan = "the plan"

        with patch.object(pipeline, "_run", return_value=_agent_result('{"verdict": "pass"}')):
            await pipeline._handle_plan_review(sample_ticket, sample_rules)

        mock_github.swap_label.assert_called_with(
            "testorg", "test-repo", 42, Label.PLAN_REVIEW, Label.PLAN
        )

    @pytest.mark.asyncio
    async def test_fail_escalate(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.plan = "the plan"

        with patch.object(
            pipeline, "_run", return_value=_agent_result('{"verdict": "fail_escalate"}')
        ):
            await pipeline._handle_plan_review(sample_ticket, sample_rules)

        mock_github.add_comment.assert_called()


class TestHandlePlan:
    @pytest.mark.asyncio
    async def test_implementer_creates_pr(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.plan = "the plan"

        with patch.object(
            pipeline, "_run",
            return_value=_agent_result("PR created: https://github.com/o/r/pull/7"),
        ):
            await pipeline._handle_plan(sample_ticket, sample_rules)

        assert sample_ticket.pr_number == 7
        mock_github.swap_label.assert_any_call(
            "testorg", "test-repo", 42, Label.IMPLEMENTING, Label.PR
        )

    @pytest.mark.asyncio
    async def test_implementer_blocked(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.plan = "the plan"

        with patch.object(
            pipeline, "_run", return_value=_agent_result("[IMPLEMENTATION_BLOCKED]")
        ):
            await pipeline._handle_plan(sample_ticket, sample_rules)

        mock_github.add_comment.assert_called()


class TestHandlePr:
    @pytest.mark.asyncio
    async def test_ci_passed(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.pr_number = 10
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.PASSED)

        await pipeline._handle_pr(sample_ticket, sample_rules)
        mock_github.swap_label.assert_called_with(
            "testorg", "test-repo", 42, Label.PR, Label.SELF_REVIEW
        )

    @pytest.mark.asyncio
    async def test_ci_failed_runs_fixer(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.pr_number = 10
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.FAILED)
        mock_github.get_ci_failure_logs = AsyncMock(return_value="error log")

        with patch.object(pipeline, "_run", return_value=_agent_result("[CI_FIX_COMPLETE]")):
            await pipeline._handle_pr(sample_ticket, sample_rules)

        mock_github.swap_label.assert_any_call(
            "testorg", "test-repo", 42, Label.CI_FIX, Label.PR
        )

    @pytest.mark.asyncio
    async def test_ci_pending_noop(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.pr_number = 10
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.PENDING)

        await pipeline._handle_pr(sample_ticket, sample_rules)
        mock_github.swap_label.assert_not_called()


class TestHandleReview:
    @pytest.mark.asyncio
    async def test_approved_and_ci_green(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.pr_number = 10
        mock_github.get_review_status = AsyncMock(return_value=ReviewStatus.APPROVED)
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.PASSED)

        await pipeline._handle_review(sample_ticket, sample_rules)
        mock_github.swap_label.assert_called_with(
            "testorg", "test-repo", 42, Label.REVIEW, Label.MERGE_READY
        )


class TestHandleMergeReady:
    @pytest.mark.asyncio
    async def test_notifies_team(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.pr_number = 10

        await pipeline._handle_merge_ready(sample_ticket, sample_rules)
        mock_github.mention_on_issue.assert_called_once()
        call_args = mock_github.mention_on_issue.call_args
        assert "engineering" in call_args[0]


class TestSpecReview:
    @pytest.mark.asyncio
    async def test_merged_pr_transitions_to_approved(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.spec_pr_number = 5
        mock_github.get_pr_state = AsyncMock(return_value="MERGED")

        await pipeline._handle_spec_review(sample_ticket, sample_rules)
        mock_github.swap_label.assert_called_with(
            "testorg", "test-repo", 42, Label.SPEC_REVIEW, Label.SPEC_APPROVED
        )

    @pytest.mark.asyncio
    async def test_closed_pr_triggers_stuck(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.spec_pr_number = 5
        mock_github.get_pr_state = AsyncMock(return_value="CLOSED")

        await pipeline._handle_spec_review(sample_ticket, sample_rules)
        mock_github.add_comment.assert_called()


class TestGetPlan:
    @pytest.mark.asyncio
    async def test_reads_from_file(self, pipeline, mock_github, tmp_path):
        ticket = Ticket(
            owner="testorg", repo="test-repo", issue_number=42,
            labels=set(), work_dir=str(tmp_path),
        )
        (tmp_path / "PLAN.md").write_text("Plan from file")
        result = await pipeline._get_plan(ticket)
        assert result == "Plan from file"

    @pytest.mark.asyncio
    async def test_falls_back_to_comments(self, pipeline, mock_github, tmp_path):
        ticket = Ticket(
            owner="testorg", repo="test-repo", issue_number=42,
            labels=set(), work_dir=str(tmp_path),
        )
        mock_github.get_comments = AsyncMock(return_value=[
            "## Development Plan\n\nPlan from comment",
        ])
        result = await pipeline._get_plan(ticket)
        assert result == "Plan from comment"

    @pytest.mark.asyncio
    async def test_returns_empty_if_none_found(self, pipeline, mock_github, tmp_path):
        ticket = Ticket(
            owner="testorg", repo="test-repo", issue_number=42,
            labels=set(), work_dir=str(tmp_path),
        )
        mock_github.get_comments = AsyncMock(return_value=[])
        result = await pipeline._get_plan(ticket)
        assert result == ""


class TestRetryTracking:
    def test_within_limit(self, pipeline):
        assert pipeline._check_retries("T-1", "plan", 2) is True
        assert pipeline._check_retries("T-1", "plan", 2) is True

    def test_exceeds_limit(self, pipeline):
        pipeline._check_retries("T-1", "ci_fix", 1)
        assert pipeline._check_retries("T-1", "ci_fix", 1) is False

    def test_separate_tickets(self, pipeline):
        pipeline._check_retries("T-1", "plan", 1)
        assert pipeline._check_retries("T-2", "plan", 1) is True
