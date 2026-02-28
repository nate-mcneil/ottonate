"""Prompt builders for each pipeline stage."""

from __future__ import annotations

from ottonate.metrics import IssueMetrics
from ottonate.models import IdeaPR, ReviewComment, Ticket


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
Break this specification into small, atomic implementation stories (GitHub issues).

CRITICAL: Your output must be ONLY a JSON array. Do NOT write files. Do NOT produce markdown.
Do NOT produce a development plan. Output raw JSON to stdout and nothing else.

Each story object must have these keys:
- "title": Short issue title
- "repo": Target GitHub repository name (e.g. "flow-tickets-delivery")
- "description": Issue body with acceptance criteria
- "estimate": "S", "M", or "L"
- "dependencies": Array of story titles this depends on (empty array if none)
- "notes": Technical implementation notes

Example format:
```json
[
  {{"title": "Migrate Avatar component to FDSE", "repo": "flow-tickets-delivery", \
"description": "...", "estimate": "M", "dependencies": [], "notes": "..."}}
]
```

End your response with [BACKLOG_COMPLETE] after the JSON array.
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


def retro_prompt(
    ticket: Ticket,
    plan: str,
    metrics: IssueMetrics,
    comments: list[dict],
    *,
    rules_context: str = "",
) -> str:
    rules = _rules_section(rules_context)

    stage_lines = []
    for s in metrics.stages:
        line = f"- **{s['stage']}** (agent: {s.get('agent', 'n/a')})"
        if s.get("retry_number", 0) > 0:
            line += f" -- retry #{s['retry_number']}"
        if s.get("was_stuck"):
            line += f" -- STUCK: {s.get('stuck_reason', 'unknown')}"
        if s.get("is_error"):
            line += " -- ERROR"
        stage_lines.append(line)

    stage_detail = "\n".join(stage_lines) if stage_lines else "No stage data recorded."

    comment_lines = (
        "\n".join(f"- @{c.get('author', 'unknown')}: {c.get('body', '')[:200]}" for c in comments)
        if comments
        else "No review comments."
    )

    return f"""## Retrospective: {ticket.issue_ref}

### Issue Summary
{ticket.summary}

### Development Plan
{plan or "No plan recorded."}

### Pipeline Metrics
- Total stages: {metrics.total_stages}
- Total retries: {metrics.total_retries}
- Total cost: ${metrics.total_cost_usd:.2f}
- Was stuck: {metrics.was_stuck}
- Stuck reasons: {", ".join(metrics.stuck_reasons) or "none"}

### Stage Detail
{stage_detail}

### Review Comments Received
{comment_lines}
{rules}
Analyze what went wrong and propose improvements to the engineering repo.
"""


# -- Idea pipeline (Step 0) --


def idea_triage_prompt(
    idea_pr: IdeaPR, file_contents: dict[str, str], *, rules_context: str = ""
) -> str:
    rules = _rules_section(rules_context)
    files_section = "\n\n".join(
        f"### File: `{name}`\n```\n{content}\n```" for name, content in file_contents.items()
    )
    return f"""## Idea PR: {idea_pr.pr_ref}

### Project Name
{idea_pr.project_name}

### Source Files
{files_section}
{rules}
Synthesize these idea files into a structured INTENT.md document.

Write the file `ideas/{idea_pr.project_name}/INTENT.md` with these sections:
- **Problem Statement**: What problem does this idea solve?
- **Proposed Solution**: High-level approach
- **Key Requirements**: Numbered list of must-haves
- **Technical Considerations**: Architecture, constraints, trade-offs
- **Open Questions**: Anything that needs human input
- **Source Files**: List of original idea files used

After writing INTENT.md, output a JSON object on its own line with the GitHub issue content:
{{"title": "short issue title", "body": "issue body in markdown"}}

End with `[IDEA_COMPLETE]` if you produced a full intent document.
End with `[IDEA_NEEDS_INPUT]` if critical information is missing.
"""


def idea_refine_prompt(
    idea_pr: IdeaPR,
    current_intent: str,
    new_comments: list[str],
    *,
    rules_context: str = "",
) -> str:
    rules = _rules_section(rules_context)
    comments_section = "\n\n".join(
        f"**Comment {i + 1}:**\n{comment}" for i, comment in enumerate(new_comments)
    )
    return f"""## Idea PR: {idea_pr.pr_ref} (Refinement)

### Project Name
{idea_pr.project_name}

### Current INTENT.md
{current_intent}

### New Human Comments
{comments_section}
{rules}
Update the INTENT.md based on the human feedback above.

Write the updated file to `ideas/{idea_pr.project_name}/INTENT.md`.

After writing the updated INTENT.md, output a JSON object on its own line with the updated GitHub issue content:
{{"title": "short issue title", "body": "updated issue body in markdown"}}

End with `[REFINE_COMPLETE]` when done.
"""
