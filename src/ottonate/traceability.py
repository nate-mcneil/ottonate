"""Artifact traceability: tracks the full delivery chain from spec to tests."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

import structlog

log = structlog.get_logger()


class ArtifactType(StrEnum):
    SPEC = "spec"
    EPIC = "epic"
    STORY = "story"
    PLAN = "plan"
    PR = "pr"
    TEST = "test"


@dataclass
class Artifact:
    type: ArtifactType
    id: str
    title: str = ""
    url: str = ""
    parent_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class TraceLink:
    source_type: ArtifactType
    source_id: str
    target_type: ArtifactType
    target_id: str
    relationship: str = "produces"


class TraceabilityGraph:
    """In-memory graph of artifact relationships for a pipeline run."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._links: list[TraceLink] = []

    def add_artifact(self, artifact: Artifact) -> None:
        self._artifacts[artifact.id] = artifact

    def add_link(self, link: TraceLink) -> None:
        self._links.append(link)

    def link(
        self,
        source_type: ArtifactType,
        source_id: str,
        target_type: ArtifactType,
        target_id: str,
        relationship: str = "produces",
    ) -> None:
        self._links.append(
            TraceLink(source_type, source_id, target_type, target_id, relationship)
        )

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self._artifacts.get(artifact_id)

    def get_children(self, parent_id: str) -> list[Artifact]:
        child_ids = {
            link.target_id for link in self._links if link.source_id == parent_id
        }
        return [self._artifacts[aid] for aid in child_ids if aid in self._artifacts]

    def get_ancestors(self, artifact_id: str) -> list[Artifact]:
        parent_ids = {
            link.source_id for link in self._links if link.target_id == artifact_id
        }
        ancestors = [self._artifacts[pid] for pid in parent_ids if pid in self._artifacts]
        for pid in list(parent_ids):
            ancestors.extend(self.get_ancestors(pid))
        return ancestors

    def trace_chain(self, artifact_id: str) -> list[Artifact]:
        """Return the full trace chain from root spec to this artifact."""
        chain = self.get_ancestors(artifact_id)
        artifact = self.get_artifact(artifact_id)
        if artifact:
            chain.append(artifact)
        type_order = list(ArtifactType)
        chain.sort(key=lambda a: type_order.index(a.type))
        return chain

    def get_coverage_report(self, epic_id: str) -> dict:
        """Report how much of an epic's spec is covered by stories, PRs, and tests."""
        stories = [a for a in self.get_children(epic_id) if a.type == ArtifactType.STORY]
        total = len(stories)
        with_pr = 0
        with_tests = 0

        for story in stories:
            children = self.get_children(story.id)
            child_types = {c.type for c in children}
            if ArtifactType.PR in child_types:
                with_pr += 1
            if ArtifactType.TEST in child_types:
                with_tests += 1

        return {
            "epic_id": epic_id,
            "total_stories": total,
            "stories_with_pr": with_pr,
            "stories_with_tests": with_tests,
            "pr_coverage": with_pr / total if total else 0,
            "test_coverage": with_tests / total if total else 0,
        }

    def to_dict(self) -> dict:
        return {
            "artifacts": [asdict(a) for a in self._artifacts.values()],
            "links": [asdict(l) for l in self._links],
        }

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> TraceabilityGraph:
        graph = cls()
        data = json.loads(path.read_text())
        for a in data.get("artifacts", []):
            a["type"] = ArtifactType(a["type"])
            graph.add_artifact(Artifact(**a))
        for l_data in data.get("links", []):
            l_data["source_type"] = ArtifactType(l_data["source_type"])
            l_data["target_type"] = ArtifactType(l_data["target_type"])
            graph.add_link(TraceLink(**l_data))
        return graph

    def format_summary(self, epic_id: str) -> str:
        """Human-readable summary of traceability for an epic."""
        report = self.get_coverage_report(epic_id)
        lines = [
            f"Traceability Report for {epic_id}",
            f"  Stories: {report['total_stories']}",
            f"  With PRs: {report['stories_with_pr']} ({report['pr_coverage']:.0%})",
            f"  With Tests: {report['stories_with_tests']} ({report['test_coverage']:.0%})",
        ]
        return "\n".join(lines)
