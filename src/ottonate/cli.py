"""CLI entry points for ottonate."""

from __future__ import annotations

import asyncio
import re

import click
import structlog

from ottonate.config import OttonateConfig
from ottonate.scheduler import Scheduler

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)


def _get_config() -> OttonateConfig:
    return OttonateConfig()


def _parse_issue_ref(ref: str) -> tuple[str, str, int]:
    """Parse 'owner/repo#number' into (owner, repo, number)."""
    match = re.match(r"^([^/]+)/([^#]+)#(\d+)$", ref)
    if not match:
        raise click.BadParameter(
            f"Invalid issue reference: {ref}. Expected format: owner/repo#number"
        )
    return match.group(1), match.group(2), int(match.group(3))


@click.group()
def main() -> None:
    """Ottonate: Automated GitHub issue-to-PR pipeline."""


@main.command()
def run() -> None:
    """Start the scheduler daemon."""
    from ottonate.agents import sync_agent_definitions

    sync_agent_definitions()
    config = _get_config()
    scheduler = Scheduler(config)
    try:
        asyncio.run(scheduler.start())
    except KeyboardInterrupt:
        click.echo("Shutting down...")


@main.command()
@click.argument("issue_ref")
def process(issue_ref: str) -> None:
    """Manually push a single issue through one pipeline step.

    ISSUE_REF should be in the format owner/repo#number (e.g. appfire/flow-api#42).
    """
    from ottonate.agents import sync_agent_definitions

    sync_agent_definitions()
    owner, repo, number = _parse_issue_ref(issue_ref)
    config = _get_config()
    scheduler = Scheduler(config)
    asyncio.run(scheduler.process_single(owner, repo, number))


@main.command("sync-agents")
def sync_agents() -> None:
    """Sync agent definitions from repo to ~/.claude/agents/."""
    from ottonate.agents import sync_agent_definitions

    updated = sync_agent_definitions()
    for name in updated:
        click.echo(f"Updated {name}")
    if not updated:
        click.echo("All agent definitions are up to date.")


@main.command("init-engineering")
def init_engineering_cmd() -> None:
    """Bootstrap the engineering repo with scaffolding and auto-discovered architecture docs."""
    from ottonate.agents import sync_agent_definitions
    from ottonate.github import GitHubClient
    from ottonate.init_engineering import init_engineering

    sync_agent_definitions()
    config = _get_config()
    github = GitHubClient()
    pr_url = asyncio.run(init_engineering(config, github))
    click.echo(f"PR created: {pr_url}")


@main.command()
@click.option("--port", default=8080, help="Port to bind the dashboard server to.")
def dashboard(port: int) -> None:
    """Start the ottonate dashboard web UI."""
    import uvicorn

    from ottonate.dashboard.app import create_app

    config = _get_config()
    app = create_app(config)
    click.echo(f"Starting dashboard at http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


@main.command("rules-check")
@click.argument("repo_ref")
def rules_check(repo_ref: str) -> None:
    """Load and display merged rules for a given repo.

    REPO_REF should be in the format owner/repo (e.g. appfire/flow-api).
    """
    from ottonate.github import GitHubClient
    from ottonate.rules import load_rules

    parts = repo_ref.split("/", 1)
    if len(parts) != 2:
        raise click.BadParameter("Expected format: owner/repo")
    owner, repo = parts

    config = _get_config()
    github = GitHubClient()

    async def _check() -> None:
        rules = await load_rules(owner, repo, config, github)
        click.echo(f"Branch pattern: {rules.branch_pattern}")
        click.echo(f"Commit format:  {rules.commit_format}")
        click.echo(f"Notify team:    {rules.notify_team or '(none)'}")
        click.echo(f"Entry label:    {rules.entry_label}")
        click.echo(f"Reviewers:      {rules.required_reviewers}")
        click.echo(f"Repo catalog:   {len(rules.repo_catalog)} repos")
        if rules.agent_context:
            click.echo(f"\n--- Agent Context ({len(rules.agent_context)} chars) ---")
            click.echo(rules.agent_context[:2000])
            if len(rules.agent_context) > 2000:
                click.echo("... (truncated)")

    asyncio.run(_check())
