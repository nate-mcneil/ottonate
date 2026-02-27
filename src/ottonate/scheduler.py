"""Main scheduler -- polls GitHub issues and dispatches to pipeline handlers."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog

from ottonate.config import OttonateConfig
from ottonate.github import GitHubClient
from ottonate.metrics import MetricsStore
from ottonate.models import ACTIONABLE_LABELS, Ticket
from ottonate.pipeline import Pipeline
from ottonate.rules import load_rules

log = structlog.get_logger()


class Scheduler:
    def __init__(self, config: OttonateConfig):
        self.config = config
        self.github = GitHubClient()
        self.metrics = MetricsStore(config.resolved_db_path())
        self._rate_limited_until: float = 0.0
        self.pipeline = Pipeline(
            config,
            self.github,
            metrics=self.metrics,
            on_rate_limit=self._signal_rate_limit,
        )
        self._semaphore = asyncio.Semaphore(config.max_concurrent_tickets)
        self._running = True
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        await self.metrics.init_db()
        log.info("scheduler_started", max_concurrent=self.config.max_concurrent_tickets)
        try:
            await self._poll_loop()
        except asyncio.CancelledError:
            log.info("scheduler_cancelled")
        finally:
            log.info("scheduler_stopped")

    async def stop(self) -> None:
        self._running = False

    async def process_single(self, owner: str, repo: str, issue_number: int) -> None:
        """Manually drive a single issue through one pipeline step."""
        labels = await self.github.get_issue_labels(owner, repo, issue_number)
        issue_data = await self.github.get_issue(owner, repo, issue_number)

        ticket = Ticket(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            labels=set(labels),
            summary=issue_data.get("title", ""),
            work_dir=str(self._workspace_path(owner, repo, issue_number)),
        )
        rules = await load_rules(owner, repo, self.config, self.github)
        await self._ensure_workspace(ticket)

        if ticket.agent_label is None:
            await self.pipeline.handle_new(ticket, rules)
        else:
            await self.pipeline.handle(ticket, rules)

    # -- Main loop --

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_and_dispatch()
            except Exception:
                log.exception("poll_error")
            await asyncio.sleep(self.config.poll_interval_s)

    def _signal_rate_limit(self) -> None:
        self._rate_limited_until = time.monotonic() + self.config.rate_limit_cooldown_s
        log.warning("rate_limit_cooldown", cooldown_s=self.config.rate_limit_cooldown_s)

    async def _poll_and_dispatch(self) -> None:
        remaining = self._rate_limited_until - time.monotonic()
        if remaining > 0:
            log.info("poll_skipped_rate_limit", remaining_s=round(remaining))
            return

        org = self.config.github_org
        if not org:
            log.error("no_github_org_configured")
            return

        try:
            issues = await self.github.search_issues(org, self.config.github_agent_label)
        except Exception:
            log.exception("search_error")
            return

        for issue in issues:
            repo_data = issue.get("repository", {})
            repo_name = repo_data.get("name", "")
            if not repo_name:
                continue

            number = issue.get("number")
            if not number:
                continue

            flight_key = f"{org}/{repo_name}#{number}"
            if flight_key in self._in_flight:
                continue

            issue_labels = [
                lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                for lbl in issue.get("labels", [])
            ]

            ticket = Ticket(
                owner=org,
                repo=repo_name,
                issue_number=number,
                labels=set(issue_labels),
                summary=issue.get("title", ""),
                work_dir=str(self._workspace_path(org, repo_name, number)),
            )

            stage = ticket.agent_label
            is_eng_repo = repo_name == self.config.github_engineering_repo

            if stage is None:
                if is_eng_repo:
                    asyncio.create_task(self._handle_with_semaphore(ticket, new_ticket=True))
                else:
                    asyncio.create_task(self._handle_with_semaphore(ticket, new_ticket=True))
            elif stage in ACTIONABLE_LABELS:
                asyncio.create_task(self._handle_with_semaphore(ticket))

    async def _handle_with_semaphore(self, ticket: Ticket, *, new_ticket: bool = False) -> None:
        flight_key = ticket.issue_ref
        self._in_flight.add(flight_key)
        try:
            async with self._semaphore:
                rules = await load_rules(ticket.owner, ticket.repo, self.config, self.github)
                await self._ensure_workspace(ticket)
                if new_ticket:
                    await self.pipeline.handle_new(ticket, rules)
                else:
                    await self.pipeline.handle(ticket, rules)
        except Exception:
            log.exception("handle_error", issue=ticket.issue_ref)
        finally:
            self._in_flight.discard(flight_key)

    # -- Workspace --

    def _workspace_path(self, owner: str, repo: str, issue_number: int) -> Path:
        return self.config.resolved_workspace_dir() / f"{owner}_{repo}_{issue_number}"

    async def _ensure_workspace(self, ticket: Ticket) -> None:
        path = Path(ticket.work_dir)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            "gh",
            "repo",
            "clone",
            ticket.full_repo,
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error("clone_failed", repo=ticket.full_repo, stderr=stderr.decode())
            raise RuntimeError(f"Failed to clone {ticket.full_repo}")
        log.info("workspace_created", repo=ticket.full_repo, path=ticket.work_dir)
