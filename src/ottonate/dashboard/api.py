"""JSON API endpoints for the ottonate dashboard."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ottonate.models import Label

router = APIRouter()

PHASE_MAP: dict[str, str] = {
    Label.SPEC.value: "planning",
    Label.PLANNING.value: "planning",
    Label.PLAN_REVIEW.value: "planning",
    Label.PLAN.value: "planning",
    Label.SPEC_APPROVED.value: "implementing",
    Label.BACKLOG_GEN.value: "implementing",
    Label.IMPLEMENTING.value: "implementing",
    Label.PR.value: "implementing",
    Label.CI_FIX.value: "implementing",
    Label.SELF_REVIEW.value: "implementing",
    Label.ADDRESSING_REVIEW.value: "implementing",
    Label.RETRO.value: "implementing",
    Label.SPEC_REVIEW.value: "awaiting_human",
    Label.BACKLOG_REVIEW.value: "awaiting_human",
    Label.REVIEW.value: "awaiting_human",
    Label.MERGE_READY.value: "awaiting_human",
    Label.STUCK.value: "stuck",
}

HUMAN_GATE_LABELS = {
    Label.SPEC_REVIEW.value,
    Label.BACKLOG_REVIEW.value,
    Label.REVIEW.value,
    Label.MERGE_READY.value,
    Label.STUCK.value,
}

ATTENTION_PRIORITY = {
    Label.STUCK.value: 0,
    Label.MERGE_READY.value: 1,
    Label.REVIEW.value: 2,
    Label.SPEC_REVIEW.value: 3,
    Label.BACKLOG_REVIEW.value: 3,
}


def _get_stage_label(labels: list[dict | str]) -> str | None:
    label_names = {(lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)) for lbl in labels}
    for stage_label in Label:
        if stage_label.value in label_names:
            return stage_label.value
    return None


def _classify_issue(issue: dict, entry_label: str) -> dict | None:
    labels = issue.get("labels", [])
    stage = _get_stage_label(labels)
    if stage is None:
        return None

    repo_data = issue.get("repository", {})
    repo_name = repo_data.get("name", "") if isinstance(repo_data, dict) else str(repo_data)
    number = issue.get("number", 0)
    title = issue.get("title", "")

    return {
        "repo": repo_name,
        "number": number,
        "title": title,
        "stage": stage,
        "phase": PHASE_MAP.get(stage, "unknown"),
        "labels": [(lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)) for lbl in labels],
    }


@router.get("/issues")
async def list_issues(request: Request) -> list[dict]:
    config = request.app.state.config
    github = request.app.state.github
    raw = await github.search_issues(config.github_org, config.github_agent_label)
    issues = []
    for item in raw:
        classified = _classify_issue(item, config.github_agent_label)
        if classified:
            issues.append(classified)
    return issues


@router.get("/attention")
async def attention_queue(request: Request) -> list[dict]:
    config = request.app.state.config
    github = request.app.state.github
    raw = await github.search_issues(config.github_org, config.github_agent_label)
    items = []
    for issue in raw:
        classified = _classify_issue(issue, config.github_agent_label)
        if classified and classified["stage"] in HUMAN_GATE_LABELS:
            classified["priority"] = ATTENTION_PRIORITY.get(classified["stage"], 99)
            items.append(classified)
    items.sort(key=lambda x: x["priority"])
    return items


class UnstickRequest(BaseModel):
    target_stage: str


@router.post("/issues/{owner}/{repo}/{number}/unstick")
async def unstick_issue(
    owner: str, repo: str, number: int, body: UnstickRequest, request: Request
) -> dict:
    github = request.app.state.github
    try:
        target = Label(body.target_stage)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {body.target_stage}")
    await github.swap_label(owner, repo, number, Label.STUCK, target)
    return {"status": "ok", "new_stage": target.value}


@router.post("/issues/{owner}/{repo}/{number}/approve")
async def approve_issue(owner: str, repo: str, number: int, request: Request) -> dict:
    github = request.app.state.github
    await github.add_comment(owner, repo, number, "Approved via ottonate dashboard.")
    return {"status": "ok"}


@router.post("/prs/{owner}/{repo}/{pr_number}/merge")
async def merge_pr(owner: str, repo: str, pr_number: int, request: Request) -> dict:
    github = request.app.state.github
    try:
        await github.merge_pr(owner, repo, pr_number)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok"}
