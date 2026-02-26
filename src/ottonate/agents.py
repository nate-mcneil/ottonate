"""Agent definition sync: repo → ~/.claude/agents/."""

from __future__ import annotations

import shutil
from pathlib import Path

import structlog

log = structlog.get_logger()

AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"
TARGET_DIR = Path.home() / ".claude" / "agents"


def sync_agent_definitions(
    source: Path | None = None, target: Path | None = None
) -> list[str]:
    """Copy agent .md files to ~/.claude/agents/ when the repo copy is newer.

    Returns list of filenames that were updated.
    """
    src = source or AGENTS_DIR
    dst = target or TARGET_DIR

    if not src.is_dir():
        log.warning("agents_dir_missing", path=str(src))
        return []

    dst.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []

    for src_file in sorted(src.glob("*.md")):
        dst_file = dst / src_file.name
        if not dst_file.exists() or src_file.stat().st_mtime > dst_file.stat().st_mtime:
            shutil.copy2(src_file, dst_file)
            log.info("agent_synced", file=src_file.name)
            updated.append(src_file.name)

    return updated
