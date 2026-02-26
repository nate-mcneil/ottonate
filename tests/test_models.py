from __future__ import annotations

from ottonate.models import (
    ACTIONABLE_LABELS,
    IN_PROGRESS_LABELS,
    STAGE_LABELS,
    Label,
    Ticket,
)


class TestLabel:
    def test_all_labels_in_stage_labels(self):
        for label in Label:
            assert label in STAGE_LABELS

    def test_actionable_labels_subset_of_stage(self):
        assert ACTIONABLE_LABELS.issubset(STAGE_LABELS)

    def test_in_progress_labels_subset_of_stage(self):
        assert IN_PROGRESS_LABELS.issubset(STAGE_LABELS)

    def test_actionable_and_in_progress_disjoint(self):
        assert ACTIONABLE_LABELS.isdisjoint(IN_PROGRESS_LABELS)


class TestTicket:
    def test_agent_label_none_when_no_stage(self, sample_ticket: Ticket):
        assert sample_ticket.agent_label is None

    def test_agent_label_returns_stage(self):
        ticket = Ticket(
            owner="org",
            repo="repo",
            issue_number=1,
            labels={"otto", "agentPlan"},
        )
        assert ticket.agent_label == Label.PLAN

    def test_agent_label_first_match(self):
        ticket = Ticket(
            owner="org",
            repo="repo",
            issue_number=1,
            labels={"otto", "agentPR", "agentReview"},
        )
        assert ticket.agent_label is not None

    def test_full_repo(self):
        ticket = Ticket(owner="org", repo="my-app", issue_number=42, labels=set())
        assert ticket.full_repo == "org/my-app"

    def test_issue_ref(self):
        ticket = Ticket(owner="org", repo="my-app", issue_number=42, labels=set())
        assert ticket.issue_ref == "org/my-app#42"
