"""Label-driven pipeline. GitHub labels ARE the state machine."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path

import structlog
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from ottonate.config import OttonateConfig
from ottonate.enrichment import EnrichedStory, enrich_story_prompt, parse_enriched_story
from ottonate.github import GitHubClient
from ottonate.metrics import MetricsStore
from ottonate.models import (
    CIStatus,
    Label,
    ReviewStatus,
    StageResult,
    Ticket,
)
from ottonate.prompts import (
    backlog_prompt,
    ci_fixer_prompt,
    implementer_prompt,
    planner_prompt,
    quality_gate_prompt,
    retro_prompt,
    review_responder_prompt,
    reviewer_prompt,
    spec_prompt,
)
from ottonate.rules import ResolvedRules
from ottonate.traceability import Artifact, ArtifactType, TraceabilityGraph

log = structlog.get_logger()


# -- Agent invocation --


class RateLimitExhaustedError(Exception):
    """Raised when rate limit backoff exceeds max delay."""


async def run_agent(
    agent_name: str,
    prompt: str,
    cwd: str,
    *,
    config: OttonateConfig | None = None,
    on_rate_limit: Callable[[], None] | None = None,
    base_delay: int = 60,
    max_delay: int = 600,
) -> StageResult:
    """Invoke a named agent defined in ~/.claude/agents/."""
    env: dict[str, str] = {}
    if config and config.use_bedrock:
        env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if config.aws_region:
            env["AWS_REGION"] = config.aws_region
        if config.aws_profile:
            env["AWS_PROFILE"] = config.aws_profile
        if config.bedrock_model:
            env["ANTHROPIC_MODEL"] = config.bedrock_model
        if config.bedrock_small_model:
            env["ANTHROPIC_SMALL_FAST_MODEL"] = config.bedrock_small_model

    log.info(
        "run_agent_start",
        agent=agent_name,
        cwd=cwd,
        use_bedrock=bool(env),
        env_keys=list(env.keys()) if env else [],
    )

    def _log_stderr(line: str) -> None:
        log.warning("agent_stderr", agent=agent_name, line=line)

    options = ClaudeAgentOptions(
        setting_sources=["user"],
        system_prompt={"type": "preset", "preset": "claude_code"},
        permission_mode="bypassPermissions",
        cwd=cwd,
        env=env,
        stderr=_log_stderr,
    )
    rate_limit_delay = base_delay
    attempt = 0
    max_attempts = 6

    while attempt < max_attempts:
        attempt += 1
        all_assistant_texts: list[str] = []
        session_id = ""
        cost = 0.0
        turns = 0
        is_error = False
        result_text = ""
        saw_rate_limit = False

        message_iter = query(prompt=f"/agent:{agent_name}\n\n{prompt}", options=options)
        while True:
            try:
                message = await message_iter.__anext__()
            except StopAsyncIteration:
                break
            except Exception as e:
                err_msg = str(e).lower()
                if "unknown message type" in err_msg:
                    if "rate_limit" in err_msg:
                        saw_rate_limit = True
                    log.debug("sdk_unknown_message", agent=agent_name, error=str(e))
                    continue
                is_rate_limit = any(
                    s in err_msg for s in ("rate_limit", "rate limit", "429", "overloaded")
                )
                if is_rate_limit:
                    saw_rate_limit = True
                    log.warning(
                        "rate_limit_exception",
                        agent=agent_name,
                        delay=rate_limit_delay,
                    )
                    if on_rate_limit:
                        on_rate_limit()
                    await asyncio.sleep(rate_limit_delay)
                    rate_limit_delay = min(rate_limit_delay * 2, max_delay)
                    continue
                raise

            if isinstance(message, AssistantMessage):
                if message.error == "rate_limit":
                    saw_rate_limit = True
                    log.warning(
                        "rate_limit_inline",
                        agent=agent_name,
                        delay=rate_limit_delay,
                    )
                    if on_rate_limit:
                        on_rate_limit()
                    await asyncio.sleep(rate_limit_delay)
                    rate_limit_delay = min(rate_limit_delay * 2, max_delay)
                    continue
                rate_limit_delay = base_delay
                for block in message.content:
                    if isinstance(block, TextBlock):
                        all_assistant_texts.append(block.text)
            elif isinstance(message, ResultMessage):
                result_text = message.result or ""
                session_id = message.session_id
                cost = message.total_cost_usd or 0.0
                turns = message.num_turns
                is_error = message.is_error

        has_output = bool(all_assistant_texts) or bool(result_text)
        if not has_output and saw_rate_limit:
            log.warning(
                "rate_limit_session_retry",
                agent=agent_name,
                attempt=attempt,
                delay=rate_limit_delay,
            )
            if on_rate_limit:
                on_rate_limit()
            await asyncio.sleep(rate_limit_delay)
            rate_limit_delay = min(rate_limit_delay * 2, max_delay)
            continue

        break

    full_text = "\n".join(all_assistant_texts) if all_assistant_texts else result_text
    log.info(
        "agent_output",
        agent=agent_name,
        text_blocks=len(all_assistant_texts),
        result_len=len(result_text),
        full_len=len(full_text),
        attempts=attempt,
    )
    return StageResult(
        text=full_text,
        session_id=session_id,
        cost_usd=cost,
        turns_used=turns,
        is_error=is_error,
    )


# -- Pipeline --


class Pipeline:
    def __init__(
        self,
        config: OttonateConfig,
        github: GitHubClient,
        metrics: MetricsStore | None = None,
        on_rate_limit: Callable[[], None] | None = None,
    ):
        self.config = config
        self.github = github
        self.metrics = metrics
        self.agent_label = config.github_agent_label
        self.trace = TraceabilityGraph()
        self._on_rate_limit = on_rate_limit
        self._attempts: dict[str, dict[str, int]] = {}

    def _check_retries(self, issue_ref: str, stage: str, max_retries: int) -> bool:
        ticket_attempts = self._attempts.setdefault(issue_ref, {})
        count = ticket_attempts.get(stage, 0) + 1
        ticket_attempts[stage] = count
        return count <= max_retries

    async def _record(
        self,
        issue_ref: str,
        stage: str,
        agent: str | None,
        result: StageResult | None = None,
        retry_number: int = 0,
        *,
        was_stuck: bool = False,
        stuck_reason: str | None = None,
    ) -> None:
        if not self.metrics:
            return
        try:
            await self.metrics.record_stage(
                issue_ref=issue_ref,
                stage=stage,
                agent=agent,
                cost_usd=result.cost_usd if result else 0.0,
                turns_used=result.turns_used if result else 0,
                is_error=result.is_error if result else False,
                retry_number=retry_number,
                was_stuck=was_stuck,
                stuck_reason=stuck_reason,
            )
        except Exception:
            log.warning("metrics_record_failed", issue=issue_ref, stage=stage)

    async def _run(self, agent_name: str, prompt: str, cwd: str) -> StageResult:
        return await run_agent(
            agent_name,
            prompt,
            cwd,
            config=self.config,
            on_rate_limit=self._on_rate_limit,
            base_delay=self.config.rate_limit_base_delay_s,
            max_delay=self.config.rate_limit_max_delay_s,
        )

    async def handle_new(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """Handle a newly discovered issue (has entry label but no stage label).

        Issues in the engineering repo enter the spec path;
        everything else enters the dev planning path.
        """
        is_eng_repo = ticket.repo == self.config.github_engineering_repo
        if is_eng_repo:
            await self._handle_spec(ticket, rules)
        else:
            await self._handle_agent(ticket, rules)

    async def handle(self, ticket: Ticket, rules: ResolvedRules) -> None:
        label = ticket.agent_label
        if label is None:
            return

        handler = {
            Label.SPEC_REVIEW: self._handle_spec_review,
            Label.SPEC_APPROVED: self._handle_spec_approved,
            Label.BACKLOG_REVIEW: self._handle_backlog_review,
            Label.PLAN_REVIEW: self._handle_plan_review,
            Label.PLAN: self._handle_plan,
            Label.PR: self._handle_pr,
            Label.SELF_REVIEW: self._handle_self_review,
            Label.REVIEW: self._handle_review,
            Label.MERGE_READY: self._handle_merge_ready,
            Label.RETRO: self._handle_retro,
        }.get(label)

        if handler is None:
            return

        try:
            await handler(ticket, rules)
        except Exception:
            log.exception("stage_failed", issue=ticket.issue_ref, label=label)
            raise

    # -- Spec & backlog handlers --

    async def _handle_spec(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """Generate a spec from an initiative issue in the engineering repo."""
        comments = await self.github.get_comments(ticket.owner, ticket.repo, ticket.issue_number)
        if any("Spec PR:" in c for c in comments):
            log.info("spec_already_exists", issue=ticket.issue_ref)
            return

        await self.github.add_label(
            ticket.owner, ticket.repo, ticket.issue_number, Label.SPEC.value
        )
        description = await self.github.get_issue_body(
            ticket.owner, ticket.repo, ticket.issue_number
        )
        prompt = spec_prompt(ticket, description, rules_context=rules.agent_context)
        result = await self._run("otto-spec-agent", prompt, ticket.work_dir)

        log.info("spec_agent_done", issue=ticket.issue_ref, turns=result.turns_used)
        await self._record(ticket.issue_ref, "spec", "otto-spec-agent", result)

        if "[SPEC_NEEDS_INPUT]" in result.text or result.is_error:
            await self._stuck(ticket, rules, "Spec agent needs more input or errored")
            return

        spec_file = Path(ticket.work_dir) / "SPEC.md" if ticket.work_dir else None
        spec_text = (
            spec_file.read_text().strip() if spec_file and spec_file.exists() else result.text
        )

        if not spec_text:
            await self._stuck(ticket, rules, "Spec agent produced no output")
            return

        await self.github.add_comment(
            ticket.owner,
            ticket.repo,
            ticket.issue_number,
            "Spec PR: opened in engineering repo",
        )

        self.trace.add_artifact(
            Artifact(
                type=ArtifactType.SPEC,
                id=f"spec:{ticket.issue_ref}",
                title=ticket.summary,
            )
        )
        await self.github.swap_label(
            ticket.owner, ticket.repo, ticket.issue_number, Label.SPEC, Label.SPEC_REVIEW
        )

    async def _handle_spec_review(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentSpecReview: check if the spec PR has been merged."""
        if not ticket.spec_pr_number:
            comments = await self.github.get_comments(
                ticket.owner, ticket.repo, ticket.issue_number
            )
            for comment in reversed(comments):
                match = re.search(r"Spec PR: #(\d+)", comment)
                if match:
                    ticket.spec_pr_number = int(match.group(1))
                    break

        if not ticket.spec_pr_number:
            return

        state = await self.github.get_pr_state(ticket.owner, ticket.repo, ticket.spec_pr_number)

        if state == "MERGED":
            await self.github.swap_label(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                Label.SPEC_REVIEW,
                Label.SPEC_APPROVED,
            )
        elif state == "CLOSED":
            await self._stuck(ticket, rules, "Spec PR was closed without merging")

    async def _handle_spec_approved(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentSpecApproved -> agentBacklogGen: generate backlog stories from spec."""
        comments = await self.github.get_comments(ticket.owner, ticket.repo, ticket.issue_number)

        if any("Backlog PR:" in c or "Stories Created" in c for c in comments):
            log.info("backlog_already_exists", issue=ticket.issue_ref)
            await self.github.remove_label(
                ticket.owner, ticket.repo, ticket.issue_number, Label.SPEC_APPROVED.value
            )
            return

        await self.github.swap_label(
            ticket.owner,
            ticket.repo,
            ticket.issue_number,
            Label.SPEC_APPROVED,
            Label.BACKLOG_GEN,
        )

        spec_text = await self.github.get_file_content(
            ticket.owner,
            ticket.repo,
            f"specs/{ticket.issue_number}-*/SPEC.md",
            self.config.github_default_branch,
        )
        if not spec_text:
            spec_file = Path(ticket.work_dir) / "SPEC.md" if ticket.work_dir else None
            spec_text = spec_file.read_text().strip() if spec_file and spec_file.exists() else ""

        if not spec_text:
            await self._stuck(ticket, rules, "Could not find approved spec content")
            return

        prompt = backlog_prompt(ticket, spec_text, rules_context=rules.agent_context)
        result = await self._run("otto-planner", prompt, ticket.work_dir)

        if "[BACKLOG_COMPLETE]" not in result.text or result.is_error:
            await self._stuck(ticket, rules, "Backlog generation failed")
            return

        stories_json = _extract_json_array(result.text)
        if stories_json:
            await self.github.add_comment(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                f"## Generated Backlog\n\n```json\n{json.dumps(stories_json, indent=2)}\n```",
            )
        await self.github.swap_label(
            ticket.owner,
            ticket.repo,
            ticket.issue_number,
            Label.BACKLOG_GEN,
            Label.BACKLOG_REVIEW,
        )

    async def _handle_backlog_review(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentBacklogReview: check if the backlog PR has been merged."""
        if not ticket.backlog_pr_number:
            comments = await self.github.get_comments(
                ticket.owner, ticket.repo, ticket.issue_number
            )
            for comment in reversed(comments):
                match = re.search(r"Backlog PR: #(\d+)", comment)
                if match:
                    ticket.backlog_pr_number = int(match.group(1))
                    break
            if not ticket.backlog_pr_number:
                for comment in reversed(comments):
                    lower = comment.lower()
                    if "backlog approved" in lower or "stories approved" in lower:
                        await self._create_stories_from_backlog(ticket, rules)
                        await self.github.remove_label(
                            ticket.owner,
                            ticket.repo,
                            ticket.issue_number,
                            Label.BACKLOG_REVIEW.value,
                        )
                        return
                    if "backlog rejected" in lower:
                        await self._stuck(ticket, rules, "Backlog rejected by reviewer")
                        return
                return

        state = await self.github.get_pr_state(ticket.owner, ticket.repo, ticket.backlog_pr_number)

        if state == "MERGED":
            await self._create_stories_from_backlog(ticket, rules)
            await self.github.remove_label(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                Label.BACKLOG_REVIEW.value,
            )
        elif state == "CLOSED":
            await self._stuck(ticket, rules, "Backlog PR was closed without merging")

    async def _create_stories_from_backlog(self, ticket: Ticket, rules: ResolvedRules) -> list[str]:
        comments = await self.github.get_comments(ticket.owner, ticket.repo, ticket.issue_number)
        stories_data = None
        for comment in reversed(comments):
            if "Generated Backlog" in comment:
                stories_data = _extract_json_array(comment)
                if stories_data:
                    break

        if not stories_data:
            log.warning("no_backlog_json", issue=ticket.issue_ref)
            return []

        created_refs: list[str] = []

        for story in stories_data:
            enriched = await self._enrich_story(story)
            title = enriched.title if enriched else story.get("title", "Untitled Story")
            body = enriched.to_markdown() if enriched else story.get("description", "")
            target_repo = (enriched.repo if enriched else story.get("repo", "")) or ticket.repo

            labels = [self.agent_label]
            try:
                number = await self.github.create_issue(
                    ticket.owner, target_repo, title, body, labels
                )
                ref = f"{ticket.owner}/{target_repo}#{number}"
                created_refs.append(ref)

                self.trace.add_artifact(
                    Artifact(
                        type=ArtifactType.STORY,
                        id=ref,
                        title=title,
                    )
                )
                self.trace.link(
                    ArtifactType.SPEC,
                    f"spec:{ticket.issue_ref}",
                    ArtifactType.STORY,
                    ref,
                )

                if ticket.project_id:
                    issue_url = f"https://github.com/{ticket.owner}/{target_repo}/issues/{number}"
                    await self.github.add_to_project(ticket.owner, ticket.project_id, issue_url)
            except Exception:
                log.exception("story_creation_failed", title=title, repo=target_repo)

        if created_refs:
            await self.github.add_comment(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                f"## Stories Created\n\n{', '.join(created_refs)}",
            )
        return created_refs

    async def _enrich_story(self, story_data: dict) -> EnrichedStory | None:
        prompt = enrich_story_prompt(story_data)
        try:
            result = await self._run("otto-planner", prompt, None)
            return parse_enriched_story(result.text)
        except Exception:
            log.warning("story_enrichment_failed", title=story_data.get("title"))
            return None

    # -- Dev pipeline handlers --

    async def _handle_agent(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """Entry -> agentPlanning: pick up issue, run planner."""
        await self.github.add_label(
            ticket.owner, ticket.repo, ticket.issue_number, Label.PLANNING.value
        )

        description = await self.github.get_issue_body(
            ticket.owner, ticket.repo, ticket.issue_number
        )
        prompt = planner_prompt(ticket, description, rules_context=rules.agent_context)
        result = await self._run("otto-planner", prompt, ticket.work_dir)

        log.info(
            "planner_done",
            issue=ticket.issue_ref,
            turns=result.turns_used,
            cost=result.cost_usd,
            result_len=len(result.text),
            result_preview=result.text[:200],
        )
        await self._record(ticket.issue_ref, "planning", "otto-planner", result)

        if "[NEEDS_MORE_INFO]" in result.text or result.is_error:
            await self._stuck(ticket, rules, "Planner needs more info or errored")
            return

        plan_file = Path(ticket.work_dir) / "PLAN.md"
        if plan_file.exists():
            plan_text = plan_file.read_text().strip()
        else:
            plan_text = _extract_plan(result.text)

        if not plan_text:
            await self._stuck(ticket, rules, "Planner produced no plan output")
            return

        await self.github.add_comment(
            ticket.owner,
            ticket.repo,
            ticket.issue_number,
            "## Development Plan\n\nPlan committed to feature branch as PLAN.md",
        )
        ticket.plan = plan_text
        await self.github.swap_label(
            ticket.owner, ticket.repo, ticket.issue_number, Label.PLANNING, Label.PLAN_REVIEW
        )

    async def _handle_plan_review(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentPlanReview -> agentPlan or back to agentPlanning."""
        description = await self.github.get_issue_body(
            ticket.owner, ticket.repo, ticket.issue_number
        )
        plan = ticket.plan or await self._get_plan(ticket)
        prompt = quality_gate_prompt(ticket, plan, description)
        result = await self._run("otto-quality-gate", prompt, ticket.work_dir)

        verdict = _parse_quality_verdict(result.text)
        log.info("quality_gate_done", issue=ticket.issue_ref, verdict=verdict)
        await self._record(ticket.issue_ref, "plan_review", "otto-quality-gate", result)

        if verdict == "pass":
            await self.github.swap_label(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                Label.PLAN_REVIEW,
                Label.PLAN,
            )
        elif verdict == "fail_retryable":
            if not self._check_retries(ticket.issue_ref, "plan", self.config.max_plan_retries):
                await self._stuck(ticket, rules, "Plan retry limit exceeded")
                return
            await self.github.swap_label(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                Label.PLAN_REVIEW,
                Label.PLANNING,
            )
            feedback = _parse_quality_feedback(result.text)
            description = await self.github.get_issue_body(
                ticket.owner, ticket.repo, ticket.issue_number
            )
            desc_with_feedback = description + f"\n\n## Previous Plan Feedback\n{feedback}"
            prompt = planner_prompt(ticket, desc_with_feedback, rules_context=rules.agent_context)
            result = await self._run("otto-planner", prompt, ticket.work_dir)
            if "[NEEDS_MORE_INFO]" in result.text or result.is_error:
                await self._stuck(ticket, rules, "Planner failed on retry")
                return
            plan_file = Path(ticket.work_dir) / "PLAN.md" if ticket.work_dir else None
            revised_plan = (
                plan_file.read_text().strip() if plan_file and plan_file.exists() else result.text
            )
            await self.github.add_comment(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                "## Development Plan (revised)\n\nRevised plan committed to branch as PLAN.md",
            )
            ticket.plan = revised_plan
            await self.github.swap_label(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                Label.PLANNING,
                Label.PLAN_REVIEW,
            )
        else:
            await self._stuck(ticket, rules, "Quality gate escalated")

    async def _handle_plan(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentPlan -> agentImplementing -> agentPR: run implementer."""
        await self.github.swap_label(
            ticket.owner,
            ticket.repo,
            ticket.issue_number,
            Label.PLAN,
            Label.IMPLEMENTING,
        )

        plan = ticket.plan or await self._get_plan(ticket)
        branch_name = _slugify_branch(ticket.issue_number, plan, rules.branch_pattern)
        prompt = implementer_prompt(ticket, plan, branch_name, rules_context=rules.agent_context)
        result = await self._run("otto-implementer", prompt, ticket.work_dir)

        log.info(
            "implementer_done",
            issue=ticket.issue_ref,
            turns=result.turns_used,
            cost=result.cost_usd,
        )
        await self._record(ticket.issue_ref, "implementing", "otto-implementer", result)

        if "[IMPLEMENTATION_BLOCKED]" in result.text or result.is_error:
            if not self._check_retries(
                ticket.issue_ref, "implement", self.config.max_implement_retries
            ):
                await self._stuck(ticket, rules, "Implementation retry limit exceeded")
                return
            await self._stuck(ticket, rules, "Implementation blocked")
            return

        pr_number = _extract_pr_number(result.text)
        if pr_number:
            ticket.pr_number = pr_number
            self.trace.add_artifact(
                Artifact(
                    type=ArtifactType.PR,
                    id=f"PR#{pr_number}",
                    title=f"{ticket.issue_ref} PR",
                    metadata={"repo": ticket.full_repo},
                )
            )
            self.trace.link(
                ArtifactType.STORY,
                ticket.issue_ref,
                ArtifactType.PR,
                f"PR#{pr_number}",
            )
            await self.github.add_comment(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                f"PR created: #{pr_number}",
            )
            await self.github.swap_label(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                Label.IMPLEMENTING,
                Label.PR,
            )
        else:
            await self._stuck(
                ticket, rules, "Implementer finished but no PR number found in output"
            )

    async def _handle_pr(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentPR: check CI status."""
        owner, repo = ticket.owner, ticket.repo

        if not ticket.pr_number:
            pr_number, pr_state = await self.github.find_pr(owner, repo, str(ticket.issue_number))
            if pr_number and pr_state == "MERGED":
                log.info("pr_already_merged", issue=ticket.issue_ref, pr_number=pr_number)
                await self.github.remove_label(owner, repo, ticket.issue_number, Label.PR.value)
                return
            elif pr_number:
                ticket.pr_number = pr_number
                log.info("pr_discovered", issue=ticket.issue_ref, pr_number=pr_number)
            else:
                await self._stuck(ticket, rules, "PR label present but no PR found")
                return

        status = await self.github.get_ci_status(owner, repo, ticket.pr_number)

        if status == CIStatus.PASSED:
            await self.github.swap_label(
                owner, repo, ticket.issue_number, Label.PR, Label.SELF_REVIEW
            )
        elif status == CIStatus.FAILED:
            if not self._check_retries(ticket.issue_ref, "ci_fix", self.config.max_ci_fix_retries):
                await self._stuck(ticket, rules, "CI fix retry limit exceeded")
                return
            await self.github.swap_label(owner, repo, ticket.issue_number, Label.PR, Label.CI_FIX)
            failure_logs = await self.github.get_ci_failure_logs(owner, repo, ticket.pr_number)
            prompt = ci_fixer_prompt(ticket, failure_logs)
            result = await self._run("otto-ci-fixer", prompt, ticket.work_dir)

            log.info("ci_fixer_done", issue=ticket.issue_ref, turns=result.turns_used)
            await self._record(ticket.issue_ref, "ci_fix", "otto-ci-fixer", result)

            if "[CI_FIX_BLOCKED]" in result.text or result.is_error:
                await self._stuck(ticket, rules, "CI fix blocked")
                return
            await self.github.swap_label(owner, repo, ticket.issue_number, Label.CI_FIX, Label.PR)

    async def _handle_self_review(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentSelfReview -> agentReview or back to fix."""
        owner, repo = ticket.owner, ticket.repo
        plan = ticket.plan or await self._get_plan(ticket)
        diff = await self.github.get_pr_diff(owner, repo, ticket.pr_number)
        prompt = reviewer_prompt(ticket, plan, diff)
        result = await self._run("otto-reviewer", prompt, ticket.work_dir)

        verdict = _parse_review_verdict(result.text)
        log.info("self_review_done", issue=ticket.issue_ref, verdict=verdict)
        await self._record(ticket.issue_ref, "self_review", "otto-reviewer", result)

        if verdict == "clean":
            await self.github.swap_label(
                owner, repo, ticket.issue_number, Label.SELF_REVIEW, Label.REVIEW
            )
            if rules.notify_team:
                await self.github.request_review(owner, repo, ticket.pr_number, rules.notify_team)
        else:
            await self.github.swap_label(
                owner, repo, ticket.issue_number, Label.SELF_REVIEW, Label.IMPLEMENTING
            )
            prompt = f"The self-review found issues:\n\n{result.text}\n\nFix these issues and push."
            await self._run("otto-implementer", prompt, ticket.work_dir)
            await self.github.swap_label(
                owner, repo, ticket.issue_number, Label.IMPLEMENTING, Label.PR
            )

    async def _handle_review(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentReview: check for human review."""
        owner, repo = ticket.owner, ticket.repo

        if not ticket.pr_number:
            pr_number, pr_state = await self.github.find_pr(owner, repo, str(ticket.issue_number))
            if pr_number and pr_state == "MERGED":
                await self.github.remove_label(owner, repo, ticket.issue_number, Label.REVIEW.value)
                return
            elif pr_number:
                ticket.pr_number = pr_number
            else:
                return

        review_status = await self.github.get_review_status(owner, repo, ticket.pr_number)

        if review_status == ReviewStatus.APPROVED:
            ci_status = await self.github.get_ci_status(owner, repo, ticket.pr_number)
            if ci_status == CIStatus.PASSED:
                await self.github.swap_label(
                    owner, repo, ticket.issue_number, Label.REVIEW, Label.MERGE_READY
                )
        elif review_status in (ReviewStatus.CHANGES_REQUESTED, ReviewStatus.COMMENTED):
            if not self._check_retries(ticket.issue_ref, "review", self.config.max_review_retries):
                await self._stuck(ticket, rules, "Review address retry limit exceeded")
                return
            await self.github.swap_label(
                owner, repo, ticket.issue_number, Label.REVIEW, Label.ADDRESSING_REVIEW
            )

            comments = await self.github.get_unaddressed_comments(
                owner, repo, ticket.pr_number, self.config.github_username
            )
            if not comments:
                await self.github.swap_label(
                    owner,
                    repo,
                    ticket.issue_number,
                    Label.ADDRESSING_REVIEW,
                    Label.REVIEW,
                )
                return

            prompt = review_responder_prompt(ticket, comments, owner, repo)
            result = await self._run("otto-review-responder", prompt, ticket.work_dir)

            log.info("review_responder_done", issue=ticket.issue_ref, turns=result.turns_used)
            await self._record(
                ticket.issue_ref, "addressing_review", "otto-review-responder", result
            )

            if "[REVIEW_ESCALATE]" in result.text:
                await self._stuck(ticket, rules, "Review comment requires human decision")
                return

            await self.github.swap_label(
                owner, repo, ticket.issue_number, Label.ADDRESSING_REVIEW, Label.PR
            )

    async def _handle_merge_ready(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentMergeReady: check merge status. If merged and had issues, trigger retro."""
        owner, repo = ticket.owner, ticket.repo

        if ticket.pr_number:
            pr_state = await self.github.get_pr_state(owner, repo, ticket.pr_number)
        else:
            pr_state = "UNKNOWN"

        if pr_state != "MERGED":
            comments = await self.github.get_comments(owner, repo, ticket.issue_number)
            already_notified = any("merge-ready" in c.lower() for c in comments)
            if not already_notified and rules.notify_team:
                await self.github.mention_on_issue(
                    owner,
                    repo,
                    ticket.issue_number,
                    rules.notify_team,
                    f"PR #{ticket.pr_number} is merge-ready (approved + CI green). "
                    f"Ready for merge.",
                )
            log.info("ticket_merge_ready_waiting", issue=ticket.issue_ref)
            return

        if self.metrics:
            summary = await self.metrics.get_issue_summary(ticket.issue_ref)
            if summary.needs_retro:
                log.info(
                    "ticket_needs_retro",
                    issue=ticket.issue_ref,
                    retries=summary.total_retries,
                    stuck=summary.was_stuck,
                )
                await self.github.swap_label(
                    owner,
                    repo,
                    ticket.issue_number,
                    Label.MERGE_READY,
                    Label.RETRO,
                )
                return

        await self.github.remove_label(owner, repo, ticket.issue_number, Label.MERGE_READY.value)
        await self.github.remove_label(owner, repo, ticket.issue_number, self.agent_label)
        log.info("ticket_complete", issue=ticket.issue_ref)

    async def _handle_retro(self, ticket: Ticket, rules: ResolvedRules) -> None:
        """agentRetro: run a retrospective on a completed issue."""
        owner, repo = ticket.owner, ticket.repo
        summary = await self.metrics.get_issue_summary(ticket.issue_ref) if self.metrics else None
        if not summary:
            from ottonate.metrics import IssueMetrics

            summary = IssueMetrics(issue_ref=ticket.issue_ref)

        plan = ticket.plan or await self._get_plan(ticket)
        comments = await self.github.get_comments(owner, repo, ticket.issue_number)
        comment_dicts = [{"author": "unknown", "body": c} for c in comments]

        prompt = retro_prompt(
            ticket,
            plan,
            summary,
            comment_dicts,
            rules_context=rules.agent_context,
        )

        eng_dir = self._eng_workspace_path()
        await self._ensure_eng_workspace()

        result = await self._run("otto-retro", prompt, str(eng_dir))
        await self._record(ticket.issue_ref, "retro", "otto-retro", result)

        if "[SELF_IMPROVEMENT]" in result.text:
            improvement = _parse_self_improvement(result.text)
            if improvement:
                await self.github.create_issue(
                    owner,
                    "ottonate",
                    improvement["title"],
                    improvement["body"],
                )

        await self.github.remove_label(owner, repo, ticket.issue_number, Label.RETRO.value)
        await self.github.remove_label(owner, repo, ticket.issue_number, self.agent_label)
        await self.github.add_comment(
            owner,
            repo,
            ticket.issue_number,
            f"Retro complete. {result.text[:200]}",
        )
        log.info("retro_complete", issue=ticket.issue_ref)

    def _eng_workspace_path(self) -> Path:
        return self.config.resolved_workspace_dir() / "engineering"

    async def _ensure_eng_workspace(self) -> None:
        eng_dir = self._eng_workspace_path()
        if eng_dir.exists():
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(eng_dir),
                "pull",
                "--ff-only",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        else:
            eng_dir.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "repo",
                "clone",
                self.config.engineering_repo_full,
                str(eng_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

    # -- Helpers --

    async def _stuck(self, ticket: Ticket, rules: ResolvedRules, reason: str) -> None:
        log.warning("ticket_stuck", issue=ticket.issue_ref, reason=reason)
        await self._record(
            ticket.issue_ref,
            "stuck",
            None,
            was_stuck=True,
            stuck_reason=reason,
        )
        current = ticket.agent_label
        if current:
            await self.github.swap_label(
                ticket.owner, ticket.repo, ticket.issue_number, current, Label.STUCK
            )
        else:
            await self.github.add_label(
                ticket.owner, ticket.repo, ticket.issue_number, Label.STUCK.value
            )
        await self.github.add_comment(
            ticket.owner,
            ticket.repo,
            ticket.issue_number,
            f"Ottonate agent stopped: {reason}",
        )
        if rules.notify_team:
            await self.github.mention_on_issue(
                ticket.owner,
                ticket.repo,
                ticket.issue_number,
                rules.notify_team,
                f"Issue stuck: {reason}",
            )

    async def _get_plan(self, ticket: Ticket) -> str:
        plan_file = Path(ticket.work_dir) / "PLAN.md" if ticket.work_dir else None
        if plan_file and plan_file.exists():
            return plan_file.read_text().strip()
        comments = await self.github.get_comments(ticket.owner, ticket.repo, ticket.issue_number)
        for comment in reversed(comments):
            marker = "## Development Plan"
            idx = comment.find(marker)
            if idx != -1:
                return comment[idx + len(marker) :].strip()
        return ""


# -- Parsing helpers --


def _extract_plan(text: str) -> str:
    for marker in ("**Summary**", "## Summary", "### Summary", "**Approach**"):
        idx = text.find(marker)
        if idx != -1:
            plan = text[idx:]
            end_idx = plan.find("[PLAN_COMPLETE]")
            if end_idx != -1:
                plan = plan[:end_idx].strip()
            return plan

    cleaned = text.replace("[PLAN_COMPLETE]", "").strip()
    return cleaned


def _parse_quality_verdict(text: str) -> str:
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            data = json.loads(match.group())
            return data.get("verdict", "fail_escalate")
    except (json.JSONDecodeError, AttributeError):
        pass
    return "fail_escalate"


def _parse_quality_feedback(text: str) -> str:
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            data = json.loads(match.group())
            return data.get("feedback", "")
    except (json.JSONDecodeError, AttributeError):
        pass
    return ""


def _parse_review_verdict(text: str) -> str:
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            data = json.loads(match.group())
            return data.get("verdict", "issues_found")
    except (json.JSONDecodeError, AttributeError):
        pass
    return "issues_found"


def _extract_pr_number(text: str) -> int | None:
    match = re.search(r"pull/(\d+)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"#(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _slugify_branch(
    issue_number: int, plan: str, pattern: str = "{issue_number}/{description}"
) -> str:
    summary = plan.split("\n")[0][:50] if plan else "implementation"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", summary).strip("-").lower()
    return pattern.format(issue_number=issue_number, description=slug)


def _parse_self_improvement(text: str) -> dict | None:
    marker = "[SELF_IMPROVEMENT]"
    idx = text.find(marker)
    if idx == -1:
        return None
    payload = text[idx + len(marker) :].strip()
    try:
        return json.loads(payload.split("\n")[0])
    except (json.JSONDecodeError, IndexError):
        for line in payload.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None


def _extract_json_array(text: str) -> list | None:
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None
