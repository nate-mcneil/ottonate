"""Interactive onboarding setup for ottonate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog

from ottonate.config import OttonateConfig
from ottonate.github import GitHubClient

log = structlog.get_logger()

# All pipeline labels with their hex colors (without #).
PIPELINE_LABEL_COLORS: dict[str, str] = {
    "agentIdeaTriage": "fbca04",
    "agentIdeaReview": "0e8a16",
    "agentIdeaRefining": "d93f0b",
    "agentSpec": "1d76db",
    "agentSpecReview": "0e8a16",
    "agentSpecApproved": "0e8a16",
    "agentBacklogGen": "1d76db",
    "agentBacklogReview": "0e8a16",
    "agentPlanning": "1d76db",
    "agentPlanReview": "1d76db",
    "agentPlan": "1d76db",
    "agentImplementing": "6f42c1",
    "agentPR": "6f42c1",
    "agentCIFix": "d93f0b",
    "agentSelfReview": "6f42c1",
    "agentReview": "0e8a16",
    "agentAddressingReview": "d93f0b",
    "agentMergeReady": "0e8a16",
    "agentRetro": "bfd4f2",
    "agentStuck": "e4e669",
}


async def detect_gh_user(github: GitHubClient) -> str:
    """Get the authenticated GitHub username."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", "user", "--jq", ".login",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip() if proc.returncode == 0 else ""


async def list_user_orgs(github: GitHubClient) -> list[str]:
    """List orgs the authenticated user belongs to."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", "user/orgs", "--jq", ".[].login",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0 or not stdout.decode().strip():
        return []
    return stdout.decode().strip().split("\n")


async def repo_exists(owner: str, repo: str) -> bool:
    """Check if a GitHub repo exists and is accessible."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "repo", "view", f"{owner}/{repo}", "--json", "name",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0


async def repo_is_empty(owner: str, repo: str) -> bool:
    """Check if a repo has no commits."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", f"repos/{owner}/{repo}/contents/",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode != 0


async def create_repo(owner: str, repo: str) -> bool:
    """Create a new repo under the given owner."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "repo", "create", f"{owner}/{repo}", "--private", "--description",
        "Engineering repository managed by ottonate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("repo_create_failed", stderr=stderr.decode())
        return False
    return True


async def ensure_labels(github: GitHubClient, owner: str, repo: str, entry_label: str) -> int:
    """Ensure all pipeline labels exist in the repo. Returns count of labels created."""
    all_labels = dict(PIPELINE_LABEL_COLORS)
    all_labels[entry_label] = "6f42c1"
    created = await github.ensure_labels(owner, repo, all_labels)
    return len(created)


async def init_empty_repo(owner: str, repo: str) -> bool:
    """Push an initial commit to an empty repo with basic scaffold."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="ottonate-setup-") as tmp:
        work = Path(tmp) / repo
        work.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "ottonate@setup"],
            ["git", "config", "user.name", "ottonate"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(work),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        (work / "README.md").write_text(f"# {repo}\n\nManaged by ottonate.\n")
        for d in ["ideas", "specs", "decisions", "architecture", ".ottonate"]:
            (work / d).mkdir(exist_ok=True)
            (work / d / ".gitkeep").touch()

        for cmd in [
            ["git", "add", "-A"],
            ["git", "commit", "-m", "Initial scaffold"],
            ["git", "branch", "-M", "main"],
            ["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"],
            ["git", "push", "-u", "origin", "main"],
        ]:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(work),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning("init_repo_failed", cmd=cmd, stderr=stderr.decode())
                return False
    return True


def write_env_file(
    env_path: Path,
    *,
    org: str,
    repo: str,
    username: str,
    entry_label: str = "otto",
) -> None:
    """Write a .env file with the core ottonate configuration."""
    lines = [
        f"OTTONATE_GITHUB_ORG={org}",
        f"OTTONATE_GITHUB_ENGINEERING_REPO={repo}",
        f"OTTONATE_GITHUB_USERNAME={username}",
        f"OTTONATE_GITHUB_AGENT_LABEL={entry_label}",
        "",
    ]
    env_path.write_text("\n".join(lines))


class SetupResult:
    """Collects results from the setup process for display."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, str]] = []

    def add(self, step: str, status: str) -> None:
        self.steps.append((step, status))

    def summary(self) -> str:
        lines = []
        for step, status in self.steps:
            lines.append(f"  {status} {step}")
        return "\n".join(lines)
