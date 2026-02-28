# Ottonate

Automated GitHub issue-to-PR pipeline powered by Claude agents. Takes issues
from planning through implementation, CI, code review, and merge readiness.
Supports a spec-driven development flow where initiatives in an engineering
repo are broken into stories across target repos.

## Prerequisites

- Python 3.11+
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) installed and authenticated

## Authentication

Ottonate invokes Claude agents via the
[Claude Agent SDK](https://docs.anthropic.com/en/docs/claude-code/sdk),
which delegates to the `claude` CLI under the hood. It does not manage API keys
itself; authentication is inherited from whatever the CLI is configured with.

**Option A: Anthropic API (default)**

Authenticate the Claude Code CLI with one of:

```bash
# Interactive OAuth login (opens browser)
claude login

# Or set an API key
export ANTHROPIC_API_KEY=sk-ant-...
```

Verify with `claude --version` and a quick `claude -p "hello"` to confirm
the CLI can reach the API.

**Option B: AWS Bedrock**

If your org routes Claude through Bedrock, set the following in `.env`:

```
OTTONATE_USE_BEDROCK=true
OTTONATE_AWS_REGION=us-east-1
OTTONATE_AWS_PROFILE=your-aws-profile    # optional, uses default chain if unset
OTTONATE_BEDROCK_MODEL=us.anthropic.claude-sonnet-4-20250514
```

Standard AWS credential resolution applies (env vars, `~/.aws/credentials`,
instance profile, etc.). The pipeline sets `CLAUDE_CODE_USE_BEDROCK=1` and
passes through `AWS_REGION` and `AWS_PROFILE` to each agent invocation.

## Installation

```bash
git clone https://github.com/nate-mcneil/ottonate.git
cd ottonate
pip install -e ".[dev]"
```

## Instructions for Agents

If you are an AI agent (Claude Code, Cursor, Copilot, etc.) tasked with setting
up ottonate for a user, follow these steps:

1. **Clone and install**
   ```bash
   git clone https://github.com/nate-mcneil/ottonate.git
   cd ottonate
   pip install -e ".[dev]"
   ```

2. **Verify GitHub CLI auth**
   ```bash
   gh auth status
   ```
   If not authenticated, prompt the user to run `gh auth login` before
   continuing.

3. **Verify Claude Code CLI auth**
   ```bash
   claude --version
   claude -p "hello"
   ```
   If the CLI is not installed, prompt the user to install it
   (`npm install -g @anthropic-ai/claude-code`). If not authenticated,
   prompt the user to run `claude login` or set `ANTHROPIC_API_KEY`.
   For Bedrock setups, confirm AWS credentials are configured and set the
   `OTTONATE_USE_BEDROCK` / `OTTONATE_AWS_*` vars in `.env` (see
   Authentication section above).

4. **Run interactive setup**
   ```bash
   ottonate setup
   ```
   This walks through an 8-step onboarding flow. You will need to respond to
   prompts:
   - **Owner selection** (numbered list): pick the org or personal account
   - **Engineering repo name**: accept default `engineering` or enter a custom name
   - **Repo creation confirm**: confirm `Y` if the repo does not exist yet
   - **Entry label**: accept default `otto` or enter a custom label
   - **.env overwrite**: confirm only if the user wants to replace an existing `.env`

   The command creates the engineering repo, writes `.env`, provisions pipeline
   labels, and syncs agent definitions.

5. **Populate architecture docs**
   ```bash
   ottonate init-engineering
   ```
   This scans the org's repos and opens a PR to the engineering repo with
   auto-discovered architecture documentation.

6. **Start the pipeline**
   ```bash
   ottonate run
   ```

7. **Verify** by opening the dashboard:
   ```bash
   ottonate dashboard
   ```

After setup, the user can label any issue with `otto` (or their chosen entry
label) to feed it into the pipeline.

## Usage

```bash
ottonate setup                     # Interactive onboarding: .env, labels, engineering repo
ottonate run                       # Start the scheduler daemon
ottonate process owner/repo#42     # Push a single issue through one pipeline step
ottonate process-idea owner/repo#42  # Triage and refine a single idea issue
ottonate sync-agents               # Sync agent definitions to ~/.claude/agents/
ottonate init-engineering          # Bootstrap the engineering repo with scaffolding
ottonate dashboard                 # Start the web dashboard UI
ottonate rules-check owner/repo    # Display merged rules for a repo
```

## Configuration

All configuration is via environment variables with the `OTTONATE_` prefix,
loaded by Pydantic settings from `.env`.

| Variable | Description | Default |
|---|---|---|
| `OTTONATE_GITHUB_ORG` | GitHub organization name | |
| `OTTONATE_GITHUB_ENGINEERING_REPO` | Engineering/knowledge repo name | `engineering` |
| `OTTONATE_GITHUB_ENGINEERING_BRANCH` | Default branch of the engineering repo | `main` |
| `OTTONATE_GITHUB_AGENT_LABEL` | Entry label that marks issues for the pipeline | `otto` |
| `OTTONATE_GITHUB_USERNAME` | Bot account username (for filtering self-comments) | |
| `OTTONATE_GITHUB_NOTIFY_TEAM` | Team/user to @mention on events | |
| `OTTONATE_CLAUDE_MODEL` | Claude model to use | `sonnet` |
| `OTTONATE_USE_BEDROCK` | Use AWS Bedrock instead of direct API | `false` |
| `OTTONATE_AWS_REGION` | AWS region for Bedrock | |
| `OTTONATE_AWS_PROFILE` | AWS credentials profile | |
| `OTTONATE_BEDROCK_MODEL` | Bedrock model ID (e.g. `us.anthropic.claude-sonnet-4-20250514`) | |
| `OTTONATE_BEDROCK_SMALL_MODEL` | Bedrock model ID for fast/cheap tasks | |
| `OTTONATE_IDEAS_DIR` | Directory name for idea files in the engineering repo | `ideas` |
| `OTTONATE_MAX_CONCURRENT_TICKETS` | Max issues processed in parallel | `3` |
| `OTTONATE_POLL_INTERVAL_S` | Scheduler polling interval in seconds | `30` |
| `OTTONATE_MAX_PLAN_RETRIES` | Max retries for the planning stage | `2` |
| `OTTONATE_MAX_IMPLEMENT_RETRIES` | Max retries for the implementation stage | `2` |
| `OTTONATE_MAX_CI_FIX_RETRIES` | Max retries for CI fix attempts | `3` |
| `OTTONATE_MAX_REVIEW_RETRIES` | Max retries for review address cycles | `5` |
| `OTTONATE_RATE_LIMIT_BASE_DELAY_S` | Initial backoff delay for rate limits | `60` |
| `OTTONATE_RATE_LIMIT_MAX_DELAY_S` | Max backoff delay for rate limits | `600` |
| `OTTONATE_RATE_LIMIT_COOLDOWN_S` | Cooldown period after rate limit recovery | `300` |
| `OTTONATE_WORKSPACE_DIR` | Directory for cloned repo workspaces | `~/.ottonate/workspaces` |
| `OTTONATE_DB_PATH` | Path to SQLite metrics database | `~/.ottonate/ottonate.db` |

## How It Works

Ottonate uses **GitHub labels as a state machine**. Every issue carries a
permanent entry label (default `otto`) that marks it as pipeline-eligible.
A second, mutable label indicates the current stage. The scheduler polls
GitHub, finds actionable issues, and dispatches them to the appropriate handler.

### Pipeline Stages

**Idea path** (engineering repo, Step 0):
`otto` -> `agentIdeaTriage` -> `agentIdeaReview` ->
(if refinement needed) `agentIdeaRefining` -> spec issue created

**Spec path** (engineering repo):
`otto` -> `agentSpec` -> `agentSpecReview` -> `agentSpecApproved` ->
`agentBacklogGen` -> `agentBacklogReview` -> stories created in target repos

**Dev path** (target repos):
`otto` -> `agentPlanning` -> `agentPlanReview` -> `agentPlan` ->
`agentImplementing` -> `agentPR` -> (if CI fails) `agentCIFix` ->
`agentSelfReview` -> `agentReview` ->
(if changes requested) `agentAddressingReview` -> `agentReview` ->
`agentMergeReady` -> (if issues detected) `agentRetro`

Any stage can move to `agentStuck` if the pipeline cannot proceed without
human intervention.

See [PIPELINE.md](PIPELINE.md) for the full flow diagram and stage details.

### Agents

| Agent | Role |
|---|---|
| `otto-idea-agent` | Triages and refines raw ideas into spec-ready issues |
| `otto-spec-agent` | Generates product specifications from initiatives |
| `otto-planner` | Produces development plans and breaks specs into stories |
| `otto-quality-gate` | Evaluates plans against acceptance criteria |
| `otto-implementer` | Writes code, creates branches and PRs |
| `otto-ci-fixer` | Reads CI failure logs and pushes fixes |
| `otto-reviewer` | Self-reviews PRs against the original plan |
| `otto-review-responder` | Addresses human review comments inline |
| `otto-retro` | Runs retrospectives and proposes engineering repo improvements |

### Improvement Loops

After a PR is merged, if the issue experienced retries or got stuck at any
point, the pipeline automatically triggers a retrospective. The retro agent:

1. Analyzes pipeline metrics and review feedback
2. Proposes targeted improvements to the engineering repo (rules, architecture docs)
3. Opens a PR to the engineering repo
4. Optionally files a self-improvement issue in the ottonate repo itself

### Rules System

Three layers of configuration, most specific wins:

1. **Built-in defaults** -- sensible defaults shipped with ottonate
2. **Org-level** -- `.ottonate/config.yml` and `.ottonate/rules.md` from the
   engineering repo, plus architecture docs
3. **Repo-level** -- `.ottonate/config.yml` and `.ottonate/rules.md` from each
   target repo

### Engineering Repo

Run `ottonate init-engineering` to bootstrap the engineering repo with:

- `architecture/overview.md` and `architecture/repos.md` (auto-populated from org scan)
- `ideas/`, `specs/`, and `decisions/` directories
- `.ottonate/config.yml` and `.ottonate/rules.md` defaults

## Development

```bash
pip install -e ".[dev]"
ruff check src/ tests/             # Lint
pytest tests/ -v                   # Test
```

Tests use `pytest` with `pytest-asyncio` in auto mode. Pipeline tests patch
`pipeline._run` to avoid real agent invocations.

## License

Proprietary.
