"""Tests for the ottonate dashboard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ottonate.config import OttonateConfig
from ottonate.dashboard.api import PHASE_MAP, _classify_issue, _get_stage_label
from ottonate.dashboard.app import create_app


@pytest.fixture
def config(tmp_path):
    return OttonateConfig(
        github_org="testorg",
        github_agent_label="otto",
        db_path=tmp_path / "test.db",
    )


@pytest.fixture
def mock_github():
    gh = AsyncMock()
    gh.search_issues = AsyncMock(return_value=[])
    gh.swap_label = AsyncMock()
    gh.add_comment = AsyncMock()
    gh.merge_pr = AsyncMock()
    return gh


@pytest.fixture
def app(config, mock_github, tmp_path):
    application = create_app(config)
    application.state.github = mock_github
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


SAMPLE_ISSUES = [
    {
        "repository": {"name": "flow-api"},
        "number": 42,
        "title": "Webhook handler",
        "labels": [{"name": "otto"}, {"name": "agentReview"}],
    },
    {
        "repository": {"name": "flow-api"},
        "number": 43,
        "title": "Rate limiting",
        "labels": [{"name": "otto"}, {"name": "agentImplementing"}],
    },
    {
        "repository": {"name": "flow-ui"},
        "number": 18,
        "title": "Settings redesign",
        "labels": [{"name": "otto"}, {"name": "agentStuck"}],
    },
    {
        "repository": {"name": "flow-api"},
        "number": 55,
        "title": "Idempotency keys",
        "labels": [{"name": "otto"}, {"name": "agentMergeReady"}],
    },
    {
        "repository": {"name": "flow-api"},
        "number": 60,
        "title": "Planning in progress",
        "labels": [{"name": "otto"}, {"name": "agentPlanning"}],
    },
]


class TestGetStageLabel:
    def test_finds_stage_label(self):
        labels = [{"name": "otto"}, {"name": "agentReview"}]
        assert _get_stage_label(labels) == "agentReview"

    def test_handles_string_labels(self):
        labels = ["otto", "agentPlanning"]
        assert _get_stage_label(labels) == "agentPlanning"

    def test_returns_none_for_no_stage(self):
        labels = [{"name": "otto"}, {"name": "bug"}]
        assert _get_stage_label(labels) is None


class TestClassifyIssue:
    def test_classifies_issue(self):
        result = _classify_issue(SAMPLE_ISSUES[0], "otto")
        assert result["repo"] == "flow-api"
        assert result["number"] == 42
        assert result["stage"] == "agentReview"
        assert result["phase"] == "awaiting_human"

    def test_returns_none_for_no_stage(self):
        issue = {
            "repository": {"name": "repo"},
            "number": 1,
            "title": "No stage",
            "labels": [{"name": "otto"}],
        }
        assert _classify_issue(issue, "otto") is None


class TestPhaseMap:
    def test_all_labels_mapped(self):
        from ottonate.models import Label

        for label in Label:
            assert label.value in PHASE_MAP, f"{label.value} not in PHASE_MAP"

    def test_planning_phases(self):
        assert PHASE_MAP["agentPlanning"] == "planning"
        assert PHASE_MAP["agentPlanReview"] == "planning"
        assert PHASE_MAP["agentPlan"] == "planning"
        assert PHASE_MAP["agentSpec"] == "planning"

    def test_implementing_phases(self):
        assert PHASE_MAP["agentImplementing"] == "implementing"
        assert PHASE_MAP["agentCIFix"] == "implementing"

    def test_awaiting_human_phases(self):
        assert PHASE_MAP["agentReview"] == "awaiting_human"
        assert PHASE_MAP["agentMergeReady"] == "awaiting_human"

    def test_stuck_phase(self):
        assert PHASE_MAP["agentStuck"] == "stuck"


class TestApiIssues:
    def test_returns_classified_issues(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/api/issues")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 5
        stages = {d["stage"] for d in data}
        assert "agentReview" in stages
        assert "agentImplementing" in stages

    def test_empty_when_no_issues(self, client, mock_github):
        mock_github.search_issues.return_value = []
        resp = client.get("/api/issues")
        assert resp.status_code == 200
        assert resp.json() == []


class TestApiAttention:
    def test_returns_only_human_gate_issues(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/api/attention")
        assert resp.status_code == 200
        data = resp.json()
        stages = {d["stage"] for d in data}
        assert "agentImplementing" not in stages
        assert "agentPlanning" not in stages
        assert "agentStuck" in stages
        assert "agentReview" in stages
        assert "agentMergeReady" in stages

    def test_priority_ordering(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/api/attention")
        data = resp.json()
        assert data[0]["stage"] == "agentStuck"
        assert data[1]["stage"] == "agentMergeReady"
        assert data[2]["stage"] == "agentReview"


class TestApiMetrics:
    def test_returns_metrics(self, client):
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "throughput" in data
        assert "stage_stats" in data
        assert "recent_completions" in data

    def test_days_filter(self, client):
        resp = client.get("/api/metrics?days=7")
        assert resp.status_code == 200


class TestApiUnstick:
    def test_swaps_label(self, client, mock_github):
        resp = client.post(
            "/api/issues/testorg/flow-ui/18/unstick",
            json={"target_stage": "agentPlanning"},
        )
        assert resp.status_code == 200
        mock_github.swap_label.assert_called_once()
        call_args = mock_github.swap_label.call_args
        assert call_args[0][0] == "testorg"
        assert call_args[0][1] == "flow-ui"
        assert call_args[0][2] == 18

    def test_rejects_invalid_stage(self, client, mock_github):
        resp = client.post(
            "/api/issues/testorg/flow-ui/18/unstick",
            json={"target_stage": "bogusLabel"},
        )
        assert resp.status_code == 400


class TestApiApprove:
    def test_posts_comment(self, client, mock_github):
        resp = client.post("/api/issues/testorg/flow-api/42/approve")
        assert resp.status_code == 200
        mock_github.add_comment.assert_called_once()


class TestApiMergePr:
    def test_merges_pr(self, client, mock_github):
        resp = client.post("/api/prs/testorg/flow-api/87/merge")
        assert resp.status_code == 200
        mock_github.merge_pr.assert_called_once_with("testorg", "flow-api", 87)

    def test_returns_500_on_failure(self, client, mock_github):
        mock_github.merge_pr.side_effect = RuntimeError("merge failed")
        resp = client.post("/api/prs/testorg/flow-api/87/merge")
        assert resp.status_code == 500


class TestHtmlViews:
    def test_pipeline_board_renders(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Ottonate" in resp.text
        assert "Pipeline" in resp.text
        assert "flow-api" in resp.text

    def test_attention_page_renders(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/attention")
        assert resp.status_code == 200
        assert "Attention" in resp.text

    def test_metrics_page_renders(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "Metrics" in resp.text
        assert "Throughput" in resp.text

    def test_pipeline_empty_state(self, client, mock_github):
        mock_github.search_issues.return_value = []
        resp = client.get("/")
        assert resp.status_code == 200
        assert "No issues" in resp.text

    def test_attention_empty_state(self, client, mock_github):
        mock_github.search_issues.return_value = []
        resp = client.get("/attention")
        assert resp.status_code == 200
        assert "All clear" in resp.text


class TestHtmlPartials:
    def test_board_partial(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/partials/board")
        assert resp.status_code == 200
        assert "flow-api" in resp.text
        assert "<!DOCTYPE" not in resp.text

    def test_queue_partial(self, client, mock_github):
        mock_github.search_issues.return_value = SAMPLE_ISSUES
        resp = client.get("/partials/queue")
        assert resp.status_code == 200
        assert "<!DOCTYPE" not in resp.text

    def test_stats_partial(self, client):
        resp = client.get("/partials/stats")
        assert resp.status_code == 200
        assert "Throughput" in resp.text
        assert "<!DOCTYPE" not in resp.text
