from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ottonate.models import CIStatus, Label, ReviewStatus, StageResult, Ticket
from ottonate.pipeline import (
    Pipeline,
    _extract_plan,
    _extract_pr_number,
    _parse_quality_verdict,
    _parse_review_verdict,
    _parse_self_improvement,
    _slugify_branch,
)


@pytest.fixture
def pipeline(config, mock_github, mock_metrics):
    return Pipeline(config, mock_github, metrics=mock_metrics)


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

        with (
            patch.object(pipeline, "_handle_spec", new_callable=AsyncMock) as mock_spec,
            patch.object(pipeline, "_handle_agent", new_callable=AsyncMock) as mock_agent,
        ):
            await pipeline.handle_new(sample_ticket, sample_rules)

        mock_spec.assert_called_once_with(sample_ticket, sample_rules)
        mock_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_repo_routes_to_agent(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "my-service"

        with (
            patch.object(pipeline, "_handle_spec", new_callable=AsyncMock) as mock_spec,
            patch.object(pipeline, "_handle_agent", new_callable=AsyncMock) as mock_agent,
        ):
            await pipeline.handle_new(sample_ticket, sample_rules)

        mock_agent.assert_called_once_with(sample_ticket, sample_rules)
        mock_spec.assert_not_called()


class TestHandleSpec:
    @pytest.mark.asyncio
    async def test_spec_creates_pr_and_posts_number(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"
        mock_github.get_comments = AsyncMock(return_value=[])
        mock_github.create_pr = AsyncMock(return_value=10)

        spec_file = tmp_path / "SPEC.md"

        def write_spec(*args, **kwargs):
            spec_file.write_text("# The Spec")
            return _agent_result("[SPEC_COMPLETE]")

        with (
            patch.object(pipeline, "_run", side_effect=write_spec),
            patch("ottonate.pipeline._git_branch_commit_push", new_callable=AsyncMock) as mock_git,
        ):
            await pipeline._handle_spec(sample_ticket, sample_rules)

        mock_github.add_label.assert_any_call("testorg", "engineering", 42, Label.SPEC.value)
        mock_git.assert_called_once()
        mock_github.create_pr.assert_called_once()
        comment_body = mock_github.add_comment.call_args[0][3]
        assert "Spec PR: #10" in comment_body
        mock_github.swap_label.assert_called_with(
            "testorg", "engineering", 42, Label.SPEC, Label.SPEC_REVIEW
        )

    @pytest.mark.asyncio
    async def test_spec_moves_file_to_specs_dir(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"
        mock_github.get_comments = AsyncMock(return_value=[])
        mock_github.create_pr = AsyncMock(return_value=10)

        spec_file = tmp_path / "SPEC.md"

        def write_spec(*args, **kwargs):
            spec_file.write_text("# The Spec")
            return _agent_result("[SPEC_COMPLETE]")

        with (
            patch.object(pipeline, "_run", side_effect=write_spec),
            patch("ottonate.pipeline._git_branch_commit_push", new_callable=AsyncMock),
        ):
            await pipeline._handle_spec(sample_ticket, sample_rules)

        expected_dir = tmp_path / "specs" / "42"
        assert (expected_dir / "SPEC.md").exists()
        assert (expected_dir / "SPEC.md").read_text() == "# The Spec"
        assert not spec_file.exists()

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
    async def test_implementer_creates_pr(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.plan = "the plan"

        with patch.object(
            pipeline,
            "_run",
            return_value=_agent_result("PR created: https://github.com/o/r/pull/7"),
        ):
            await pipeline._handle_plan(sample_ticket, sample_rules)

        assert sample_ticket.pr_number == 7
        mock_github.swap_label.assert_any_call(
            "testorg", "test-repo", 42, Label.IMPLEMENTING, Label.PR
        )

    @pytest.mark.asyncio
    async def test_implementer_blocked(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.plan = "the plan"

        with patch.object(pipeline, "_run", return_value=_agent_result("[IMPLEMENTATION_BLOCKED]")):
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
    async def test_ci_failed_runs_fixer(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.pr_number = 10
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.FAILED)
        mock_github.get_ci_failure_logs = AsyncMock(return_value="error log")

        with patch.object(pipeline, "_run", return_value=_agent_result("[CI_FIX_COMPLETE]")):
            await pipeline._handle_pr(sample_ticket, sample_rules)

        mock_github.swap_label.assert_any_call("testorg", "test-repo", 42, Label.CI_FIX, Label.PR)

    @pytest.mark.asyncio
    async def test_ci_pending_noop(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.pr_number = 10
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.PENDING)

        await pipeline._handle_pr(sample_ticket, sample_rules)
        mock_github.swap_label.assert_not_called()


class TestHandleReview:
    @pytest.mark.asyncio
    async def test_approved_and_ci_green(self, pipeline, sample_ticket, sample_rules, mock_github):
        sample_ticket.pr_number = 10
        mock_github.get_review_status = AsyncMock(return_value=ReviewStatus.APPROVED)
        mock_github.get_ci_status = AsyncMock(return_value=CIStatus.PASSED)

        await pipeline._handle_review(sample_ticket, sample_rules)
        mock_github.swap_label.assert_called_with(
            "testorg", "test-repo", 42, Label.REVIEW, Label.MERGE_READY
        )


class TestHandleMergeReady:
    @pytest.mark.asyncio
    async def test_notifies_when_not_merged(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.pr_number = 10
        mock_github.get_pr_state = AsyncMock(return_value="OPEN")
        mock_github.get_comments = AsyncMock(return_value=[])

        await pipeline._handle_merge_ready(sample_ticket, sample_rules)
        mock_github.mention_on_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_notification_if_already_notified(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.pr_number = 10
        mock_github.get_pr_state = AsyncMock(return_value="OPEN")
        mock_github.get_comments = AsyncMock(return_value=["PR is merge-ready, waiting"])

        await pipeline._handle_merge_ready(sample_ticket, sample_rules)
        mock_github.mention_on_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_merged_clean_removes_labels(
        self, pipeline, sample_ticket, sample_rules, mock_github, mock_metrics
    ):
        sample_ticket.pr_number = 10
        mock_github.get_pr_state = AsyncMock(return_value="MERGED")

        await pipeline._handle_merge_ready(sample_ticket, sample_rules)
        mock_github.remove_label.assert_any_call(
            "testorg", "test-repo", 42, Label.MERGE_READY.value
        )

    @pytest.mark.asyncio
    async def test_merged_with_retries_triggers_retro(
        self, pipeline, sample_ticket, sample_rules, mock_github, mock_metrics
    ):
        sample_ticket.pr_number = 10
        mock_github.get_pr_state = AsyncMock(return_value="MERGED")

        await mock_metrics.record_stage(
            sample_ticket.issue_ref,
            "planning",
            "otto-planner",
            0.05,
            10,
            False,
            0,
        )
        await mock_metrics.record_stage(
            sample_ticket.issue_ref,
            "planning",
            "otto-planner",
            0.03,
            8,
            False,
            1,
        )

        await pipeline._handle_merge_ready(sample_ticket, sample_rules)
        mock_github.swap_label.assert_called_with(
            "testorg",
            "test-repo",
            42,
            Label.MERGE_READY,
            Label.RETRO,
        )

    @pytest.mark.asyncio
    async def test_merged_with_stuck_triggers_retro(
        self, pipeline, sample_ticket, sample_rules, mock_github, mock_metrics
    ):
        sample_ticket.pr_number = 10
        mock_github.get_pr_state = AsyncMock(return_value="MERGED")

        await mock_metrics.record_stage(
            sample_ticket.issue_ref,
            "implementing",
            "otto-implementer",
            1.0,
            50,
            True,
            0,
            was_stuck=True,
            stuck_reason="CI blocked",
        )

        await pipeline._handle_merge_ready(sample_ticket, sample_rules)
        mock_github.swap_label.assert_called_with(
            "testorg",
            "test-repo",
            42,
            Label.MERGE_READY,
            Label.RETRO,
        )


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


class TestHandleSpecApproved:
    @pytest.mark.asyncio
    async def test_missing_spec_in_repo_triggers_stuck(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"
        mock_github.get_comments = AsyncMock(return_value=[])
        mock_github.get_file_content = AsyncMock(return_value=None)

        (tmp_path / "SPEC.md").write_text("local fallback should be ignored")

        with patch.object(pipeline, "_run") as mock_run:
            await pipeline._handle_spec_approved(sample_ticket, sample_rules)
            mock_run.assert_not_called()

        mock_github.add_comment.assert_called()
        stuck_comment = mock_github.add_comment.call_args[0][3]
        assert "spec" in stuck_comment.lower()

    @pytest.mark.asyncio
    async def test_spec_found_in_repo_runs_backlog(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"
        mock_github.get_comments = AsyncMock(return_value=[])
        mock_github.get_file_content = AsyncMock(return_value="# Spec Content")

        with patch.object(pipeline, "_run", return_value=_agent_result("[BACKLOG_COMPLETE]")):
            await pipeline._handle_spec_approved(sample_ticket, sample_rules)

        mock_github.swap_label.assert_any_call(
            "testorg", "engineering", 42, Label.BACKLOG_GEN, Label.BACKLOG_REVIEW
        )


class TestCreateStoriesFromBacklog:
    @pytest.mark.asyncio
    async def test_creates_project_and_adds_stories(
        self, pipeline, sample_ticket, sample_rules, mock_github
    ):
        sample_ticket.repo = "engineering"
        backlog_json = (
            "## Generated Backlog\n\n```json\n"
            '[{"title": "Story A", "repo": "target-repo", "description": "Do A", '
            '"estimate": "S", "dependencies": [], "notes": ""}]\n```'
        )
        mock_github.get_comments = AsyncMock(return_value=[backlog_json])
        mock_github.create_issue = AsyncMock(return_value=99)
        mock_github.create_project = AsyncMock(return_value="7")
        mock_github.add_to_project = AsyncMock()

        with patch.object(pipeline, "_enrich_story", return_value=None):
            refs = await pipeline._create_stories_from_backlog(sample_ticket, sample_rules)

        assert len(refs) == 1
        mock_github.create_project.assert_called_once_with("testorg", "Test issue")
        assert sample_ticket.project_id == "7"
        add_calls = mock_github.add_to_project.call_args_list
        assert len(add_calls) == 2
        assert add_calls[0].args == (
            "testorg",
            "7",
            "https://github.com/testorg/engineering/issues/42",
        )
        assert add_calls[1].args == (
            "testorg",
            "7",
            "https://github.com/testorg/target-repo/issues/99",
        )


class TestGetPlan:
    @pytest.mark.asyncio
    async def test_reads_from_file(self, pipeline, mock_github, tmp_path):
        ticket = Ticket(
            owner="testorg",
            repo="test-repo",
            issue_number=42,
            labels=set(),
            work_dir=str(tmp_path),
        )
        (tmp_path / "PLAN.md").write_text("Plan from file")
        result = await pipeline._get_plan(ticket)
        assert result == "Plan from file"

    @pytest.mark.asyncio
    async def test_falls_back_to_comments(self, pipeline, mock_github, tmp_path):
        ticket = Ticket(
            owner="testorg",
            repo="test-repo",
            issue_number=42,
            labels=set(),
            work_dir=str(tmp_path),
        )
        mock_github.get_comments = AsyncMock(
            return_value=[
                "## Development Plan\n\nPlan from comment",
            ]
        )
        result = await pipeline._get_plan(ticket)
        assert result == "Plan from comment"

    @pytest.mark.asyncio
    async def test_returns_empty_if_none_found(self, pipeline, mock_github, tmp_path):
        ticket = Ticket(
            owner="testorg",
            repo="test-repo",
            issue_number=42,
            labels=set(),
            work_dir=str(tmp_path),
        )
        mock_github.get_comments = AsyncMock(return_value=[])
        result = await pipeline._get_plan(ticket)
        assert result == ""


class TestHandleRetro:
    @pytest.mark.asyncio
    async def test_runs_retro_agent(
        self, pipeline, sample_ticket, sample_rules, mock_github, mock_metrics, tmp_path
    ):
        sample_ticket.pr_number = 10
        sample_ticket.work_dir = str(tmp_path)
        mock_github.get_comments = AsyncMock(return_value=[])

        await mock_metrics.record_stage(
            sample_ticket.issue_ref,
            "planning",
            "otto-planner",
            0.05,
            10,
            False,
            1,
        )

        with (
            patch.object(pipeline, "_run", return_value=_agent_result("[RETRO_COMPLETE]")),
            patch.object(pipeline, "_ensure_eng_workspace", new_callable=AsyncMock),
        ):
            pipeline._eng_workspace_path = lambda: tmp_path
            await pipeline._handle_retro(sample_ticket, sample_rules)

        mock_github.remove_label.assert_any_call(
            "testorg",
            "test-repo",
            42,
            Label.RETRO.value,
        )
        mock_github.add_comment.assert_called()
        comment_body = mock_github.add_comment.call_args[0][3]
        assert "Retro complete" in comment_body

    @pytest.mark.asyncio
    async def test_retro_files_self_improvement_issue(
        self, pipeline, sample_ticket, sample_rules, mock_github, mock_metrics, tmp_path
    ):
        sample_ticket.pr_number = 10
        sample_ticket.work_dir = str(tmp_path)
        mock_github.get_comments = AsyncMock(return_value=[])
        mock_github.create_issue = AsyncMock(return_value=99)

        retro_text = (
            "[RETRO_COMPLETE]\n"
            "[SELF_IMPROVEMENT]\n"
            '{"title": "Improve CI fixer prompt", "body": "The CI fixer should check lockfiles."}'
        )

        with (
            patch.object(pipeline, "_run", return_value=_agent_result(retro_text)),
            patch.object(pipeline, "_ensure_eng_workspace", new_callable=AsyncMock),
        ):
            pipeline._eng_workspace_path = lambda: tmp_path
            await pipeline._handle_retro(sample_ticket, sample_rules)

        mock_github.create_issue.assert_called_once()
        call_args = mock_github.create_issue.call_args[0]
        assert call_args[1] == "ottonate"
        assert "CI fixer" in call_args[2]


class TestParseSelfImprovement:
    def test_parses_json(self):
        text = 'stuff [SELF_IMPROVEMENT]\n{"title": "Fix X", "body": "Do Y"}'
        result = _parse_self_improvement(text)
        assert result == {"title": "Fix X", "body": "Do Y"}

    def test_returns_none_when_no_marker(self):
        assert _parse_self_improvement("no marker here") is None

    def test_returns_none_on_bad_json(self):
        assert _parse_self_improvement("[SELF_IMPROVEMENT]\nnot json") is None


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
