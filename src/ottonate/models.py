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
    # -- Idea pipeline (Step 0) --
    IDEA_TRIAGE = "agentIdeaTriage"
    IDEA_REVIEW = "agentIdeaReview"
    IDEA_REFINING = "agentIdeaRefining"
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
    RETRO = "agentRetro"
    STUCK = "agentStuck"


STAGE_LABELS = set(Label)

ACTIONABLE_LABELS = {
    Label.IDEA_REVIEW,
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
    Label.IDEA_TRIAGE,
    Label.IDEA_REFINING,
    Label.SPEC,
    Label.BACKLOG_GEN,
    Label.PLANNING,
    Label.IMPLEMENTING,
    Label.CI_FIX,
    Label.ADDRESSING_REVIEW,
    Label.RETRO,
}

LABEL_COLORS: dict[str, str] = {
    Label.IDEA_TRIAGE.value: "fbca04",
    Label.IDEA_REVIEW.value: "0e8a16",
    Label.IDEA_REFINING.value: "d93f0b",
    Label.SPEC.value: "1d76db",
    Label.SPEC_REVIEW.value: "0e8a16",
    Label.SPEC_APPROVED.value: "0e8a16",
    Label.BACKLOG_GEN.value: "1d76db",
    Label.BACKLOG_REVIEW.value: "0e8a16",
    Label.PLANNING.value: "1d76db",
    Label.PLAN_REVIEW.value: "1d76db",
    Label.PLAN.value: "1d76db",
    Label.IMPLEMENTING.value: "6f42c1",
    Label.PR.value: "6f42c1",
    Label.CI_FIX.value: "d93f0b",
    Label.SELF_REVIEW.value: "6f42c1",
    Label.REVIEW.value: "0e8a16",
    Label.ADDRESSING_REVIEW.value: "d93f0b",
    Label.MERGE_READY.value: "0e8a16",
    Label.RETRO.value: "bfd4f2",
    Label.STUCK.value: "e4e669",
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
class IdeaPR:
    """A GitHub pull request containing raw idea files."""

    owner: str
    repo: str
    pr_number: int
    branch: str
    labels: set[str]
    title: str = ""
    project_name: str = ""
    linked_issue_number: int | None = None

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def pr_ref(self) -> str:
        return f"{self.full_repo}#{self.pr_number}"

    @property
    def idea_label(self) -> Label | None:
        for label in (Label.IDEA_TRIAGE, Label.IDEA_REVIEW, Label.IDEA_REFINING):
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
