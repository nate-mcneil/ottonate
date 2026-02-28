"""Issue metrics derived from GitHub timeline events and structured comments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

STAGE_META_PATTERN = re.compile(r"<!-- otto:(.*?) -->")


@dataclass
class IssueMetrics:
    issue_ref: str
    total_stages: int = 0
    total_retries: int = 0
    total_cost_usd: float = 0.0
    was_stuck: bool = False
    stuck_reasons: list[str] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)

    @property
    def needs_retro(self) -> bool:
        return self.total_retries > 0 or self.was_stuck


def parse_stage_comments(comments: list[str]) -> list[dict]:
    """Extract stage metadata dicts from structured HTML comments."""
    stages: list[dict] = []
    for comment in comments:
        match = STAGE_META_PATTERN.search(comment)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            stages.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return stages


def _detect_stuck(timeline: list[dict]) -> bool:
    return any(e["label"] == "agentStuck" and e["event"] == "labeled" for e in timeline)


def _detect_retries(timeline: list[dict]) -> int:
    label_counts: dict[str, int] = {}
    for e in timeline:
        if e["event"] == "labeled":
            label_counts[e["label"]] = label_counts.get(e["label"], 0) + 1
    return sum(count - 1 for count in label_counts.values() if count > 1)


async def build_issue_metrics(github, owner: str, repo: str, number: int) -> IssueMetrics:
    """Build IssueMetrics from GitHub timeline events and structured issue comments."""
    issue_ref = f"{owner}/{repo}#{number}"
    timeline = await github.get_issue_timeline(owner, repo, number)
    comments = await github.get_comments(owner, repo, number)

    stages = parse_stage_comments(comments)
    was_stuck = _detect_stuck(timeline)
    total_retries_from_timeline = _detect_retries(timeline)
    total_retries_from_comments = sum(1 for s in stages if s.get("retry_number", 0) > 0)
    total_retries = max(total_retries_from_timeline, total_retries_from_comments)
    total_cost = sum(s.get("cost_usd", 0.0) for s in stages)
    stuck_reasons = [
        s["stuck_reason"] for s in stages if s.get("was_stuck") and s.get("stuck_reason")
    ]
    if was_stuck and not any(s.get("was_stuck") for s in stages):
        stuck_reasons = []

    return IssueMetrics(
        issue_ref=issue_ref,
        total_stages=len(stages),
        total_retries=total_retries,
        total_cost_usd=total_cost,
        was_stuck=was_stuck or any(s.get("was_stuck") for s in stages),
        stuck_reasons=stuck_reasons,
        stages=stages,
    )
