from __future__ import annotations

from pathlib import Path

from ottonate.traceability import Artifact, ArtifactType, TraceabilityGraph


class TestTraceabilityGraph:
    def _build_graph(self) -> TraceabilityGraph:
        g = TraceabilityGraph()
        g.add_artifact(Artifact(ArtifactType.SPEC, "spec:FLOW-100", "Login Feature"))
        g.add_artifact(Artifact(ArtifactType.EPIC, "FLOW-100", "Login Epic"))
        g.add_artifact(Artifact(ArtifactType.STORY, "FLOW-101", "Login form"))
        g.add_artifact(Artifact(ArtifactType.STORY, "FLOW-102", "OAuth"))
        g.add_artifact(Artifact(ArtifactType.PR, "PR#42", "Login form PR"))
        g.add_artifact(Artifact(ArtifactType.TEST, "test:login", "Login tests"))

        g.link(ArtifactType.SPEC, "spec:FLOW-100", ArtifactType.EPIC, "FLOW-100")
        g.link(ArtifactType.EPIC, "FLOW-100", ArtifactType.STORY, "FLOW-101")
        g.link(ArtifactType.EPIC, "FLOW-100", ArtifactType.STORY, "FLOW-102")
        g.link(ArtifactType.STORY, "FLOW-101", ArtifactType.PR, "PR#42")
        g.link(ArtifactType.STORY, "FLOW-101", ArtifactType.TEST, "test:login")
        return g

    def test_get_children(self):
        g = self._build_graph()
        children = g.get_children("FLOW-100")
        child_ids = {c.id for c in children}
        assert child_ids == {"FLOW-101", "FLOW-102"}

    def test_get_ancestors(self):
        g = self._build_graph()
        ancestors = g.get_ancestors("PR#42")
        ancestor_ids = {a.id for a in ancestors}
        assert "FLOW-101" in ancestor_ids

    def test_coverage_report(self):
        g = self._build_graph()
        report = g.get_coverage_report("FLOW-100")
        assert report["total_stories"] == 2
        assert report["stories_with_pr"] == 1
        assert report["stories_with_tests"] == 1
        assert report["pr_coverage"] == 0.5
        assert report["test_coverage"] == 0.5

    def test_trace_chain(self):
        g = self._build_graph()
        chain = g.trace_chain("PR#42")
        types = [a.type for a in chain]
        assert ArtifactType.STORY in types
        assert ArtifactType.PR in types

    def test_save_and_load(self, tmp_path: Path):
        g = self._build_graph()
        path = tmp_path / "trace.json"
        g.save(path)

        loaded = TraceabilityGraph.load(path)
        assert loaded.get_artifact("FLOW-101") is not None
        assert len(loaded.get_children("FLOW-100")) == 2

    def test_format_summary(self):
        g = self._build_graph()
        summary = g.format_summary("FLOW-100")
        assert "Stories: 2" in summary
        assert "50%" in summary
