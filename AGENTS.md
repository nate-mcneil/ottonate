# Ottonate

Ottonate is an automated GitHub issue-to-PR pipeline that uses Claude agents to
take issues from planning through implementation, CI, code review, and merge
readiness. It supports a spec-driven development flow where initiatives in an
engineering repo are broken into stories across target repos.

## Quick Start

```bash
pip install -e ".[dev]"
pytest tests/ -v
ottonate run                         # Start the scheduler daemon
ottonate process owner/repo#42       # Process a single issue
ottonate dashboard                   # Start the dashboard web UI (localhost:8080)
ottonate sync-agents                 # Sync agent definitions to ~/.claude/agents/
ottonate init-engineering            # Bootstrap the engineering repo
ottonate rules-check owner/repo      # Debug merged rules for a repo
```

## Architecture

Ottonate is a single async Python process with three layers:

1. **Scheduler** (`scheduler.py`) -- polls GitHub for issues with the entry
   label (default `otto`, configurable via `OTTONATE_GITHUB_AGENT_LABEL`),
   builds `Ticket` objects, and dispatches them to the pipeline with concurrency
   control.

2. **Pipeline** (`pipeline.py`) -- a label-driven state machine. Each GitHub
   label maps to a handler that invokes a Claude agent, processes the result,
   and swaps the label to advance the issue. See `PIPELINE.md` for the full
   flow diagram.

3. **Integrations** (`integrations/`) -- thin async clients for GitHub
   (via `gh` CLI).

4. **Rules** (`rules.py`) -- three-layer rules system that loads configuration
   and context from built-in defaults, the engineering repo, and target repos.

## Directory Structure

```
ottonate/
  agents/                    # Claude agent definitions (synced to ~/.claude/agents/)
    otto-planner.md
    otto-quality-gate.md
    otto-implementer.md
    otto-ci-fixer.md
    otto-reviewer.md
    otto-review-responder.md
    otto-spec-agent.md
    otto-retro.md
  src/ottonate/
    cli.py                   # Click CLI: run, process, dashboard, sync-agents, etc.
    config.py                # Pydantic settings (env prefix: OTTONATE_)
    models.py                # Label enum, Ticket, StageResult, CIStatus, ReviewStatus
    pipeline.py              # Stage handlers and agent invocation
    scheduler.py             # GitHub polling loop with concurrency control
    prompts.py               # Prompt builders for each pipeline stage
    rules.py                 # Three-layer rules loader (built-in + org + repo)
    agents.py                # Agent definition sync (repo -> ~/.claude/agents/)
    enrichment.py            # Story enrichment (AC, tech notes, test expectations)
    traceability.py          # Artifact traceability graph (spec -> story -> PR -> tests)
    metrics.py               # Issue metrics derived from GitHub timeline + structured comments
    init_engineering.py      # Engineering repo bootstrap (scaffold + org scan)
    github.py                # GitHub via gh CLI (issue CRUD, PRs, labels, projects)
    dashboard/               # Local web dashboard (FastAPI + HTMX + Primer CSS)
      app.py                 # FastAPI app factory
      api.py                 # JSON API endpoints
      views.py               # HTML page routes
      templates/             # Jinja2 templates (base, pipeline, attention)
      static/                # CSS overrides
  tests/
    conftest.py              # Shared fixtures (config, mocks, sample_ticket)
    test_models.py
    test_pipeline.py
    test_scheduler.py
    test_enrichment.py
    test_rules.py
    test_traceability.py
    test_metrics.py
    test_init_engineering.py
    test_github.py
    test_dashboard.py
  PIPELINE.md                # Full pipeline flow documentation
  pyproject.toml
```

## Key Concepts

### Label-Driven State Machine

GitHub labels ARE the state machine. Every issue carries a permanent entry label
(default `otto`) that marks it as pipeline-eligible. A second mutable label
indicates the current stage. The `Label` enum in `models.py` defines all stage
labels. The entry label is NOT in the enum -- it is resolved at runtime from
`OttonateConfig.github_agent_label`.

### Two Entry Paths

- **Spec path** (engineering repo issues): `otto` -> `agentSpec` ->
  `agentSpecReview` -> `agentSpecApproved` -> `agentBacklogGen` ->
  `agentBacklogReview` -> stories created in target repos
- **Dev path** (target repo issues): `otto` -> `agentPlanning` ->
  `agentPlanReview` -> `agentPlan` -> `agentImplementing` -> `agentPR` ->
  `agentSelfReview` -> `agentReview` -> `agentMergeReady` ->
  (if issues detected) `agentRetro`

Any stage can move to `agentStuck` if the pipeline cannot proceed.

### Engineering Repo

A configurable repo (default name from `OTTONATE_GITHUB_ENGINEERING_REPO`)
serves as the org-level knowledge base. It contains:

- `architecture/` -- system architecture docs and repo catalog
- `specs/` -- PR-reviewed product specifications
- `decisions/` -- architecture decision records (ADRs)
- `.ottonate/` -- org-level rules (config.yml + rules.md)

### Rules System

Three layers of configuration, most specific wins:

1. **Built-in defaults** -- sensible defaults shipped with ottonate
2. **Org-level** -- `.ottonate/config.yml` and `.ottonate/rules.md` from the
   engineering repo, plus architecture docs
3. **Repo-level** -- `.ottonate/config.yml` and `.ottonate/rules.md` from each
   target repo

`config.yml` controls pipeline behavior (branch patterns, reviewer assignment).
`rules.md` is prose injected into every agent prompt for coding conventions.

### Approval Gates

All approvals are PR-based. Spec review, backlog review, and code review all
use PR merge as the approval signal. No polling for magic comment strings.

### Agent Definitions

Agent `.md` files live in `agents/` and are synced to `~/.claude/agents/` on
`run`, `process`, and `sync-agents`. They use YAML frontmatter for
configuration (model, maxTurns) and markdown for the system prompt.

### Dashboard

`ottonate dashboard` starts a local web UI (FastAPI + HTMX + Primer CSS) on
`127.0.0.1:8080`. Two views:

- **Pipeline Board** (`/`) -- kanban board grouping issues into Planning,
  Implementing, Awaiting Human, and Stuck columns.
- **Attention Queue** (`/attention`) -- prioritized list of items needing human
  action (stuck, needs merge, needs review, needs approval) with inline action
  buttons.

All issue/PR detail links open GitHub in a new tab. The dashboard auto-refreshes
via HTMX polling every 10 seconds.

### Configuration

All config is via environment variables with the `OTTONATE_` prefix, loaded by
Pydantic settings from `.env`. Key variables:

- `OTTONATE_GITHUB_ORG` -- GitHub organization name
- `OTTONATE_GITHUB_ENGINEERING_REPO` -- engineering/knowledge repo name (default `engineering`)
- `OTTONATE_GITHUB_AGENT_LABEL` -- the entry label (default `otto`)
- `OTTONATE_GITHUB_USERNAME` -- bot account username for filtering
- `OTTONATE_GITHUB_NOTIFY_TEAM` -- team/user to @mention on events

## Conventions

### Code Style

- Python 3.11+, async throughout
- Ruff for linting (line length 100)
- Type hints on all function signatures
- Minimal comments -- code should be self-explanatory
- Prefer pure SQL over ORM queries
- TDD approach: write failing tests first

### Testing

- `pytest` with `pytest-asyncio` (auto mode)
- Use `AsyncMock` for async integration clients
- Pipeline tests patch `pipeline._run` to avoid real agent invocations
- Scheduler tests patch integration constructors

### Adding a New Pipeline Stage

1. Add the label to the `Label` enum in `models.py`
2. Add it to `ACTIONABLE_LABELS` or `IN_PROGRESS_LABELS` as appropriate
3. Add a handler method `_handle_<stage>` to `Pipeline` in `pipeline.py`
4. Register it in the dispatch dict in `Pipeline.handle`
5. Add a prompt builder to `prompts.py` if the stage invokes an agent
6. Add tests in `test_pipeline.py`
7. Update `PIPELINE.md`

### Customizing for Your Org

1. Create an engineering repo with the expected structure
2. Add `.ottonate/config.yml` for pipeline settings
3. Add `.ottonate/rules.md` for coding conventions
4. Add `architecture/overview.md` and `architecture/repos.md` for system context
5. Set the `OTTONATE_*` environment variables
6. Run `ottonate run`
