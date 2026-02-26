"""Bootstrap the engineering repository with scaffolding and auto-discovered architecture docs."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import structlog

from ottonate.config import OttonateConfig
from ottonate.github import GitHubClient
from ottonate.pipeline import run_agent

log = structlog.get_logger()

_DEFAULT_CONFIG_YML = """\
# Ottonate pipeline configuration for this organization.
# Values here override built-in defaults. Repo-specific rules
# can be added as sections in rules.md below.
branch_pattern: "otto/{issue_number}/{slug}"
commit_format: "{issue_ref} {summary}"
"""

_DEFAULT_RULES_MD = """\
# Organization Rules

These rules are injected into every ottonate agent prompt. Add coding
conventions, architectural constraints, and repo-specific guidance below.

## General

- Follow existing code style in each repository.
- Write tests for new functionality.
- Keep PRs focused and small.
"""

_OVERVIEW_TEMPLATE = """\
# Architecture Overview

This document describes the high-level architecture of the organization's systems.

<!-- This file will be auto-populated by ottonate during init. -->
"""

_REPOS_TEMPLATE = """\
# Repository Catalog

| Repository | Description | Tech Stack | Dependencies |
|-----------|-------------|------------|--------------|

<!-- This file will be auto-populated by ottonate during init. -->
"""

_SCAN_PROMPT = """\
You are initializing an engineering repository for an organization.

Your task: scan the organization's repositories and populate the architecture docs.

Steps:
1. Run `gh repo list {org} --limit 100 --json name,description,primaryLanguage,isArchived` \
to discover repositories.
2. Filter out archived repos.
3. For each active repo, look at the primary language and description.
4. Write `architecture/overview.md` with a high-level summary of the organization's systems, \
grouping repos by domain/function.
5. Write `architecture/repos.md` as a table with columns: Repository, Description, Tech Stack, \
Dependencies (infer what you can from the repo metadata).

Important:
- Keep the overview concise and factual.
- Don't fabricate details you can't infer from the repo metadata.
- Use markdown formatting.
- Output [INIT_COMPLETE] when done.
"""


async def _git(work_dir: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(work_dir),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode().strip()
        log.warning("git_command_failed", args=args, stderr=msg)
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
    return stdout.decode().strip()


async def init_engineering(config: OttonateConfig, github: GitHubClient) -> str:
    """Bootstrap the engineering repo with scaffolding and agent-discovered docs.

    Returns the PR URL or number.
    """
    org = config.github_org
    repo = config.github_engineering_repo
    full_repo = f"{org}/{repo}"

    with tempfile.TemporaryDirectory(prefix="ottonate-init-") as tmp:
        work_dir = Path(tmp) / repo

        clone_proc = await asyncio.create_subprocess_exec(
            "gh",
            "repo",
            "clone",
            full_repo,
            str(work_dir),
            "--",
            "--depth=1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await clone_proc.communicate()

        _scaffold(work_dir)

        branch = "otto/init-engineering"
        await _git(work_dir, "checkout", "-b", branch)
        await _git(work_dir, "add", "-A")

        has_scaffold_changes = True
        try:
            await _git(work_dir, "diff-index", "--quiet", "HEAD", "--")
            has_scaffold_changes = False
        except RuntimeError:
            pass

        if has_scaffold_changes:
            await _git(work_dir, "commit", "-m", "chore: scaffold engineering repo")

        await _git(work_dir, "push", "-u", "origin", branch)

        prompt = _SCAN_PROMPT.format(org=org)
        log.info("init_engineering_scan_start", org=org)
        result = await run_agent("otto-planner", prompt, str(work_dir), config=config)

        if result.is_error:
            log.error("init_engineering_scan_failed", error=result.text[:500])

        await _git(work_dir, "add", "-A")

        has_changes = True
        try:
            await _git(work_dir, "diff-index", "--quiet", "HEAD", "--")
            has_changes = False
        except RuntimeError:
            pass

        if has_changes:
            await _git(
                work_dir,
                "commit",
                "-m",
                "chore: auto-populate architecture docs from org scan",
            )
            await _git(work_dir, "push")

        pr_number = await github.create_pr(
            org,
            repo,
            branch,
            title="Initialize engineering repository",
            body=(
                "Scaffolds the engineering repo with:\n"
                "- `architecture/` docs (auto-populated from org scan)\n"
                "- `specs/` and `decisions/` directories\n"
                "- `.ottonate/config.yml` and `.ottonate/rules.md` defaults\n"
            ),
        )

        pr_url = f"https://github.com/{org}/{repo}/pull/{pr_number}"
        log.info("init_engineering_done", pr_url=pr_url)
        return pr_url


def _scaffold(work_dir: Path) -> None:
    dirs = [
        work_dir / "architecture",
        work_dir / "specs",
        work_dir / "decisions",
        work_dir / ".ottonate",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    files = {
        work_dir / "architecture" / "overview.md": _OVERVIEW_TEMPLATE,
        work_dir / "architecture" / "repos.md": _REPOS_TEMPLATE,
        work_dir / "specs" / ".gitkeep": "",
        work_dir / "decisions" / ".gitkeep": "",
        work_dir / ".ottonate" / "config.yml": _DEFAULT_CONFIG_YML,
        work_dir / ".ottonate" / "rules.md": _DEFAULT_RULES_MD,
    }
    for path, content in files.items():
        if not path.exists():
            path.write_text(content)
