"""Tests for interactive onboarding setup utilities."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

from ottonate.github import GitHubClient
from ottonate.setup import (
    PIPELINE_LABEL_COLORS,
    SetupResult,
    create_repo,
    detect_gh_user,
    ensure_labels,
    init_empty_repo,
    list_user_orgs,
    repo_exists,
    repo_is_empty,
    write_env_file,
)


def _gh_result(stdout: str, returncode: int = 0):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    return proc


@pytest.fixture
def github():
    return GitHubClient()


class TestDetectGhUser:
    @pytest.mark.asyncio
    async def test_success_returns_username(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("nate-mcneil\n")):
            result = await detect_gh_user(github)
        assert result == "nate-mcneil"

    @pytest.mark.asyncio
    async def test_failure_returns_empty_string(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            result = await detect_gh_user(github)
        assert result == ""


class TestListUserOrgs:
    @pytest.mark.asyncio
    async def test_returns_parsed_org_list(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("appfire\nother-org\n")):
            result = await list_user_orgs(github)
        assert result == ["appfire", "other-org"]

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            result = await list_user_orgs(github)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_empty_stdout(self, github):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("")):
            result = await list_user_orgs(github)
        assert result == []


class TestRepoExists:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result('{"name":"eng"}')):
            assert await repo_exists("org", "eng") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            assert await repo_exists("org", "eng") is False


class TestRepoIsEmpty:
    @pytest.mark.asyncio
    async def test_returns_true_on_nonzero_returncode(self):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            assert await repo_is_empty("org", "eng") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_success(self):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result('[{"name":"README.md"}]')):
            assert await repo_is_empty("org", "eng") is False


class TestCreateRepo:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("")):
            assert await create_repo("org", "eng") is True

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        with patch("asyncio.create_subprocess_exec", return_value=_gh_result("", returncode=1)):
            assert await create_repo("org", "eng") is False


class TestEnsureLabels:
    @pytest.mark.asyncio
    async def test_calls_github_with_combined_labels(self):
        gh = AsyncMock(spec=GitHubClient)
        gh.ensure_labels = AsyncMock(return_value=["agentIdeaTriage", "otto"])
        count = await ensure_labels(gh, "org", "eng", "otto")
        assert count == 2
        gh.ensure_labels.assert_called_once()
        labels_arg = gh.ensure_labels.call_args[0][2]
        assert "otto" in labels_arg
        assert "agentIdeaTriage" in labels_arg
        assert len(labels_arg) == len(PIPELINE_LABEL_COLORS) + 1

    @pytest.mark.asyncio
    async def test_returns_zero_when_all_exist(self):
        gh = AsyncMock(spec=GitHubClient)
        gh.ensure_labels = AsyncMock(return_value=[])
        count = await ensure_labels(gh, "org", "eng", "otto")
        assert count == 0


class TestWriteEnvFile:
    def test_writes_correct_content(self, tmp_path):
        env_path = tmp_path / ".env"
        write_env_file(env_path, org="myorg", repo="engineering", username="nate", entry_label="otto")
        content = env_path.read_text()
        assert "OTTONATE_GITHUB_ORG=myorg" in content
        assert "OTTONATE_GITHUB_ENGINEERING_REPO=engineering" in content
        assert "OTTONATE_GITHUB_USERNAME=nate" in content
        assert "OTTONATE_GITHUB_AGENT_LABEL=otto" in content

    def test_uses_default_entry_label(self, tmp_path):
        env_path = tmp_path / ".env"
        write_env_file(env_path, org="o", repo="r", username="u")
        content = env_path.read_text()
        assert "OTTONATE_GITHUB_AGENT_LABEL=otto" in content

    def test_overwrites_existing(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("OLD_VAR=old\n")
        write_env_file(env_path, org="o", repo="r", username="u", entry_label="bot")
        content = env_path.read_text()
        assert "OLD_VAR" not in content
        assert "OTTONATE_GITHUB_AGENT_LABEL=bot" in content


class TestSetupResult:
    def test_add_and_summary(self):
        r = SetupResult()
        r.add("GitHub auth", "OK")
        r.add("Labels", "5 created")
        summary = r.summary()
        assert "OK GitHub auth" in summary
        assert "5 created Labels" in summary

    def test_empty_summary(self):
        r = SetupResult()
        assert r.summary() == ""


class TestInitEmptyRepo:
    @pytest.mark.asyncio
    async def test_success_runs_git_commands(self):
        calls = []

        async def mock_exec(*args, **kwargs):
            calls.append(args)
            return _gh_result("")

        with patch("ottonate.setup.asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await init_empty_repo("org", "eng")

        assert result is True
        # 4 init commands (git init, config email, config name, credential helper) + 5 commit/push = 9
        assert len(calls) == 9
        # Verify key git commands were called
        flat_args = [" ".join(c) for c in calls]
        assert any("git init" in a for a in flat_args)
        assert any("git add -A" in a for a in flat_args)
        assert any("git commit" in a for a in flat_args)
        assert any("git push" in a for a in flat_args)
        assert any("git remote add origin" in a for a in flat_args)

    @pytest.mark.asyncio
    async def test_failure_returns_false(self):
        call_count = 0

        async def mock_exec(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Fail on the 5th call (git add -A, which is the first post-init command)
            if call_count == 5:
                return _gh_result("", returncode=1)
            return _gh_result("")

        with patch("ottonate.setup.asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await init_empty_repo("org", "eng")

        assert result is False
