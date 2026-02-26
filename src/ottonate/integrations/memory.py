"""Bedrock AgentCore memory integration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass
class MemoryEntry:
    content: str
    namespace: str
    score: float
    metadata: dict[str, Any]


class OttonateMemory:
    """Pipeline-oriented wrapper around Bedrock AgentCore MemoryClient.

    Store namespace patterns (defined at provisioning time):
      - team:  /ottonate/team/{sessionId}/
      - repo:  /ottonate/repos/{actorId}/{sessionId}/
      - issue: /ottonate/issues/{actorId}/{sessionId}/

    At runtime the SDK resolves {actorId} and {sessionId} from the
    actor_id and session_id params passed to create_event / retrieve_memories.
    """

    ACTOR_ID = "ottonate"

    def __init__(
        self,
        region: str = "us-west-2",
        broad_memory_id: str = "",
        repo_memory_id: str = "",
        jira_memory_id: str = "",
    ):
        self._broad_id = broad_memory_id
        self._repo_id = repo_memory_id
        self._issue_id = jira_memory_id
        self._region = region
        self._client = None
        self._enabled = bool(broad_memory_id or repo_memory_id or jira_memory_id)

    @property
    def client(self):
        if self._client is None:
            try:
                from bedrock_agentcore.memory import MemoryClient

                self._client = MemoryClient(region_name=self._region)
            except Exception as e:
                log.warning("memory_init_failed", error=str(e))
                self._enabled = False
        return self._client

    # -- Search --

    async def search_ticket_context(
        self, ticket_key: str, query: str = "", top_k: int = 5
    ) -> list[MemoryEntry]:
        if not query:
            query = f"ticket {ticket_key} context plan decisions"
        results: list[MemoryEntry] = []
        if self._issue_id:
            results.extend(
                await self._search(self._issue_id, query, ticket_key, top_k)
            )
        if self._broad_id:
            results.extend(
                await self._search(self._broad_id, query, None, top_k)
            )
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    async def search_repo_context(
        self, repo_name: str, query: str = "", top_k: int = 5
    ) -> list[MemoryEntry]:
        if not self._repo_id:
            return []
        if not query:
            query = f"repo {repo_name} patterns bugs conventions"
        return await self._search(self._repo_id, query, repo_name, top_k)

    # -- Store --

    async def store_plan(self, ticket_key: str, plan_summary: str) -> None:
        if not self._issue_id:
            return
        await self._store(self._issue_id, ticket_key, f"plan_{ticket_key}", plan_summary)

    async def store_learnings(self, ticket_key: str, learnings: str) -> None:
        if not self._issue_id:
            return
        await self._store(self._issue_id, ticket_key, f"learnings_{ticket_key}", learnings)

    async def store_repo_pattern(self, repo_name: str, pattern: str) -> None:
        if not self._repo_id:
            return
        await self._store(self._repo_id, repo_name, f"pattern_{uuid.uuid4().hex[:8]}", pattern)

    async def store_repo_bug(self, repo_name: str, description: str) -> None:
        if not self._repo_id:
            return
        await self._store(self._repo_id, repo_name, f"bug_{uuid.uuid4().hex[:8]}", description)

    async def store_decision(self, decision: str) -> None:
        if not self._broad_id:
            return
        await self._store(
            self._broad_id, None, f"decision_{uuid.uuid4().hex[:8]}", decision
        )

    # -- Internals --

    def _namespace_for(self, memory_id: str, actor_id: str) -> str:
        if memory_id == self._broad_id:
            return f"/ottonate/team/{actor_id}/"
        elif memory_id == self._repo_id:
            return f"/ottonate/repos/{actor_id}/"
        elif memory_id == self._issue_id:
            return f"/ottonate/issues/{actor_id}/"
        return f"/ottonate/{actor_id}/"

    async def _search(
        self,
        memory_id: str,
        query: str,
        actor_id_override: str | None,
        top_k: int,
        min_score: float = 0.3,
    ) -> list[MemoryEntry]:
        if not self._enabled or not self.client:
            return []
        results: list[MemoryEntry] = []
        aid = actor_id_override or self.ACTOR_ID
        namespace = self._namespace_for(memory_id, aid)
        try:
            records = self.client.retrieve_memories(
                memory_id=memory_id,
                namespace=namespace,
                query=query,
                actor_id=aid,
                top_k=top_k,
            )
            for record in records:
                score = record.get("score", 1.0)
                if score >= min_score:
                    results.append(
                        MemoryEntry(
                            content=record.get("content", record.get("text", "")),
                            namespace=record.get("namespace", ""),
                            score=score,
                            metadata=record.get("metadata", {}),
                        )
                    )
        except Exception as e:
            log.warning("memory_search_failed", memory_id=memory_id, error=str(e))
        return results

    async def _store(
        self, memory_id: str, actor_id_override: str | None, session_id: str, content: str
    ) -> None:
        if not self._enabled or not self.client:
            return
        aid = actor_id_override or self.ACTOR_ID
        try:
            self.client.create_event(
                memory_id=memory_id,
                actor_id=aid,
                session_id=session_id,
                messages=[
                    (content, "USER"),
                    ("Stored.", "ASSISTANT"),
                ],
            )
            log.debug("memory_stored", memory_id=memory_id, session_id=session_id)
        except Exception as e:
            log.warning("memory_store_failed", memory_id=memory_id, error=str(e))


def format_memory_context(entries: list[MemoryEntry], header: str = "Relevant Context") -> str:
    if not entries:
        return ""
    lines = [f"## {header}\n"]
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i}. [{entry.score:.0%}] {entry.content[:500]}\n")
    return "\n".join(lines)
