"""HTML page routes for the ottonate dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


async def _build_phases(request: Request) -> tuple[dict[str, list[dict]], str]:
    config = request.app.state.config
    github = request.app.state.github

    from .api import _classify_issue

    raw = await github.search_issues(config.github_org, config.github_agent_label)
    phases: dict[str, list[dict]] = {
        "planning": [],
        "implementing": [],
        "awaiting_human": [],
        "stuck": [],
    }
    for item in raw:
        classified = _classify_issue(item, config.github_agent_label)
        if classified:
            classified["github_url"] = (
                f"https://github.com/{config.github_org}/{classified['repo']}"
                f"/issues/{classified['number']}"
            )
            phase = classified["phase"]
            if phase in phases:
                phases[phase].append(classified)

    return phases, config.github_org


async def _build_sections(request: Request) -> tuple[dict[str, list[dict]], str]:
    config = request.app.state.config
    github = request.app.state.github

    from .api import ATTENTION_PRIORITY, HUMAN_GATE_LABELS, _classify_issue

    raw = await github.search_issues(config.github_org, config.github_agent_label)
    items = []
    for issue in raw:
        classified = _classify_issue(issue, config.github_agent_label)
        if classified and classified["stage"] in HUMAN_GATE_LABELS:
            classified["priority"] = ATTENTION_PRIORITY.get(classified["stage"], 99)
            classified["github_url"] = (
                f"https://github.com/{config.github_org}/{classified['repo']}"
                f"/issues/{classified['number']}"
            )
            items.append(classified)
    items.sort(key=lambda x: x["priority"])

    sections = {
        "stuck": [i for i in items if i["stage"] == "agentStuck"],
        "merge": [i for i in items if i["stage"] == "agentMergeReady"],
        "review": [i for i in items if i["stage"] == "agentReview"],
        "approval": [i for i in items if i["stage"] in ("agentSpecReview", "agentBacklogReview")],
    }

    return sections, config.github_org


@router.get("/", response_class=HTMLResponse)
async def pipeline_board(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    phases, org = await _build_phases(request)
    return templates.TemplateResponse(request, "pipeline.html", {"phases": phases, "org": org})


@router.get("/attention", response_class=HTMLResponse)
async def attention_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    sections, org = await _build_sections(request)
    return templates.TemplateResponse(request, "attention.html", {"sections": sections, "org": org})


@router.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request, days: int | None = None) -> HTMLResponse:
    templates = request.app.state.templates
    store = request.app.state.metrics

    throughput = await store.get_throughput_stats(days)
    stage_stats = await store.get_stage_stats(days)
    completions = await store.get_recent_completions(days)

    return templates.TemplateResponse(
        request,
        "metrics.html",
        {
            "throughput": throughput,
            "stage_stats": stage_stats,
            "completions": completions[:20],
            "days": days,
        },
    )


@router.get("/partials/board", response_class=HTMLResponse)
async def partial_board(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    phases, org = await _build_phases(request)
    return templates.TemplateResponse(
        request, "partials/_board.html", {"phases": phases, "org": org}
    )


@router.get("/partials/queue", response_class=HTMLResponse)
async def partial_queue(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    sections, org = await _build_sections(request)
    return templates.TemplateResponse(
        request, "partials/_queue.html", {"sections": sections, "org": org}
    )


@router.get("/partials/stats", response_class=HTMLResponse)
async def partial_stats(request: Request, days: int | None = None) -> HTMLResponse:
    templates = request.app.state.templates
    store = request.app.state.metrics

    throughput = await store.get_throughput_stats(days)
    stage_stats = await store.get_stage_stats(days)
    completions = await store.get_recent_completions(days)

    return templates.TemplateResponse(
        request,
        "partials/_stats.html",
        {
            "throughput": throughput,
            "stage_stats": stage_stats,
            "completions": completions[:20],
            "days": days,
        },
    )
