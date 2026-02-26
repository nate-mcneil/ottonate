# Ottonate Pipeline: Initiative to Merge-Ready PR

This document describes the complete lifecycle of an initiative as it moves
through the ottonate pipeline, from initial concept to a pull request that is
approved and ready to merge.

## How It Works

Ottonate uses **GitHub labels as a state machine**. Every issue carries a
permanent entry label that marks it as pipeline-eligible. By default this label
is `otto`, but it is configurable via the `OTTONATE_GITHUB_AGENT_LABEL`
environment variable. A second, mutable label indicates the issue's current
stage. The scheduler polls GitHub on a configurable interval, finds issues with
actionable label combinations, and dispatches them to the appropriate handler.
Each handler does its work, then swaps the stage label to advance the issue.

There are two entry paths:

- **Spec path** (engineering repo issues) -- starts at spec generation,
  produces stories in target repos, then each story enters the dev path
  independently.
- **Dev path** (target repo issues) -- starts at planning, moves through
  implementation, CI, review, and merge readiness.

At any point, an issue can be moved to `agentStuck` if the pipeline cannot
proceed without human intervention.

### Rules Injection

Every agent invocation receives a `rules_context` parameter containing merged
rules from the engineering repo and the target repo. This includes architecture
docs, coding conventions, and project-specific guidance. See `rules.py` for
the three-layer merge logic.

---

## The Spec Path (Initiative to Stories)

This path takes a high-level initiative and turns it into a set of
execution-grade GitHub issues across target repos.

### 1. Spec Generation

| | |
|---|---|
| **Trigger** | Issue in the engineering repo has the entry label, no stage label |
| **Label** | `otto` -> `agentSpec` |
| **Agent** | `otto-spec-agent` |
| **What happens** | The spec agent reads the issue description, searches memory for related context, analyzes the codebase, and produces a structured product specification. A PR is opened in the engineering repo with the SPEC.md file. |
| **Next** | `agentSpecReview` |
| **Failure** | `agentStuck` if the agent signals `[SPEC_NEEDS_INPUT]` or errors |

### 2. Spec Review

| | |
|---|---|
| **Label** | `agentSpecReview` |
| **Agent** | None (human gate) |
| **What happens** | The pipeline checks if the spec PR has been merged. Merging the PR is the approval signal. |
| **Next** | `agentSpecApproved` |
| **Failure** | `agentStuck` if PR is closed without merging |

### 3. Backlog Generation

| | |
|---|---|
| **Trigger** | Spec PR has been merged |
| **Label** | `agentSpecApproved` -> `agentBacklogGen` |
| **Agent** | `otto-planner` |
| **What happens** | The planner agent reads the approved SPEC.md from main and breaks it down into a JSON array of implementation stories, each with a title, target repo, description, size estimate, dependencies, and technical notes. The generated backlog is posted as a comment. |
| **Next** | `agentBacklogReview` |
| **Failure** | `agentStuck` if generation fails or the spec cannot be located |

### 4. Backlog Review

| | |
|---|---|
| **Label** | `agentBacklogReview` |
| **Agent** | None (human gate) |
| **What happens** | The pipeline checks for a backlog PR merge or an approval comment. On approval, each story in the backlog is enriched with detailed acceptance criteria, technical notes, and test expectations, then created as a GitHub issue in the appropriate target repo. Each issue receives the entry label. If a GitHub Project is associated, issues are added to it. |
| **Next** | Stories enter the dev path independently. |
| **Failure** | `agentStuck` on rejection |

### Story Enrichment (happens during creation)

When stories are created from the backlog, each one is passed through an
enrichment step that produces markdown-formatted issue bodies containing:

- **Acceptance Criteria** -- testable conditions as checkboxes
- **Technical Notes** -- implementation guidance, API contracts
- **Test Expectations** -- specific unit, integration, and e2e tests to write
- **Estimate** -- S/M/L sizing with justification
- **Dependencies** -- references to other issues

---

## The Dev Path (Issue to Merge-Ready PR)

This path takes an individual issue from planning through to a reviewed,
CI-green pull request.

### 5. Planning

| | |
|---|---|
| **Trigger** | Issue in a target repo has the entry label, no stage label |
| **Label** | `otto` -> `agentPlanning` |
| **Agent** | `otto-planner` |
| **What happens** | The planner agent reads the issue description, searches memory for related context and repo patterns, analyzes the codebase, and produces a development plan. The plan is committed to the feature branch as PLAN.md and a comment is posted on the issue. |
| **Next** | `agentPlanReview` |
| **Failure** | `agentStuck` if the planner signals `[NEEDS_MORE_INFO]` or produces no output |

### 6. Plan Review (Quality Gate)

| | |
|---|---|
| **Label** | `agentPlanReview` |
| **Agent** | `otto-quality-gate` |
| **What happens** | An automated quality gate evaluates the plan against the issue description. It returns a JSON verdict: `pass`, `fail_retryable`, or `fail_escalate`. On retryable failure, the planner is re-invoked with the gate's feedback. |
| **Next** | `agentPlan` on pass |
| **Retry** | Back to `agentPlanning` on `fail_retryable` (up to `max_plan_retries`) |
| **Failure** | `agentStuck` on `fail_escalate` or retry limit exceeded |

### 7. Implementation

| | |
|---|---|
| **Label** | `agentPlan` -> `agentImplementing` |
| **Agent** | `otto-implementer` |
| **What happens** | The implementer agent reads the approved plan, searches memory for repo-specific patterns, creates a feature branch (name from rules config), implements the changes following TDD, and opens a pull request. The PR number is extracted and recorded. |
| **Next** | `agentPR` |
| **Failure** | `agentStuck` on `[IMPLEMENTATION_BLOCKED]` or retry limit exceeded |

### 8. CI Monitoring

| | |
|---|---|
| **Label** | `agentPR` |
| **Agent** | `otto-ci-fixer` (only if CI fails) |
| **What happens** | The pipeline checks the PR's CI status via GitHub. If all checks pass, the issue advances. If checks fail, the CI fixer agent is invoked with the failure logs. If checks are pending, re-check next cycle. |
| **Next** | `agentSelfReview` on CI pass |
| **Retry** | `agentCIFix` -> back to `agentPR` after fix (up to `max_ci_fix_retries`) |
| **Failure** | `agentStuck` on `[CI_FIX_BLOCKED]` or retry limit exceeded |

### 9. Self-Review

| | |
|---|---|
| **Label** | `agentSelfReview` |
| **Agent** | `otto-reviewer` |
| **What happens** | The reviewer agent compares the PR diff against the original plan. If clean, the issue advances to human review. If issues are found, the implementer is re-invoked with the review feedback. |
| **Next** | `agentReview` on clean verdict |
| **Retry** | `agentImplementing` -> `agentPR` if issues found |

### 10. Human Review

| | |
|---|---|
| **Label** | `agentReview` |
| **Agent** | `otto-review-responder` (only if comments exist) |
| **What happens** | The pipeline polls the GitHub PR for review status. If approved and CI is green, the issue is marked merge-ready. If changes are requested, the review responder agent addresses each comment inline, then returns to CI monitoring. |
| **Next** | `agentMergeReady` on approval + CI green |
| **Retry** | `agentAddressingReview` -> `agentPR` after responding (up to `max_review_retries`) |
| **Failure** | `agentStuck` on `[REVIEW_ESCALATE]` or retry limit exceeded |

### 11. Merge Ready

| | |
|---|---|
| **Label** | `agentMergeReady` |
| **Agent** | None (human action) |
| **What happens** | An @mention notification is posted on the issue indicating the PR is approved and CI is green. The label remains until a human merges the PR. |

---

## Label Reference

| Label | System State | Who Acts | Mutable? |
|---|---|---|---|
| `otto` (configurable) | Issue is eligible for the pipeline | -- | No (permanent flag) |
| `agentSpec` | Spec agent is generating a PRD | Agent | Yes |
| `agentSpecReview` | Spec PR is waiting for human merge | Human | Yes |
| `agentSpecApproved` | Spec merged, backlog generation starting | Agent | Yes |
| `agentBacklogGen` | Stories are being generated from the spec | Agent | Yes |
| `agentBacklogReview` | Generated stories waiting for human approval | Human | Yes |
| `agentPlanning` | Planner agent is writing a dev plan | Agent | Yes |
| `agentPlanReview` | Quality gate is evaluating the plan | Agent | Yes |
| `agentPlan` | Plan approved, ready for implementation | Agent | Yes |
| `agentImplementing` | Implementer agent is coding and creating PR | Agent | Yes |
| `agentPR` | PR exists, CI status is being monitored | Agent | Yes |
| `agentCIFix` | CI fixer agent is addressing failures | Agent | Yes |
| `agentSelfReview` | Reviewer agent is checking PR against plan | Agent | Yes |
| `agentReview` | PR is waiting for human code review | Human | Yes |
| `agentAddressingReview` | Review responder is addressing comments | Agent | Yes |
| `agentMergeReady` | PR approved + CI green, waiting for merge | Human | Yes |
| `agentStuck` | Pipeline cannot proceed, needs human help | Human | Yes |

---

## State Diagram

```
                          SPEC PATH
                          =========

  [Initiative Issue in engineering repo]
        |
        v
    agentSpec  ---------> agentStuck
        |
        v
   agentSpecReview -----> agentStuck (PR closed)
        |  (PR merged)
        v
  agentSpecApproved
        |
        v
   agentBacklogGen -----> agentStuck
        |
        v
  agentBacklogReview ---> agentStuck (rejected)
        |  (approved)
        v
  [Issues Created in Target Repos]
        |
        v
  Each issue enters dev path


                          DEV PATH
                          ========

  [Issue with entry label in target repo]
        |
        v
   agentPlanning -------> agentStuck
        |
        v
   agentPlanReview -----> agentStuck (escalated / retries exceeded)
        |  |
        |  +-- fail_retryable --> agentPlanning (loop)
        |
        v  (pass)
     agentPlan
        |
        v
  agentImplementing ----> agentStuck
        |
        v
     agentPR
        |
        +-- pending ----> (wait, re-check next cycle)
        |
        +-- failed -----> agentCIFix --> agentPR (loop)
        |                     |
        |                     +--------> agentStuck (retries exceeded)
        v  (passed)
  agentSelfReview
        |
        +-- issues -----> agentImplementing --> agentPR (loop)
        |
        v  (clean)
    agentReview
        |
        +-- pending ----> (wait, re-check next cycle)
        |
        +-- comments ---> agentAddressingReview --> agentPR (loop)
        |                     |
        |                     +-------------------> agentStuck (retries exceeded)
        v  (approved + CI green)
   agentMergeReady
        |
        v
  [Human merges PR]
```

---

## Retry Limits

| Stage | Config Key | Default |
|---|---|---|
| Plan revision | `max_plan_retries` | 2 |
| Implementation | `max_implement_retries` | 2 |
| CI fix | `max_ci_fix_retries` | 3 |
| Review response | `max_review_retries` | 5 |

When a retry limit is exceeded, the issue moves to `agentStuck`.

---

## Agents Involved

| Agent | Role |
|---|---|
| `otto-spec-agent` | Generates product specifications from initiatives |
| `otto-planner` | Produces development plans and breaks specs into stories |
| `otto-quality-gate` | Evaluates plans against acceptance criteria |
| `otto-implementer` | Writes code, creates branches and PRs |
| `otto-ci-fixer` | Reads CI failure logs and pushes fixes |
| `otto-reviewer` | Self-reviews PRs against the original plan |
| `otto-review-responder` | Addresses human review comments inline |

---

## Traceability

The pipeline maintains a traceability graph that links artifacts across the
full delivery chain:

```
Spec -> Project -> Issue -> Plan -> PR -> Tests
```

At any point you can query the graph to see coverage: how many issues have
PRs, how many have tests, and trace any artifact back to the spec that
originated it.

---

## Human Touchpoints

The pipeline is designed to be autonomous but includes deliberate human gates:

1. **Spec Review** -- humans merge (or close) the spec PR
2. **Backlog Review** -- humans approve the generated stories
3. **Code Review** -- humans review the PR on GitHub
4. **Merge** -- humans perform the final merge

Everything between these gates is automated.

---

## Rules System

Ottonate supports a three-layer rules system for customization:

### Layers

1. **Built-in defaults** -- sensible defaults (branch naming, commit format)
2. **Org-level** -- from `{engineering_repo}/.ottonate/config.yml` and `rules.md`
3. **Repo-level** -- from `{target_repo}/.ottonate/config.yml` and `rules.md`

### config.yml (machine-readable)

```yaml
branch_pattern: "{issue_number}/{description}"
commit_format: "#{issue_number} - {description}"
notify_team: "engineering"
required_reviewers:
  default: []
  paths:
    "src/db/migrations/**": ["dba-team"]
labels:
  entry: "otto"
```

### rules.md (agent context)

```markdown
# Project Rules

## Stack
- TypeScript, Node.js 20, Express

## Testing
- Every API endpoint must have integration tests
- Use factories for test data, not fixtures
```

Org-level rules are prepended; repo-level rules are appended. Combined
context is injected into every agent prompt.
