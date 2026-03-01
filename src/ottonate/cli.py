"""CLI entry points for ottonate."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

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


@main.command("process-idea")
@click.argument("pr_ref")
def process_idea(pr_ref: str) -> None:
    """Manually process an idea PR through the idea pipeline.

    PR_REF should be in the format owner/repo#number (e.g. smereddy/engineering#1).
    """
    from ottonate.agents import sync_agent_definitions
    from ottonate.github import GitHubClient
    from ottonate.models import IdeaPR
    from ottonate.pipeline import Pipeline
    from ottonate.rules import load_rules

    sync_agent_definitions()
    owner, repo, number = _parse_issue_ref(pr_ref)
    config = _get_config()
    github = GitHubClient()

    async def _process() -> None:
        # Fetch PR details to get branch, labels, title
        details = await github.get_pr_details(owner, repo, number)
        if not details:
            click.echo(f"Could not find PR #{number} in {owner}/{repo}")
            return

        pr_labels = {
            lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
            for lbl in details.get("labels", [])
        }

        # Get PR files to extract project name
        pr_files = await github.get_pr_files(owner, repo, number)
        ideas_dir = config.ideas_dir
        project_name = ""
        prefix = f"{ideas_dir}/"
        for f in pr_files:
            filename = f.get("filename", "")
            if filename.startswith(prefix):
                rest = filename[len(prefix) :]
                parts = rest.split("/")
                if parts and parts[0]:
                    project_name = parts[0]
                    break

        if not project_name:
            click.echo(f"No idea files found in {ideas_dir}/ for PR #{number}")
            return

        idea_pr = IdeaPR(
            owner=owner,
            repo=repo,
            pr_number=number,
            branch=details.get("headRefName", ""),
            labels=pr_labels,
            title=details.get("title", ""),
            project_name=project_name,
        )

        click.echo(f"Processing idea PR: {idea_pr.pr_ref} (project: {project_name})")

        pipeline = Pipeline(config, github)
        rules = await load_rules(owner, repo, config, github)
        await pipeline.handle_idea_pr(idea_pr, rules)
        click.echo("Done.")

    asyncio.run(_process())


@main.command()
def setup() -> None:
    """Interactive onboarding: configure .env, create labels, scaffold engineering repo."""
    from ottonate.agents import sync_agent_definitions
    from ottonate.github import GitHubClient
    from ottonate.setup import (
        SetupResult,
        create_repo,
        detect_gh_user,
        ensure_labels,
        init_empty_repo,
        list_user_orgs,
        repo_exists,
        repo_is_empty,
        write_env_file,
    )

    github = GitHubClient()
    result = SetupResult()
    env_path = Path(".env")

    async def _setup() -> None:
        # Step 1: Detect GitHub auth
        click.echo("\n--- Ottonate Setup ---\n")
        username = await detect_gh_user(github)
        if not username:
            click.echo("Error: Not authenticated with gh CLI. Run 'gh auth login' first.")
            return
        click.echo(f"Authenticated as: {username}")
        result.add("GitHub auth", "OK")

        # Step 2: Choose org
        orgs = await list_user_orgs(github)
        choices = [username] + [o for o in orgs if o != username]
        click.echo("\nAvailable owners:")
        for i, c in enumerate(choices, 1):
            click.echo(f"  {i}. {c}")
        pick = click.prompt("Select owner for engineering repo", type=int, default=1)
        org = choices[min(pick, len(choices)) - 1]
        click.echo(f"Using: {org}")

        # Step 3: Engineering repo
        eng_repo = click.prompt("Engineering repo name", default="engineering")
        exists = await repo_exists(org, eng_repo)
        if not exists:
            if click.confirm(f"Repo {org}/{eng_repo} does not exist. Create it?", default=True):
                ok = await create_repo(org, eng_repo)
                if ok:
                    result.add(f"Create {org}/{eng_repo}", "OK")
                else:
                    click.echo("Failed to create repo. Create it manually and re-run setup.")
                    return
            else:
                click.echo("Setup cancelled. Create the repo and re-run.")
                return

        # Step 4: Initialize empty repo
        if await repo_is_empty(org, eng_repo):
            click.echo(f"Repo {org}/{eng_repo} is empty, initializing scaffold...")
            ok = await init_empty_repo(org, eng_repo)
            if ok:
                result.add("Scaffold engineering repo", "OK")
            else:
                click.echo("Failed to initialize repo.")
                return
        else:
            result.add("Engineering repo", "exists")

        # Step 5: Entry label
        entry_label = click.prompt("Entry label for agent issues", default="otto")

        # Step 6: Write .env
        if env_path.exists():
            if not click.confirm(".env already exists. Overwrite?", default=False):
                click.echo("Keeping existing .env")
                result.add(".env file", "kept")
            else:
                write_env_file(
                    env_path,
                    org=org,
                    repo=eng_repo,
                    username=username,
                    entry_label=entry_label,
                )
                result.add(".env file", "written")
        else:
            write_env_file(
                env_path,
                org=org,
                repo=eng_repo,
                username=username,
                entry_label=entry_label,
            )
            result.add(".env file", "written")

        # Step 7: Ensure labels
        click.echo(f"\nCreating pipeline labels in {org}/{eng_repo}...")
        count = await ensure_labels(github, org, eng_repo, entry_label)
        result.add("Pipeline labels", f"{count} created" if count else "all exist")

        # Step 8: Sync agents
        click.echo("Syncing agent definitions...")
        updated = sync_agent_definitions()
        result.add("Agent definitions", f"{len(updated)} synced" if updated else "up to date")

        # Summary
        click.echo("\n--- Setup Complete ---\n")
        click.echo(result.summary())
        click.echo("\nNext steps:")
        click.echo("  ottonate run            # Start the scheduler daemon")
        click.echo("  ottonate init-engineering  # Auto-populate architecture docs")
        click.echo("  ottonate rules-check    # Debug merged rules for a repo")
        click.echo()

    asyncio.run(_setup())


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
