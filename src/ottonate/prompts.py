"""Prompt builders for each pipeline stage."""

from __future__ import annotations

from ottonate.models import ReviewComment, Ticket


def _rules_section(rules_context: str) -> str:
    if not rules_context:
        return ""
    return f"\n### Project Context\n{rules_context}\n"


def spec_prompt(ticket: Ticket, description: str, *, rules_context: str = "") -> str:
    rules = _rules_section(rules_context)
    return f"""## Initiative: {ticket.issue_ref}

### Description
{description}

### Repository
{ticket.full_repo}
{rules}
Generate a comprehensive product specification for this initiative. Write the spec to SPEC.md.
"""


def backlog_prompt(ticket: Ticket, spec_body: str, *, rules_context: str = "") -> str:
    rules = _rules_section(rules_context)
    return f"""## Initiative: {ticket.issue_ref}

### Approved Specification
{spec_body}
{rules}
Break this specification into implementation stories. For each story produce:
- Title
- Target repository (from the repo catalog above)
- Description with acceptance criteria
- Estimation (S/M/L)
- Dependencies on other stories
- Technical notes

Output as a JSON array of story objects with keys: title, repo, description, estimate, \
dependencies, notes.
End with [BACKLOG_COMPLETE] when done.
"""


def planner_prompt(ticket: Ticket, description: str, *, rules_context: str = "") -> str:
    rules = _rules_section(rules_context)
    return f"""## Issue: {ticket.issue_ref}

### Description
{description}

### Repository
{ticket.full_repo}
{rules}
Analyze the codebase and produce a development plan for this issue.
"""


def quality_gate_prompt(ticket: Ticket, plan: str, description: str) -> str:
    return f"""## Issue: {ticket.issue_ref}

### Issue Description
{description}

### Development Plan to Evaluate
{plan}

Evaluate this plan and respond with JSON.
"""


def implementer_prompt(
    ticket: Ticket,
    plan: str,
    branch_name: str,
    *,
    rules_context: str = "",
) -> str:
    rules = _rules_section(rules_context)
    return f"""## Issue: {ticket.issue_ref}

### Branch
Create branch: `{branch_name}` from the default branch.

### Development Plan
{plan}
{rules}
Implement this plan following TDD. Create the PR when done.
"""


def ci_fixer_prompt(ticket: Ticket, failure_logs: str) -> str:
    return f"""## Issue: {ticket.issue_ref}
## PR: #{ticket.pr_number}
## Repo: {ticket.full_repo}

### CI Failure Logs
{failure_logs}

Fix the CI failures and push.
"""


def reviewer_prompt(ticket: Ticket, plan: str, diff: str) -> str:
    return f"""## Issue: {ticket.issue_ref}
## PR: #{ticket.pr_number}
## Repo: {ticket.full_repo}

### Original Plan
{plan}

### PR Diff
{diff}

Review this PR against the plan.
"""


def review_responder_prompt(
    ticket: Ticket, comments: list[ReviewComment], repo_owner: str, repo_name: str
) -> str:
    comments_text = "\n\n".join(
        f"### Comment #{c.id} by @{c.author}\nFile: {c.path or 'general'}:{c.line or ''}\n{c.body}"
        for c in comments
    )
    return f"""## Issue: {ticket.issue_ref}
## PR: #{ticket.pr_number}
## Repo: {repo_owner}/{repo_name}

### Review Comments to Address
{comments_text}

Address each comment. Reply inline using gh api.
"""
