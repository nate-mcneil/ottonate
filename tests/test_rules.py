from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ottonate.config import OttonateConfig
from ottonate.integrations.github import GitHubClient
from ottonate.rules import (
    ResolvedRules,
    _merge_agent_context,
    _merge_config,
    _parse_repo_catalog,
    load_rules,
)


@pytest.fixture
def config() -> OttonateConfig:
    return OttonateConfig(
        github_org="testorg",
        github_engineering_repo="engineering",
        github_agent_label="otto",
    )


@pytest.fixture
def mock_github() -> AsyncMock:
    return AsyncMock(spec=GitHubClient)


class TestMergeConfig:
    def test_overlay_replaces_values(self):
        base = {"branch_pattern": "default", "notify_team": ""}
        overlay = {"notify_team": "backend"}
        result = _merge_config(base, overlay)
        assert result["branch_pattern"] == "default"
        assert result["notify_team"] == "backend"

    def test_deep_merge_dicts(self):
        base = {"required_reviewers": {"default": [], "paths": {}}}
        overlay = {"required_reviewers": {"paths": {"src/db/**": ["dba-team"]}}}
        result = _merge_config(base, overlay)
        assert result["required_reviewers"]["default"] == []
        assert result["required_reviewers"]["paths"]["src/db/**"] == ["dba-team"]

    def test_overlay_replaces_lists(self):
        base = {"required_reviewers": {"default": ["alice"]}}
        overlay = {"required_reviewers": {"default": ["bob"]}}
        result = _merge_config(base, overlay)
        assert result["required_reviewers"]["default"] == ["bob"]


class TestMergeAgentContext:
    def test_all_sections(self):
        result = _merge_agent_context("org rules", "repo rules", "arch context")
        assert "Architecture" in result
        assert "Organization Guidelines" in result
        assert "Repository Guidelines" in result
        assert result.index("Architecture") < result.index("Organization Guidelines")
        assert result.index("Organization Guidelines") < result.index("Repository Guidelines")

    def test_empty_sections_omitted(self):
        result = _merge_agent_context("", "repo rules", "")
        assert "Architecture" not in result
        assert "Organization" not in result
        assert "Repository Guidelines" in result


class TestParseRepoCatalog:
    def test_parses_entries(self):
        context = """## Repository Catalog

## flow-api
- **Purpose**: Backend API
- **Stack**: Python, FastAPI
- **Domain**: Core
- **Owner**: @backend

## flow-web
- **Purpose**: Frontend
- **Stack**: TypeScript, React
"""
        repos = _parse_repo_catalog(context)
        assert len(repos) == 2
        assert repos[0]["name"] == "flow-api"
        assert repos[0]["stack"] == "Python, FastAPI"
        assert repos[1]["name"] == "flow-web"

    def test_empty_context(self):
        assert _parse_repo_catalog("") == []

    def test_no_catalog_section(self):
        assert _parse_repo_catalog("Some other content") == []


class TestLoadRules:
    @pytest.mark.asyncio
    async def test_loads_defaults_when_no_files(self, config, mock_github):
        mock_github.get_file_content = AsyncMock(return_value=None)
        rules = await load_rules("testorg", "my-repo", config, mock_github)
        assert isinstance(rules, ResolvedRules)
        assert rules.branch_pattern == "{issue_number}/{description}"
        assert rules.entry_label == "otto"

    @pytest.mark.asyncio
    async def test_org_rules_override_defaults(self, config, mock_github):
        async def _mock_content(owner, repo, path, ref="main"):
            if repo == "engineering" and path == ".ottonate/config.yml":
                return "branch_pattern: 'feature/{issue_number}'\nnotify_team: backend"
            if repo == "engineering" and path == ".ottonate/rules.md":
                return "Always write tests first."
            return None

        mock_github.get_file_content = AsyncMock(side_effect=_mock_content)
        rules = await load_rules("testorg", "my-repo", config, mock_github)
        assert rules.branch_pattern == "feature/{issue_number}"
        assert rules.notify_team == "backend"
        assert "tests first" in rules.agent_context

    @pytest.mark.asyncio
    async def test_repo_rules_override_org(self, config, mock_github):
        async def _mock_content(owner, repo, path, ref="main"):
            if repo == "engineering" and path == ".ottonate/config.yml":
                return "notify_team: org-team"
            if repo == "my-repo" and path == ".ottonate/config.yml":
                return "notify_team: repo-team"
            return None

        mock_github.get_file_content = AsyncMock(side_effect=_mock_content)
        rules = await load_rules("testorg", "my-repo", config, mock_github)
        assert rules.notify_team == "repo-team"

    @pytest.mark.asyncio
    async def test_engineering_repo_skips_repo_layer(self, config, mock_github):
        call_count = 0

        async def _mock_content(owner, repo, path, ref="main"):
            nonlocal call_count
            call_count += 1
            return None

        mock_github.get_file_content = AsyncMock(side_effect=_mock_content)
        await load_rules("testorg", "engineering", config, mock_github)
        # Should not double-load: only eng repo layer + 2 arch files = 4 calls
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_architecture_context_loaded(self, config, mock_github):
        async def _mock_content(owner, repo, path, ref="main"):
            if path == "architecture/overview.md":
                return "# System Overview\nMicroservices architecture"
            if path == "architecture/repos.md":
                return "## my-repo\n- **Purpose**: API service"
            return None

        mock_github.get_file_content = AsyncMock(side_effect=_mock_content)
        rules = await load_rules("testorg", "my-repo", config, mock_github)
        assert "Microservices" in rules.architecture_context
        assert len(rules.repo_catalog) >= 1
