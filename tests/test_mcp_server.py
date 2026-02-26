"""Tests for the MCP memory server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ottonate.mcp_server import OttonateMemoryClient, create_server


@pytest.fixture
def mock_bedrock_client():
    client = MagicMock()
    client.save_turn = MagicMock()
    client.retrieve_memories = MagicMock(return_value=[])
    return client


@pytest.fixture
def memory_client(mock_bedrock_client):
    with patch("ottonate.mcp_server.MemoryClient", return_value=mock_bedrock_client):
        client = OttonateMemoryClient(
            region="us-west-2",
            broad_memory_id="broad-123",
            repo_memory_id="repo-456",
            issue_memory_id="issue-789",
        )
        _ = client.client
    return client


class TestOttonateMemoryClient:
    def test_init_stores_ids(self, memory_client):
        assert memory_client._broad_id == "broad-123"
        assert memory_client._repo_id == "repo-456"
        assert memory_client._issue_id == "issue-789"

    @pytest.mark.asyncio
    async def test_store_fact_calls_save_turn(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_fact("Python 3.11 is required")
        assert result["success"] is True
        mock_bedrock_client.save_turn.assert_called_once()
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "broad-123"
        assert call_kwargs.kwargs["user_input"] == "Python 3.11 is required"

    @pytest.mark.asyncio
    async def test_store_fact_returns_error_when_not_configured(self, mock_bedrock_client):
        with patch("ottonate.mcp_server.MemoryClient", return_value=mock_bedrock_client):
            client = OttonateMemoryClient(region="us-west-2", broad_memory_id="")
        result = await client.store_fact("something")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_store_decision_uses_broad_store(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_decision("Use PostgreSQL over MySQL")
        assert result["success"] is True
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "broad-123"

    @pytest.mark.asyncio
    async def test_store_repo_bug(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_repo_bug("ottonate", "Off-by-one in pagination")
        assert result["success"] is True
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "repo-456"
        assert call_kwargs.kwargs["actor_id"] == "ottonate"

    @pytest.mark.asyncio
    async def test_store_repo_pattern(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_repo_pattern("ottonate", "Use Pydantic Settings")
        assert result["success"] is True
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "repo-456"

    @pytest.mark.asyncio
    async def test_store_plan(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_plan("org/repo#42", "Step 1: do the thing")
        assert result["success"] is True
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "issue-789"
        assert call_kwargs.kwargs["actor_id"] == "org/repo#42"

    @pytest.mark.asyncio
    async def test_store_learnings(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_learnings("org/repo#42", "Watch for race conditions")
        assert result["success"] is True
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "issue-789"

    @pytest.mark.asyncio
    async def test_store_issue_context(self, memory_client, mock_bedrock_client):
        result = await memory_client.store_issue_context("org/repo#42", "Blocked on API access")
        assert result["success"] is True
        call_kwargs = mock_bedrock_client.save_turn.call_args
        assert call_kwargs.kwargs["memory_id"] == "issue-789"

    @pytest.mark.asyncio
    async def test_search_team_returns_entries(self, memory_client, mock_bedrock_client):
        mock_bedrock_client.retrieve_memories.return_value = [
            {
                "content": "Python is great",
                "namespace": "/ottonate/team/",
                "score": 0.9,
                "metadata": {},
            },
        ]
        results = await memory_client.search_team("Python", top_k=5)
        assert len(results) == 1
        assert results[0]["content"] == "Python is great"
        assert results[0]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_search_repo(self, memory_client, mock_bedrock_client):
        mock_bedrock_client.retrieve_memories.return_value = [
            {
                "content": "Use structlog",
                "namespace": "/ottonate/repos/ottonate/",
                "score": 0.85,
                "metadata": {},
            },
        ]
        results = await memory_client.search_repo("ottonate", "logging")
        assert len(results) == 1
        assert results[0]["content"] == "Use structlog"

    @pytest.mark.asyncio
    async def test_search_issue(self, memory_client, mock_bedrock_client):
        mock_bedrock_client.retrieve_memories.return_value = [
            {"content": "Plan: step 1", "score": 0.8, "metadata": {}},
        ]
        results = await memory_client.search_issue("org/repo#42", "plan")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_all_aggregates(self, memory_client, mock_bedrock_client):
        mock_bedrock_client.retrieve_memories.return_value = [
            {"content": "hit", "score": 0.7, "metadata": {}},
        ]
        results = await memory_client.search_all(
            "test query", repo_name="ottonate", issue_ref="org/repo#1"
        )
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_search_filters_low_scores(self, memory_client, mock_bedrock_client):
        mock_bedrock_client.retrieve_memories.return_value = [
            {"content": "low", "score": 0.1, "metadata": {}},
        ]
        results = await memory_client.search_team("query")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_store_handles_exception(self, memory_client, mock_bedrock_client):
        mock_bedrock_client.save_turn.side_effect = RuntimeError("boom")
        result = await memory_client.store_fact("something")
        assert "error" in result


class TestMcpServer:
    @pytest.mark.asyncio
    async def test_list_tools_returns_all(self):
        with patch("ottonate.mcp_server.MemoryClient"):
            server = create_server()
        handlers = server.request_handlers
        list_tools_handler = None
        for key, handler in handlers.items():
            if "ListTools" in str(key):
                list_tools_handler = handler
                break

        assert list_tools_handler is not None

    @pytest.mark.asyncio
    async def test_create_server_returns_server(self):
        with patch("ottonate.mcp_server.MemoryClient"):
            server = create_server()
        assert server is not None
