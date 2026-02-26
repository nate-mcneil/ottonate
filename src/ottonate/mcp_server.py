"""MCP server exposing ottonate's Bedrock AgentCore memory stores."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from bedrock_agentcore.memory import MemoryClient
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from ottonate.config import OttonateConfig

logging.getLogger("bedrock_agentcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

ACTOR_ID = "ottonate-mcp"


class OttonateMemoryClient:
    """Client for ottonate's AgentCore memory stores."""

    def __init__(
        self,
        region: str = "us-west-2",
        broad_memory_id: str = "",
        repo_memory_id: str = "",
        issue_memory_id: str = "",
    ):
        self._broad_id = broad_memory_id
        self._repo_id = repo_memory_id
        self._issue_id = issue_memory_id
        self._region = region
        self._client: MemoryClient | None = None

    @property
    def client(self) -> MemoryClient:
        if self._client is None:
            self._client = MemoryClient(region_name=self._region)
        return self._client

    # -- Store: team --

    async def store_fact(self, content: str) -> dict[str, Any]:
        if not self._broad_id:
            return {"error": "Broad memory not configured"}
        return await self._store(self._broad_id, "/ottonate/team/facts", content)

    async def store_decision(self, content: str) -> dict[str, Any]:
        if not self._broad_id:
            return {"error": "Broad memory not configured"}
        return await self._store(self._broad_id, "/ottonate/team/decisions", content)

    # -- Store: repo --

    async def store_repo_bug(self, repo_name: str, content: str) -> dict[str, Any]:
        if not self._repo_id:
            return {"error": "Repo memory not configured"}
        return await self._store(
            self._repo_id, f"/ottonate/repos/{repo_name}/bugs", content, actor_id=repo_name
        )

    async def store_repo_pattern(self, repo_name: str, content: str) -> dict[str, Any]:
        if not self._repo_id:
            return {"error": "Repo memory not configured"}
        return await self._store(
            self._repo_id, f"/ottonate/repos/{repo_name}/patterns", content, actor_id=repo_name
        )

    # -- Store: issue --

    async def store_plan(self, issue_ref: str, content: str) -> dict[str, Any]:
        if not self._issue_id:
            return {"error": "Issue memory not configured"}
        return await self._store(
            self._issue_id, f"/ottonate/issues/{issue_ref}/plan", content, actor_id=issue_ref
        )

    async def store_learnings(self, issue_ref: str, content: str) -> dict[str, Any]:
        if not self._issue_id:
            return {"error": "Issue memory not configured"}
        return await self._store(
            self._issue_id, f"/ottonate/issues/{issue_ref}/learnings", content, actor_id=issue_ref
        )

    async def store_issue_context(self, issue_ref: str, content: str) -> dict[str, Any]:
        if not self._issue_id:
            return {"error": "Issue memory not configured"}
        return await self._store(
            self._issue_id, f"/ottonate/issues/{issue_ref}/context", content, actor_id=issue_ref
        )

    # -- Search --

    async def search_team(
        self, query: str, top_k: int = 10, min_score: float = 0.3
    ) -> list[dict[str, Any]]:
        if not self._broad_id:
            return []
        return await self._search(self._broad_id, query, top_k=top_k, min_score=min_score)

    async def search_repo(
        self, repo_name: str, query: str, top_k: int = 10, min_score: float = 0.3
    ) -> list[dict[str, Any]]:
        if not self._repo_id:
            return []
        return await self._search(
            self._repo_id, query, actor_id=repo_name, top_k=top_k, min_score=min_score
        )

    async def search_issue(
        self, issue_ref: str, query: str, top_k: int = 10, min_score: float = 0.3
    ) -> list[dict[str, Any]]:
        if not self._issue_id:
            return []
        return await self._search(
            self._issue_id, query, actor_id=issue_ref, top_k=top_k, min_score=min_score
        )

    async def search_all(
        self,
        query: str,
        repo_name: str | None = None,
        issue_ref: str | None = None,
        top_k: int = 10,
        min_score: float = 0.3,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        results.extend(await self.search_team(query, top_k, min_score))
        if repo_name:
            results.extend(await self.search_repo(repo_name, query, top_k, min_score))
        if issue_ref:
            results.extend(await self.search_issue(issue_ref, query, top_k, min_score))
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results[:top_k]

    # -- Internals --

    def _namespace_to_session_prefix(self, namespace: str) -> str:
        prefix = namespace.lstrip("/").replace("/", "_")
        if not prefix or not prefix[0].isalnum():
            prefix = "s" + prefix
        return prefix

    async def _store(
        self,
        memory_id: str,
        namespace: str,
        content: str,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            session_prefix = self._namespace_to_session_prefix(namespace)
            session_id = f"{session_prefix}_{uuid.uuid4().hex[:8]}"
            aid = actor_id or ACTOR_ID
            self.client.save_turn(
                memory_id=memory_id,
                actor_id=aid,
                session_id=session_id,
                user_input=content,
                agent_response=f"Stored to {namespace}",
            )
            return {"success": True, "namespace": namespace, "session_id": session_id}
        except Exception as e:
            return {"error": str(e)}

    async def _search(
        self,
        memory_id: str,
        query: str,
        actor_id: str | None = None,
        top_k: int = 10,
        min_score: float = 0.3,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        aid = actor_id or ACTOR_ID
        try:
            records = self.client.retrieve_memories(
                memory_id=memory_id,
                query=query,
                actor_id=aid,
                top_k=top_k,
            )
            for record in records:
                score = record.get("score", 1.0)
                if score >= min_score:
                    results.append(
                        {
                            "content": record.get("content", record.get("text", "")),
                            "namespace": record.get("namespace", ""),
                            "score": score,
                        }
                    )
        except Exception as e:
            logger.error("Memory search failed: %s", e)
        return results


# -- Tool definitions --

STORE_TOOLS: list[Tool] = [
    Tool(
        name="store_fact",
        description=(
            "Store a fact to shared team knowledge. "
            "Use for platform-wide knowledge that applies across repos."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact or knowledge to store"},
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="store_decision",
        description=(
            "Record an architectural decision. "
            "Use when making or discovering important technical decisions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The architectural decision and its rationale",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="store_repo_bug",
        description="Document a bug fix with root cause for a specific repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_name": {"type": "string", "description": "The repository name"},
                "content": {
                    "type": "string",
                    "description": "Description of the bug, root cause, and fix",
                },
            },
            "required": ["repo_name", "content"],
        },
    ),
    Tool(
        name="store_repo_pattern",
        description="Save a discovered code pattern or idiom for a specific repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "repo_name": {"type": "string", "description": "The repository name"},
                "content": {
                    "type": "string",
                    "description": "Description of the pattern and when to use it",
                },
            },
            "required": ["repo_name", "content"],
        },
    ),
    Tool(
        name="store_plan",
        description="Store a development plan for a GitHub issue.",
        inputSchema={
            "type": "object",
            "properties": {
                "issue_ref": {
                    "type": "string",
                    "description": "The issue reference (e.g. owner/repo#42)",
                },
                "content": {"type": "string", "description": "The development plan"},
            },
            "required": ["issue_ref", "content"],
        },
    ),
    Tool(
        name="store_learnings",
        description=(
            "Document learnings from working on a GitHub issue. "
            "Use for insights, unexpected challenges, or best practices discovered."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_ref": {
                    "type": "string",
                    "description": "The issue reference (e.g. owner/repo#42)",
                },
                "content": {"type": "string", "description": "The learnings or insights gained"},
            },
            "required": ["issue_ref", "content"],
        },
    ),
    Tool(
        name="store_issue_context",
        description=(
            "Store context about a GitHub issue. "
            "Use for general information, background, or ongoing notes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_ref": {
                    "type": "string",
                    "description": "The issue reference (e.g. owner/repo#42)",
                },
                "content": {"type": "string", "description": "The context or information to store"},
            },
            "required": ["issue_ref", "content"],
        },
    ),
]

SEARCH_TOOLS: list[Tool] = [
    Tool(
        name="search_memory",
        description=(
            "Semantic search across all ottonate memories. "
            "Returns relevant facts, decisions, patterns, and issue context."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "repo_name": {
                    "type": "string",
                    "description": "Optional: include repo-specific memories in search",
                },
                "issue_ref": {
                    "type": "string",
                    "description": "Optional: include issue memories in search",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="search_repo",
        description=(
            "Search within a specific repository's memories for patterns, bugs, and conventions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo_name": {"type": "string", "description": "The repository name"},
                "query": {"type": "string", "description": "The search query"},
                "top_k": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["repo_name", "query"],
        },
    ),
    Tool(
        name="search_issue",
        description=(
            "Search within a GitHub issue's memories. "
            "Returns plans, decisions, and learnings related to that issue."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_ref": {
                    "type": "string",
                    "description": "The issue reference (e.g. owner/repo#42)",
                },
                "query": {"type": "string", "description": "The search query"},
                "top_k": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["issue_ref", "query"],
        },
    ),
    Tool(
        name="search_similar",
        description=(
            "Find similar past work based on a description. Searches across all memory stores."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Description of the work to find similar examples of",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum results to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["description"],
        },
    ),
]


def _serialize_results(results: list[dict[str, Any]]) -> str:
    return json.dumps(results)


def create_server() -> Server:
    """Create and configure the MCP server."""
    server = Server("ottonate-mcp")
    config = OttonateConfig()
    memory = OttonateMemoryClient(
        region=config.agentcore_region,
        broad_memory_id=config.agentcore_broad_memory_id,
        repo_memory_id=config.agentcore_repo_memory_id,
        issue_memory_id=config.agentcore_issue_memory_id,
    )

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return STORE_TOOLS + SEARCH_TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        # -- Store handlers --
        if name == "store_fact":
            result = await memory.store_fact(arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "store_decision":
            result = await memory.store_decision(arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "store_repo_bug":
            result = await memory.store_repo_bug(arguments["repo_name"], arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "store_repo_pattern":
            result = await memory.store_repo_pattern(arguments["repo_name"], arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "store_plan":
            result = await memory.store_plan(arguments["issue_ref"], arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "store_learnings":
            result = await memory.store_learnings(arguments["issue_ref"], arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        if name == "store_issue_context":
            result = await memory.store_issue_context(arguments["issue_ref"], arguments["content"])
            return [TextContent(type="text", text=json.dumps(result))]

        # -- Search handlers --
        if name == "search_memory":
            results = await memory.search_all(
                query=arguments["query"],
                repo_name=arguments.get("repo_name"),
                issue_ref=arguments.get("issue_ref"),
                top_k=arguments.get("top_k", 10),
            )
            return [TextContent(type="text", text=_serialize_results(results))]

        if name == "search_repo":
            results = await memory.search_repo(
                repo_name=arguments["repo_name"],
                query=arguments["query"],
                top_k=arguments.get("top_k", 10),
            )
            return [TextContent(type="text", text=_serialize_results(results))]

        if name == "search_issue":
            results = await memory.search_issue(
                issue_ref=arguments["issue_ref"],
                query=arguments["query"],
                top_k=arguments.get("top_k", 10),
            )
            return [TextContent(type="text", text=_serialize_results(results))]

        if name == "search_similar":
            results = await memory.search_all(
                query=arguments["description"],
                top_k=arguments.get("top_k", 10),
            )
            return [TextContent(type="text", text=_serialize_results(results))]

        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    return server


async def run_server() -> None:
    """Run the MCP server over stdio."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
