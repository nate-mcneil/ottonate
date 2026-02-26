from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ottonate.models import Label
from ottonate.scheduler import Scheduler


@pytest.fixture
def scheduler(config):
    with patch("ottonate.scheduler.GitHubClient") as mock_gh_cls:
        s = Scheduler(config)
        s.github = mock_gh_cls.return_value
        s.pipeline = MagicMock()
        s.pipeline.handle = AsyncMock()
        s.pipeline.handle_new = AsyncMock()
        return s


class TestProcessSingle:
    @pytest.mark.asyncio
    async def test_dispatches_issue(self, scheduler):
        scheduler.github.get_issue_labels = AsyncMock(return_value=["otto", "agentPlan"])
        scheduler.github.get_issue = AsyncMock(return_value={"title": "Test"})

        with (
            patch.object(scheduler, "_ensure_workspace", new_callable=AsyncMock),
            patch("ottonate.scheduler.load_rules", new_callable=AsyncMock),
        ):
            await scheduler.process_single("testorg", "test-repo", 42)

        scheduler.pipeline.handle.assert_called_once()


class TestPollAndDispatch:
    @pytest.mark.asyncio
    async def test_skips_in_flight(self, scheduler):
        scheduler._in_flight.add("testorg/test-repo#42")
        scheduler.github.search_issues = AsyncMock(
            return_value=[
                {
                    "repository": {"name": "test-repo"},
                    "number": 42,
                    "labels": [{"name": "otto"}, {"name": Label.PLAN.value}],
                    "title": "test",
                }
            ]
        )

        await scheduler._poll_and_dispatch()
        scheduler.pipeline.handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_cooldown_skips_poll(self, scheduler):
        scheduler._rate_limited_until = time.monotonic() + 9999
        scheduler.github.search_issues = AsyncMock()

        await scheduler._poll_and_dispatch()
        scheduler.github.search_issues.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_org(self, scheduler):
        scheduler.config.github_org = ""
        scheduler.github.search_issues = AsyncMock()

        await scheduler._poll_and_dispatch()
        scheduler.github.search_issues.assert_not_called()
