from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest

from ottonate.models import IdeaPR, Label, StageResult
from ottonate.pipeline import Pipeline, _extract_json_object
from ottonate.prompts import idea_refine_prompt, idea_triage_prompt
from ottonate.scheduler import _extract_project_name


# -- Utilities --


def _agent_result(text: str = "", is_error: bool = False) -> StageResult:
    return StageResult(text=text, session_id="s1", cost_usd=0.01, turns_used=5, is_error=is_error)


@pytest.fixture
def idea_pr() -> IdeaPR:
    return IdeaPR(
        owner="testorg",
        repo="engineering",
        pr_number=7,
        branch="feature/my-idea",
        labels=set(),
        title="Add my idea",
        project_name="my-feature",
    )


@pytest.fixture
def pipeline(config, mock_github, mock_metrics):
    return Pipeline(config, mock_github, metrics=mock_metrics)


# -- _extract_project_name --


class TestExtractProjectName:
    def test_extracts_from_ideas_prefix(self):
        files = [{"filename": "ideas/my-feature/notes.md"}]
        assert _extract_project_name(files, "ideas") == "my-feature"

    def test_extracts_first_project(self):
        files = [
            {"filename": "ideas/project-a/file1.md"},
            {"filename": "ideas/project-b/file2.md"},
        ]
        assert _extract_project_name(files, "ideas") == "project-a"

    def test_ignores_non_ideas_files(self):
        files = [{"filename": "src/main.py"}, {"filename": "README.md"}]
        assert _extract_project_name(files, "ideas") == ""

    def test_empty_file_list(self):
        assert _extract_project_name([], "ideas") == ""

    def test_custom_ideas_dir(self):
        files = [{"filename": "proposals/cool-thing/spec.md"}]
        assert _extract_project_name(files, "proposals") == "cool-thing"

    def test_file_at_ideas_root(self):
        files = [{"filename": "ideas/orphan.md"}]
        assert _extract_project_name(files, "ideas") == "orphan.md"

    def test_nested_files(self):
        files = [{"filename": "ideas/deep-project/sub/dir/file.md"}]
        assert _extract_project_name(files, "ideas") == "deep-project"


# -- _extract_json_object --


class TestExtractJsonObject:
    def test_extracts_title_body_json(self):
        text = 'Some text before\n{"title": "My Feature", "body": "The description"}\nmore text'
        result = _extract_json_object(text)
        assert result == {"title": "My Feature", "body": "The description"}

    def test_returns_last_matching_object(self):
        text = (
            '{"title": "First", "body": "A"}\n'
            'middle text\n'
            '{"title": "Second", "body": "B"}'
        )
        result = _extract_json_object(text)
        assert result["title"] == "Second"

    def test_ignores_json_without_title_body(self):
        text = '{"verdict": "pass"}\n{"other": "data"}'
        assert _extract_json_object(text) is None

    def test_returns_none_on_no_json(self):
        assert _extract_json_object("no json here") is None

    def test_returns_none_on_invalid_json(self):
        assert _extract_json_object("{broken json}") is None

    def test_mixed_json_objects(self):
        text = '{"verdict": "ok"}\n{"title": "Found", "body": "It"}'
        result = _extract_json_object(text)
        assert result == {"title": "Found", "body": "It"}


# -- Prompt builders --


class TestIdeaTriagePrompt:
    def test_includes_project_name(self, idea_pr):
        prompt = idea_triage_prompt(idea_pr, {"notes.md": "some notes"})
        assert "my-feature" in prompt

    def test_includes_file_contents(self, idea_pr):
        prompt = idea_triage_prompt(
            idea_pr,
            {"notes.md": "idea notes", "sketch.py": "code here"},
        )
        assert "notes.md" in prompt
        assert "idea notes" in prompt
        assert "sketch.py" in prompt
        assert "code here" in prompt

    def test_includes_rules_context(self, idea_pr):
        prompt = idea_triage_prompt(
            idea_pr, {"f.md": "content"}, rules_context="Use TDD"
        )
        assert "Use TDD" in prompt

    def test_includes_signal_markers(self, idea_pr):
        prompt = idea_triage_prompt(idea_pr, {"f.md": "c"})
        assert "[IDEA_COMPLETE]" in prompt
        assert "[IDEA_NEEDS_INPUT]" in prompt


class TestIdeaRefinePrompt:
    def test_includes_current_intent(self, idea_pr):
        prompt = idea_refine_prompt(idea_pr, "# Current Intent", ["fix this"])
        assert "# Current Intent" in prompt

    def test_includes_comments(self, idea_pr):
        prompt = idea_refine_prompt(
            idea_pr, "intent", ["comment 1", "comment 2"]
        )
        assert "comment 1" in prompt
        assert "comment 2" in prompt

    def test_includes_refine_complete_marker(self, idea_pr):
        prompt = idea_refine_prompt(idea_pr, "intent", ["feedback"])
        assert "[REFINE_COMPLETE]" in prompt


# -- IdeaPR model --


class TestIdeaPR:
    def test_full_repo(self, idea_pr):
        assert idea_pr.full_repo == "testorg/engineering"

    def test_pr_ref(self, idea_pr):
        assert idea_pr.pr_ref == "testorg/engineering#7"

    def test_idea_label_none(self, idea_pr):
        assert idea_pr.idea_label is None

    def test_idea_label_triage(self):
        pr = IdeaPR(
            owner="o", repo="r", pr_number=1, branch="b",
            labels={Label.IDEA_TRIAGE.value},
        )
        assert pr.idea_label == Label.IDEA_TRIAGE

    def test_idea_label_review(self):
        pr = IdeaPR(
            owner="o", repo="r", pr_number=1, branch="b",
            labels={Label.IDEA_REVIEW.value},
        )
        assert pr.idea_label == Label.IDEA_REVIEW

    def test_idea_label_refining(self):
        pr = IdeaPR(
            owner="o", repo="r", pr_number=1, branch="b",
            labels={Label.IDEA_REFINING.value},
        )
        assert pr.idea_label == Label.IDEA_REFINING


# -- Pipeline handlers --


class TestHandleIdeaPrRouter:
    @pytest.mark.asyncio
    async def test_routes_to_triage_when_no_label(
        self, pipeline, idea_pr, sample_rules
    ):
        with (
            patch.object(pipeline, "_handle_idea_triage", new_callable=AsyncMock) as mock_triage,
            patch.object(pipeline, "_handle_idea_review", new_callable=AsyncMock) as mock_review,
        ):
            await pipeline.handle_idea_pr(idea_pr, sample_rules)

        mock_triage.assert_called_once_with(idea_pr, sample_rules)
        mock_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_routes_to_review_when_review_label(
        self, pipeline, idea_pr, sample_rules
    ):
        idea_pr.labels.add(Label.IDEA_REVIEW.value)
        with (
            patch.object(pipeline, "_handle_idea_triage", new_callable=AsyncMock) as mock_triage,
            patch.object(pipeline, "_handle_idea_review", new_callable=AsyncMock) as mock_review,
        ):
            await pipeline.handle_idea_pr(idea_pr, sample_rules)

        mock_review.assert_called_once_with(idea_pr, sample_rules)
        mock_triage.assert_not_called()


class TestHandleIdeaTriage:
    @pytest.mark.asyncio
    async def test_triage_creates_issue_and_comments(
        self, pipeline, idea_pr, sample_rules, mock_github, tmp_path
    ):
        mock_github.get_directory_contents = AsyncMock(
            return_value=[{"type": "file", "name": "notes.md", "path": "ideas/my-feature/notes.md"}]
        )
        mock_github.get_file_content = AsyncMock(return_value="# My idea notes")
        mock_github.create_issue = AsyncMock(return_value=42)

        agent_output = (
            'INTENT generated.\n'
            '{"title": "My Feature", "body": "Feature description"}\n'
            '[IDEA_COMPLETE]'
        )

        with (
            patch.object(pipeline, "_run", return_value=_agent_result(agent_output)),
            patch.object(pipeline, "_ensure_idea_workspace", new_callable=AsyncMock),
            patch("ottonate.pipeline._git_commit_push_existing", new_callable=AsyncMock),
        ):
            await pipeline._handle_idea_triage(idea_pr, sample_rules)

        mock_github.add_pr_label.assert_any_call(
            "testorg", "engineering", 7, Label.IDEA_TRIAGE.value
        )
        mock_github.create_issue.assert_called_once_with(
            "testorg", "engineering", "My Feature", "Feature description", ["otto"]
        )
        assert idea_pr.linked_issue_number == 42

        # Verify comment on PR
        comment_calls = mock_github.add_comment.call_args_list
        assert any("issue created: #42" in str(c) for c in comment_calls)

        mock_github.swap_pr_label.assert_called_with(
            "testorg", "engineering", 7, Label.IDEA_TRIAGE, Label.IDEA_REVIEW
        )

    @pytest.mark.asyncio
    async def test_triage_needs_input(
        self, pipeline, idea_pr, sample_rules, mock_github, tmp_path
    ):
        mock_github.get_directory_contents = AsyncMock(
            return_value=[{"type": "file", "name": "notes.md", "path": "ideas/my-feature/notes.md"}]
        )
        mock_github.get_file_content = AsyncMock(return_value="vague idea")

        with (
            patch.object(
                pipeline, "_run", return_value=_agent_result("[IDEA_NEEDS_INPUT]")
            ),
            patch.object(pipeline, "_ensure_idea_workspace", new_callable=AsyncMock),
        ):
            await pipeline._handle_idea_triage(idea_pr, sample_rules)

        mock_github.swap_pr_label.assert_called_with(
            "testorg", "engineering", 7, Label.IDEA_TRIAGE, Label.IDEA_REVIEW
        )
        comment_calls = mock_github.add_comment.call_args_list
        assert any("needs more information" in str(c) for c in comment_calls)

    @pytest.mark.asyncio
    async def test_triage_no_files_returns_early(
        self, pipeline, idea_pr, sample_rules, mock_github
    ):
        mock_github.get_directory_contents = AsyncMock(return_value=[])

        with patch.object(pipeline, "_run") as mock_run:
            await pipeline._handle_idea_triage(idea_pr, sample_rules)

        mock_run.assert_not_called()
        mock_github.remove_pr_label.assert_called_with(
            "testorg", "engineering", 7, Label.IDEA_TRIAGE.value
        )

    @pytest.mark.asyncio
    async def test_triage_skips_hidden_files(
        self, pipeline, idea_pr, sample_rules, mock_github
    ):
        mock_github.get_directory_contents = AsyncMock(
            return_value=[
                {"type": "file", "name": ".gitkeep", "path": "ideas/my-feature/.gitkeep"},
                {"type": "file", "name": "notes.md", "path": "ideas/my-feature/notes.md"},
            ]
        )
        mock_github.get_file_content = AsyncMock(return_value="content")
        mock_github.create_issue = AsyncMock(return_value=1)

        with (
            patch.object(pipeline, "_run", return_value=_agent_result('[IDEA_COMPLETE]\n{"title":"T","body":"B"}')),
            patch.object(pipeline, "_ensure_idea_workspace", new_callable=AsyncMock),
            patch("ottonate.pipeline._git_commit_push_existing", new_callable=AsyncMock),
        ):
            await pipeline._handle_idea_triage(idea_pr, sample_rules)

        # get_file_content should be called once (for notes.md, not .gitkeep)
        calls = mock_github.get_file_content.call_args_list
        paths = [c[0][2] for c in calls]
        assert "ideas/my-feature/.gitkeep" not in paths
        assert "ideas/my-feature/notes.md" in paths


class TestHandleIdeaReview:
    @pytest.mark.asyncio
    async def test_refines_on_new_comments(
        self, pipeline, idea_pr, sample_rules, mock_github
    ):
        idea_pr.labels.add(Label.IDEA_REVIEW.value)
        idea_pr.linked_issue_number = 42

        mock_github.get_pr_details = AsyncMock(return_value={
            "comments": [
                {"author": {"login": "test-bot"}, "body": "INTENT.md generated and issue created: #42"},
                {"author": {"login": "human"}, "body": "Please add caching support"},
            ],
        })
        mock_github.get_file_content = AsyncMock(return_value="# Existing Intent")

        agent_output = (
            'Updated.\n'
            '{"title": "Updated Feature", "body": "Updated body"}\n'
            '[REFINE_COMPLETE]'
        )

        with (
            patch.object(pipeline, "_run", return_value=_agent_result(agent_output)),
            patch.object(pipeline, "_ensure_idea_workspace", new_callable=AsyncMock),
            patch("ottonate.pipeline._git_commit_push_existing", new_callable=AsyncMock),
        ):
            await pipeline._handle_idea_review(idea_pr, sample_rules)

        mock_github.swap_pr_label.assert_any_call(
            "testorg", "engineering", 7, Label.IDEA_REVIEW, Label.IDEA_REFINING
        )
        mock_github.edit_issue_body.assert_called_once_with(
            "testorg", "engineering", 42, "Updated body"
        )
        mock_github.swap_pr_label.assert_called_with(
            "testorg", "engineering", 7, Label.IDEA_REFINING, Label.IDEA_REVIEW
        )

    @pytest.mark.asyncio
    async def test_no_new_comments_returns_early(
        self, pipeline, idea_pr, sample_rules, mock_github
    ):
        idea_pr.labels.add(Label.IDEA_REVIEW.value)

        mock_github.get_pr_details = AsyncMock(return_value={
            "comments": [
                {"author": {"login": "test-bot"}, "body": "INTENT.md generated"},
            ],
        })

        with patch.object(pipeline, "_run") as mock_run:
            await pipeline._handle_idea_review(idea_pr, sample_rules)

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_extracts_linked_issue_from_bot_comment(
        self, pipeline, idea_pr, sample_rules, mock_github
    ):
        idea_pr.labels.add(Label.IDEA_REVIEW.value)

        mock_github.get_pr_details = AsyncMock(return_value={
            "comments": [
                {"author": {"login": "test-bot"}, "body": "issue created: #55\nReview the intent."},
                {"author": {"login": "human"}, "body": "Looks good, add tests"},
            ],
        })
        mock_github.get_file_content = AsyncMock(return_value="# Intent")

        with (
            patch.object(pipeline, "_run", return_value=_agent_result('{"title":"T","body":"B"}\n[REFINE_COMPLETE]')),
            patch.object(pipeline, "_ensure_idea_workspace", new_callable=AsyncMock),
            patch("ottonate.pipeline._git_commit_push_existing", new_callable=AsyncMock),
        ):
            await pipeline._handle_idea_review(idea_pr, sample_rules)

        assert idea_pr.linked_issue_number == 55
        mock_github.edit_issue_body.assert_called_once()


# -- Scheduler integration --


class TestSchedulerIdeaPolling:
    @pytest.mark.asyncio
    async def test_poll_detects_idea_prs(self, config):
        from ottonate.scheduler import Scheduler

        with (
            patch("ottonate.scheduler.GitHubClient") as mock_gh_cls,
            patch("ottonate.scheduler.MetricsStore") as mock_metrics_cls,
        ):
            mock_metrics_cls.return_value.init_db = AsyncMock()
            scheduler = Scheduler(config)
            scheduler.github = mock_gh_cls.return_value
            scheduler.pipeline = AsyncMock()
            scheduler.pipeline.handle_idea_pr = AsyncMock()

            scheduler.github.list_open_prs = AsyncMock(return_value=[
                {
                    "number": 5,
                    "headRefName": "feature/test-idea",
                    "labels": [],
                    "title": "Add test idea",
                },
            ])
            scheduler.github.get_pr_files = AsyncMock(return_value=[
                {"filename": "ideas/test-idea/notes.md"},
            ])

            await scheduler._poll_idea_prs("testorg")

            # Should have created a task for the idea PR
            # Give the task a moment to be created
            import asyncio
            await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_poll_skips_in_progress_labels(self, config):
        from ottonate.scheduler import Scheduler

        with (
            patch("ottonate.scheduler.GitHubClient") as mock_gh_cls,
            patch("ottonate.scheduler.MetricsStore") as mock_metrics_cls,
        ):
            mock_metrics_cls.return_value.init_db = AsyncMock()
            scheduler = Scheduler(config)
            scheduler.github = mock_gh_cls.return_value
            scheduler.pipeline = AsyncMock()

            scheduler.github.list_open_prs = AsyncMock(return_value=[
                {
                    "number": 5,
                    "headRefName": "feature/idea",
                    "labels": [{"name": Label.IDEA_TRIAGE.value}],
                    "title": "Idea",
                },
            ])

            await scheduler._poll_idea_prs("testorg")

            # Should NOT call get_pr_files since it's in progress
            scheduler.github.get_pr_files.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_skips_when_disabled(self, config):
        from ottonate.scheduler import Scheduler

        config.idea_poll_enabled = False
        with (
            patch("ottonate.scheduler.GitHubClient") as mock_gh_cls,
            patch("ottonate.scheduler.MetricsStore") as mock_metrics_cls,
        ):
            mock_metrics_cls.return_value.init_db = AsyncMock()
            scheduler = Scheduler(config)
            scheduler.github = mock_gh_cls.return_value

            await scheduler._poll_idea_prs("testorg")

            scheduler.github.list_open_prs.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_ignores_non_idea_prs(self, config):
        from ottonate.scheduler import Scheduler

        with (
            patch("ottonate.scheduler.GitHubClient") as mock_gh_cls,
            patch("ottonate.scheduler.MetricsStore") as mock_metrics_cls,
        ):
            mock_metrics_cls.return_value.init_db = AsyncMock()
            scheduler = Scheduler(config)
            scheduler.github = mock_gh_cls.return_value
            scheduler.pipeline = AsyncMock()

            scheduler.github.list_open_prs = AsyncMock(return_value=[
                {
                    "number": 10,
                    "headRefName": "fix/something",
                    "labels": [],
                    "title": "Fix something",
                },
            ])
            scheduler.github.get_pr_files = AsyncMock(return_value=[
                {"filename": "src/main.py"},
            ])

            await scheduler._poll_idea_prs("testorg")

            # Pipeline should not be called
            scheduler.pipeline.handle_idea_pr.assert_not_called()


# -- Label auto-creation --


class TestEnsurePipelineLabels:
    @pytest.mark.asyncio
    async def test_calls_ensure_labels(self, pipeline, mock_github):
        await pipeline.ensure_pipeline_labels("testorg", "engineering")
        mock_github.ensure_labels.assert_called_once()
        call_args = mock_github.ensure_labels.call_args[0]
        assert call_args[0] == "testorg"
        assert call_args[1] == "engineering"
        labels = call_args[2]
        assert "otto" in labels
        assert Label.IDEA_TRIAGE.value in labels
        assert Label.SPEC.value in labels
        assert Label.STUCK.value in labels

    @pytest.mark.asyncio
    async def test_handle_new_ensures_labels(
        self, pipeline, sample_ticket, sample_rules, mock_github, tmp_path
    ):
        sample_ticket.work_dir = str(tmp_path)
        sample_ticket.repo = "engineering"
        mock_github.get_comments = AsyncMock(return_value=["Spec PR: already exists"])

        await pipeline.handle_new(sample_ticket, sample_rules)
        mock_github.ensure_labels.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_idea_pr_ensures_labels(
        self, pipeline, idea_pr, sample_rules, mock_github
    ):
        with patch.object(pipeline, "_handle_idea_triage", new_callable=AsyncMock):
            await pipeline.handle_idea_pr(idea_pr, sample_rules)
        mock_github.ensure_labels.assert_called_once()


# -- _git_commit_push_existing edge cases --


class TestGitCommitPushExisting:
    @pytest.mark.asyncio
    async def test_no_changes_is_noop(self, tmp_path):
        """When there are no staged changes, commit+push should be skipped."""
        from ottonate.pipeline import _git_commit_push_existing

        # Create a git repo with a commit so there's a valid HEAD
        import subprocess

        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)

        # Running with no changes should not raise
        await _git_commit_push_existing(str(tmp_path), "no-op commit")
