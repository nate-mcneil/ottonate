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

    async def get_all_issue_summaries(self, days: int | None = None) -> list[IssueMetrics]:
        where = ""
        params: tuple = ()
        if days is not None:
            where = "WHERE created_at >= datetime('now', ?)"
            params = (f"-{days} days",)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT DISTINCT issue_ref FROM stage_events {where} ORDER BY issue_ref",
                params,
            )
            refs = [row["issue_ref"] for row in await cursor.fetchall()]

        return [await self.get_issue_summary(ref) for ref in refs]

    async def get_stage_stats(self, days: int | None = None) -> list[dict]:
        where = ""
        params: tuple = ()
        if days is not None:
            where = "WHERE created_at >= datetime('now', ?)"
            params = (f"-{days} days",)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT
                        stage,
                        COUNT(*) as runs,
                        SUM(CASE WHEN retry_number > 0 THEN 1 ELSE 0 END) as retries,
                        SUM(CASE WHEN was_stuck THEN 1 ELSE 0 END) as stuck_count,
                        SUM(cost_usd) as total_cost,
                        AVG(cost_usd) as avg_cost
                    FROM stage_events
                    {where}
                    GROUP BY stage
                    ORDER BY stage""",
                params,
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_completions(self, days: int | None = None) -> list[dict]:
        where = ""
        params: tuple = ()
        if days is not None:
            where = "AND e.created_at >= datetime('now', ?)"
            params = (f"-{days} days",)

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT
                        e.issue_ref,
                        SUM(e.cost_usd) as total_cost,
                        SUM(CASE WHEN e.retry_number > 0 THEN 1 ELSE 0 END) as retries,
                        MAX(CASE WHEN e.was_stuck THEN 1 ELSE 0 END) as was_stuck,
                        MIN(e.created_at) as started_at,
                        MAX(e.created_at) as completed_at
                    FROM stage_events e
                    WHERE e.issue_ref IN (
                        SELECT DISTINCT issue_ref FROM stage_events
                        WHERE stage = 'agentMergeReady'
                    )
                    {where}
                    GROUP BY e.issue_ref
                    ORDER BY completed_at DESC""",
                params,
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_throughput_stats(self, days: int | None = None) -> dict:
        where = ""
        params: tuple = ()
        if days is not None:
            where = "WHERE created_at >= datetime('now', ?)"
            params = (f"-{days} days",)

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                f"SELECT COUNT(DISTINCT issue_ref) as cnt FROM stage_events {where}",
                params,
            )
            row = await cursor.fetchone()
            total_issues = row[0] if row else 0

            cursor = await db.execute(
                f"""SELECT COUNT(DISTINCT issue_ref) as cnt FROM stage_events
                    WHERE stage = 'agentMergeReady' {where.replace("WHERE", "AND")}""",
                params,
            )
            row = await cursor.fetchone()
            completed_issues = row[0] if row else 0

            cursor = await db.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) as total FROM stage_events {where}",
                params,
            )
            row = await cursor.fetchone()
            total_cost = row[0] if row else 0.0

        avg_cost = total_cost / total_issues if total_issues else 0.0

        return {
            "total_issues": total_issues,
            "completed_issues": completed_issues,
            "total_cost": total_cost,
            "avg_cost_per_issue": avg_cost,
        }

    async def close(self) -> None:
        pass
