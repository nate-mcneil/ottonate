from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ottonate.integrations.github import GitHubClient
from ottonate.models import CIStatus, ReviewStatus


@pytest.fixture
def github():
    return GitHubClient()


def _gh_result(stdout: str, returncode: int = 0):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    return proc


class TestSearchIssues:
    @pytest.mark.asyncio
    async def test_returns_issues(self, github):
        issues = [
            {"repository": {"name": "my-app"}, "number": 1, "labels": [], "title": "Test"},
        ]
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(issues))):
            result = await github.search_issues("org", "otto")
        assert len(result) == 1
        assert result[0]["number"] == 1

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            result = await github.search_issues("org", "otto")
        assert result == []


class TestGetIssue:
    @pytest.mark.asyncio
    async def test_returns_issue(self, github):
        issue = {
            "number": 42,
            "title": "Test",
            "body": "Description",
            "labels": [],
            "state": "OPEN",
        }
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(issue))):
            result = await github.get_issue("org", "repo", 42)
        assert result["title"] == "Test"


class TestCreateIssue:
    @pytest.mark.asyncio
    async def test_parses_number_from_url(self, github):
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_gh_result("https://github.com/org/repo/issues/99"),
        ):
            number = await github.create_issue("org", "repo", "Title", "Body", ["otto"])
        assert number == 99


class TestSwapLabel:
    @pytest.mark.asyncio
    async def test_calls_gh(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("")) as mock_exec:
            from ottonate.models import Label

            await github.swap_label("org", "repo", 42, Label.PLANNING, Label.PLAN_REVIEW)
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert "--remove-label" in args
        assert "--add-label" in args


class TestFindPr:
    @pytest.mark.asyncio
    async def test_finds_by_branch(self, github):
        prs = [{"number": 42, "headRefName": "42/feature", "state": "OPEN"}]
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(prs))):
            number, state = await github.find_pr("owner", "repo", "42")
        assert number == 42
        assert state == "OPEN"

    @pytest.mark.asyncio
    async def test_returns_none(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("")):
            number, state = await github.find_pr("owner", "repo", "42")
        assert number is None


class TestGetCiStatus:
    @pytest.mark.asyncio
    async def test_all_passed(self, github):
        checks = [{"name": "build", "state": "COMPLETED", "conclusion": "SUCCESS"}]
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(checks))):
            assert await github.get_ci_status("o", "r", 1) == CIStatus.PASSED

    @pytest.mark.asyncio
    async def test_failure(self, github):
        checks = [{"name": "build", "state": "COMPLETED", "conclusion": "FAILURE"}]
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(checks))):
            assert await github.get_ci_status("o", "r", 1) == CIStatus.FAILED

    @pytest.mark.asyncio
    async def test_pending(self, github):
        checks = [{"name": "build", "state": "PENDING", "conclusion": None}]
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(checks))):
            assert await github.get_ci_status("o", "r", 1) == CIStatus.PENDING

    @pytest.mark.asyncio
    async def test_no_pr(self, github):
        assert await github.get_ci_status("o", "r", None) == CIStatus.PENDING


class TestGetCiFailureLogs:
    @pytest.mark.asyncio
    async def test_extracts_run_id_from_details_url(self, github):
        checks = [
            {
                "name": "build",
                "state": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/o/r/actions/runs/12345/job/678",
            }
        ]
        call_count = 0

        async def _mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _gh_result(json.dumps(checks))
            return _gh_result("Error: tests failed\nAssertionError")

        with patch("asyncio.create_subprocess_exec", side_effect=_mock_exec):
            logs = await github.get_ci_failure_logs("o", "r", 1)

        assert "Failed check: build" in logs
        assert "AssertionError" in logs


class TestGetReviewStatus:
    @pytest.mark.asyncio
    async def test_approved(self, github):
        data = {"reviews": [{"author": {"login": "reviewer"}, "state": "APPROVED"}]}
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(data))):
            assert await github.get_review_status("o", "r", 1) == ReviewStatus.APPROVED

    @pytest.mark.asyncio
    async def test_changes_requested(self, github):
        data = {"reviews": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}]}
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(data))):
            assert await github.get_review_status("o", "r", 1) == ReviewStatus.CHANGES_REQUESTED

    @pytest.mark.asyncio
    async def test_no_pr(self, github):
        assert await github.get_review_status("o", "r", None) == ReviewStatus.PENDING


class TestGetFileContent:
    @pytest.mark.asyncio
    async def test_decodes_base64(self, github):
        import base64

        content = base64.b64encode(b"hello world").decode()
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(content)):
            result = await github.get_file_content("o", "r", "README.md")
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_returns_none_on_missing(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            result = await github.get_file_content("o", "r", "missing.md")
        assert result is None


class TestGetPrState:
    @pytest.mark.asyncio
    async def test_merged(self, github):
        data = {"state": "MERGED"}
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(data))):
            assert await github.get_pr_state("o", "r", 1) == "MERGED"

    @pytest.mark.asyncio
    async def test_open(self, github):
        data = {"state": "OPEN"}
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result(json.dumps(data))):
            assert await github.get_pr_state("o", "r", 1) == "OPEN"
