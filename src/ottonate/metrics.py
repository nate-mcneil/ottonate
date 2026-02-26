"""Stage metrics persistence via SQLite."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import structlog

log = structlog.get_logger()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_ref TEXT NOT NULL,
    stage TEXT NOT NULL,
    agent TEXT,
    cost_usd REAL DEFAULT 0,
    turns_used INTEGER DEFAULT 0,
    is_error BOOLEAN DEFAULT FALSE,
    retry_number INTEGER DEFAULT 0,
    was_stuck BOOLEAN DEFAULT FALSE,
    stuck_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


@dataclass
class IssueMetrics:
    issue_ref: str
    total_stages: int = 0
    total_retries: int = 0
    total_cost_usd: float = 0.0
    was_stuck: bool = False
    stuck_reasons: list[str] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)

    @property
    def needs_retro(self) -> bool:
        return self.total_retries > 0 or self.was_stuck


class MetricsStore:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    async def init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)

    async def record_stage(
        self,
        issue_ref: str,
        stage: str,
        agent: str | None,
        cost_usd: float = 0.0,
        turns_used: int = 0,
        is_error: bool = False,
        retry_number: int = 0,
        *,
        was_stuck: bool = False,
        stuck_reason: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO stage_events
                   (issue_ref, stage, agent, cost_usd, turns_used,
                    is_error, retry_number, was_stuck, stuck_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    issue_ref,
                    stage,
                    agent,
                    cost_usd,
                    turns_used,
                    is_error,
                    retry_number,
                    was_stuck,
                    stuck_reason,
                ),
            )
            await db.commit()

    async def get_issue_summary(self, issue_ref: str) -> IssueMetrics:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM stage_events WHERE issue_ref = ? ORDER BY id",
                (issue_ref,),
            )
            rows = await cursor.fetchall()

        if not rows:
            return IssueMetrics(issue_ref=issue_ref)

        stages = [dict(row) for row in rows]
        total_retries = sum(1 for r in rows if r["retry_number"] > 0)
        total_cost = sum(r["cost_usd"] for r in rows)
        any_stuck = any(r["was_stuck"] for r in rows)
        stuck_reasons = [r["stuck_reason"] for r in rows if r["was_stuck"] and r["stuck_reason"]]

        return IssueMetrics(
            issue_ref=issue_ref,
            total_stages=len(rows),
            total_retries=total_retries,
            total_cost_usd=total_cost,
            was_stuck=any_stuck,
            stuck_reasons=stuck_reasons,
            stages=stages,
        )

    async def close(self) -> None:
        pass
