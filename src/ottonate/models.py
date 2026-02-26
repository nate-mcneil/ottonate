"""Data models and enums for ottonate."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Label(StrEnum):
    """GitHub labels that drive the state machine. Each label maps to a pipeline stage.

    The entry label (e.g. "otto") is NOT part of this enum -- it is configurable
    via ``OttonateConfig.github_agent_label`` and resolved at runtime.
    """

    # -- Spec-driven development --
    SPEC = "agentSpec"
    SPEC_REVIEW = "agentSpecReview"
    SPEC_APPROVED = "agentSpecApproved"
    BACKLOG_GEN = "agentBacklogGen"
    BACKLOG_REVIEW = "agentBacklogReview"
    # -- Dev planning & implementation --
    PLANNING = "agentPlanning"
    PLAN_REVIEW = "agentPlanReview"
    PLAN = "agentPlan"
    IMPLEMENTING = "agentImplementing"
    PR = "agentPR"
    CI_FIX = "agentCIFix"
    SELF_REVIEW = "agentSelfReview"
    REVIEW = "agentReview"
    ADDRESSING_REVIEW = "agentAddressingReview"
    MERGE_READY = "agentMergeReady"
    STUCK = "agentStuck"


STAGE_LABELS = set(Label)

ACTIONABLE_LABELS = {
    Label.SPEC_REVIEW,
    Label.SPEC_APPROVED,
    Label.BACKLOG_REVIEW,
    Label.PLAN_REVIEW,
    Label.PLAN,
    Label.PR,
    Label.SELF_REVIEW,
    Label.REVIEW,
    Label.MERGE_READY,
}

IN_PROGRESS_LABELS = {
    Label.SPEC,
    Label.BACKLOG_GEN,
    Label.PLANNING,
    Label.IMPLEMENTING,
    Label.CI_FIX,
    Label.ADDRESSING_REVIEW,
}


@dataclass
class Ticket:
    """A GitHub issue with the context ottonate needs."""

    owner: str
    repo: str
    issue_number: int
    labels: set[str]
    summary: str = ""
    pr_number: int | None = None
    plan: str | None = None
    work_dir: str | None = None
    spec_pr_number: int | None = None
    backlog_pr_number: int | None = None
    project_id: str | None = None

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def issue_ref(self) -> str:
        return f"{self.full_repo}#{self.issue_number}"

    @property
    def agent_label(self) -> Label | None:
        for label in STAGE_LABELS:
            if label.value in self.labels:
                return label
        return None


@dataclass
class StageResult:
    text: str
    session_id: str
    cost_usd: float = 0.0
    turns_used: int = 0
    is_error: bool = False


class CIStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"


@dataclass
class ReviewComment:
    id: int
    author: str
    body: str
    path: str | None = None
    line: int | None = None
    created_at: str | None = None
