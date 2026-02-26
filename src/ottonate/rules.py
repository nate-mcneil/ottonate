"""Three-layer rules system for contextual agent configuration.

Layers (most specific wins):
1. Built-in defaults
2. Org-level: .ottonate/config.yml + .ottonate/rules.md + architecture/ from engineering repo
3. Repo-level: .ottonate/config.yml + .ottonate/rules.md from the target repo
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
import yaml

if TYPE_CHECKING:
    from ottonate.config import OttonateConfig
    from ottonate.github import GitHubClient

log = structlog.get_logger()

DEFAULT_CONFIG: dict = {
    "branch_pattern": "{issue_number}/{description}",
    "commit_format": "#{issue_number} - {description}",
    "notify_team": "",
    "required_reviewers": {"default": []},
    "labels": {"entry": "otto"},
}

DEFAULT_RULES = ""


@dataclass
class ResolvedRules:
    """Merged rules from all layers for a specific repo context."""

    branch_pattern: str = "{issue_number}/{description}"
    commit_format: str = "#{issue_number} - {description}"
    notify_team: str = ""
    required_reviewers: dict[str, list[str]] = field(default_factory=dict)
    entry_label: str = "otto"
    agent_context: str = ""
    architecture_context: str = ""
    repo_catalog: list[dict] = field(default_factory=list)


async def load_rules(
    owner: str,
    repo: str,
    config: OttonateConfig,
    github: GitHubClient,
) -> ResolvedRules:
    """Load and merge rules from all three layers."""
    merged_config = dict(DEFAULT_CONFIG)
    org_rules_md = DEFAULT_RULES
    ref = config.github_default_branch

    eng_repo = config.github_engineering_repo
    org_config, org_rules_md = await _load_layer(owner, eng_repo, github, ref)
    merged_config = _merge_config(merged_config, org_config)

    arch_context = await _load_org_context(owner, eng_repo, github, ref)

    repo_config: dict = {}
    repo_rules_md = ""
    if repo != eng_repo:
        repo_config, repo_rules_md = await _load_layer(owner, repo, github, ref)
        merged_config = _merge_config(merged_config, repo_config)

    agent_context = _merge_agent_context(org_rules_md, repo_rules_md, arch_context)

    reviewers = merged_config.get("required_reviewers", {})
    if isinstance(reviewers, list):
        reviewers = {"default": reviewers}

    labels = merged_config.get("labels", {})
    entry_label = labels.get("entry", config.github_agent_label)

    repo_catalog = _parse_repo_catalog(arch_context)

    return ResolvedRules(
        branch_pattern=merged_config.get("branch_pattern", DEFAULT_CONFIG["branch_pattern"]),
        commit_format=merged_config.get("commit_format", DEFAULT_CONFIG["commit_format"]),
        notify_team=merged_config.get("notify_team", config.github_notify_team),
        required_reviewers=reviewers,
        entry_label=entry_label,
        agent_context=agent_context,
        architecture_context=arch_context,
        repo_catalog=repo_catalog,
    )


async def _load_layer(owner: str, repo: str, github: GitHubClient, ref: str) -> tuple[dict, str]:
    """Fetch .ottonate/config.yml and .ottonate/rules.md from a repo."""
    config_yml = await github.get_file_content(owner, repo, ".ottonate/config.yml", ref)
    rules_md = await github.get_file_content(owner, repo, ".ottonate/rules.md", ref)

    parsed_config: dict = {}
    if config_yml:
        try:
            parsed_config = yaml.safe_load(config_yml) or {}
        except yaml.YAMLError:
            log.warning("rules_config_parse_error", owner=owner, repo=repo)

    return parsed_config, rules_md or ""


async def _load_org_context(owner: str, eng_repo: str, github: GitHubClient, ref: str) -> str:
    """Load architecture docs from the engineering repo."""
    overview = await github.get_file_content(owner, eng_repo, "architecture/overview.md", ref)
    repos_md = await github.get_file_content(owner, eng_repo, "architecture/repos.md", ref)

    parts: list[str] = []
    if overview:
        parts.append(f"## System Architecture\n\n{overview.strip()}")
    if repos_md:
        parts.append(f"## Repository Catalog\n\n{repos_md.strip()}")
    return "\n\n".join(parts)


async def search_decisions(
    owner: str,
    eng_repo: str,
    keywords: list[str],
    github: GitHubClient,
    ref: str = "main",
) -> str:
    """Search decisions/ for ADRs matching keywords. Returns relevant content."""
    listing = await github.get_file_content(owner, eng_repo, "decisions/", ref)
    if not listing:
        return ""

    matched_parts: list[str] = []
    files = re.findall(r"(\d{3}-.+\.md)", listing)
    kw_lower = [k.lower() for k in keywords]

    for filename in files:
        if any(kw in filename.lower() for kw in kw_lower):
            content = await github.get_file_content(owner, eng_repo, f"decisions/{filename}", ref)
            if content:
                matched_parts.append(content.strip())

    return "\n\n---\n\n".join(matched_parts)


def _merge_config(base: dict, overlay: dict) -> dict:
    """Deep-merge two config dicts. Overlay values replace base values."""
    merged = dict(base)
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_agent_context(org_rules_md: str, repo_rules_md: str, architecture_context: str) -> str:
    """Combine all prose context with clear section headers."""
    parts: list[str] = []
    if architecture_context:
        parts.append(f"# Architecture\n\n{architecture_context.strip()}")
    if org_rules_md:
        parts.append(f"# Organization Guidelines\n\n{org_rules_md.strip()}")
    if repo_rules_md:
        parts.append(f"# Repository Guidelines\n\n{repo_rules_md.strip()}")
    return "\n\n---\n\n".join(parts)


def _parse_repo_catalog(architecture_context: str) -> list[dict]:
    """Extract repo entries from the architecture context (repos.md section)."""
    repos: list[dict] = []
    if not architecture_context:
        return repos

    catalog_match = re.search(r"## Repository Catalog\s*\n(.*)", architecture_context, re.DOTALL)
    if not catalog_match:
        return repos

    catalog_text = catalog_match.group(1)
    entries = re.split(r"(?=^## \S)", catalog_text, flags=re.MULTILINE)

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        name_match = re.match(r"## (\S+)", entry)
        if not name_match:
            continue
        name = name_match.group(1)
        repo_info: dict = {"name": name}
        for field_name in ("Purpose", "Stack", "Domain", "Owner"):
            field_match = re.search(rf"\*\*{field_name}\*\*:\s*(.+)", entry)
            if field_match:
                repo_info[field_name.lower()] = field_match.group(1).strip()
        repos.append(repo_info)

    return repos
