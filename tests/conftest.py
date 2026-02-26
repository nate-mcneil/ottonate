from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ottonate.config import OttonateConfig
from ottonate.github import GitHubClient
from ottonate.metrics import MetricsStore
from ottonate.models import Ticket
from ottonate.rules import ResolvedRules


@pytest.fixture
def config() -> OttonateConfig:
    return OttonateConfig(
        github_org="testorg",
        github_engineering_repo="engineering",
        github_username="test-bot",
        github_agent_label="otto",
        github_notify_team="engineering",
        max_plan_retries=2,
        max_implement_retries=2,
        max_ci_fix_retries=3,
        max_review_retries=5,
    )


@pytest.fixture
def mock_github() -> AsyncMock:
    gh = AsyncMock(spec=GitHubClient)
    gh.get_comments = AsyncMock(return_value=[])
    gh.get_issue_body = AsyncMock(return_value="# Test\n\nDescription")
    gh.get_issue = AsyncMock(return_value={"title": "Test issue", "labels": []})
    gh.get_issue_labels = AsyncMock(return_value=["otto"])
    gh.get_file_content = AsyncMock(return_value=None)
    gh.get_pr_state = AsyncMock(return_value="OPEN")
    return gh


@pytest.fixture
async def mock_metrics(tmp_path) -> MetricsStore:
    store = MetricsStore(tmp_path / "test.db")
    await store.init_db()
    return store


@pytest.fixture
def sample_ticket() -> Ticket:
    return Ticket(
        owner="testorg",
        repo="test-repo",
        issue_number=42,
        labels={"otto"},
        summary="Test issue",
        work_dir="/tmp/workspaces/testorg_test-repo_42",
    )


@pytest.fixture
def sample_rules() -> ResolvedRules:
    return ResolvedRules(
        notify_team="engineering",
        agent_context="## Testing\n- Write tests first\n",
    )
