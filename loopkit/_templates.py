"""Scaffolding templates emitted by `loopkit init` — pure data, no logic.

Extracted from `cli.py` so the command module stays command logic, not ~220 lines of embedded
YAML/TOML/Markdown blobs. Nothing here imports anything: these are the literal file bodies that
`init` writes (and that `examples/ci/` mirrors — `tests/test_ci.py` asserts byte-for-byte equality
between these constants and the shipped example files, so edit both together or the drift test fails).

`cli.py` re-exports every name from here, so existing imports of `loopkit.cli._CONFIG_TEMPLATE`
(and the `_CI_*_TEMPLATE` constants the CI tests import) keep resolving unchanged.
"""
from __future__ import annotations

_CONFIG_TEMPLATE = """\
# loopkit.toml — the whole loop as one object. Validate with `loopkit doctor`.
goal = "Describe exactly what 'done' means — the condition the loop drives toward."
repo = "."
branch = "loopkit/run"           # never main/master (Ch 16)

[agent]
adapter = "claude-code"          # mock | claude-code | codex | claude-api | openai-api
max_cost_usd = 5.0               # budget ceiling (Ch 14) — bites on real cost (see `doctor`)

[prompt]
anchors = ["PROMPT.md"]          # fixed context reloaded each tick (Ch 4-5)

[gate]
iteration = "python -m pytest tests/seen -q"      # fast, in-sample (Ch 6-7)
acceptance = "python -m pytest tests/holdout -q"  # held-out, run once before done (Ch 9)

[stops]
max_iter = 20                    # Ch 13
no_progress_after = 3

[safety]
protected_paths = ["tests/"]     # the loop may not touch these (Ch 9 + 16)
require_clean_tree = true
allow_branches = ["loopkit/*"]

# [remote]                       # opt-in OUTWARD edge (Ch 16): at DONE, push the branch + open a draft PR.
# enabled = true                 # OFF by default — nothing leaves your machine. Needs gh/glab authed.
# open_pr = true                 # one-run alternative (no block): `loopkit run --open-pr`
"""

_PROMPT_TEMPLATE = """\
# Task

<Describe the goal and state the spec precisely.>

The visible tests are an incomplete check — passing them is necessary but not sufficient.
Make the behaviour correct. Do not weaken, delete, or skip any test.
"""

# Plan-driven backlog mode (shape #2): `loopkit init --plan` scaffolds these three files instead of the
# single-task pair above. One loop grinds a markdown checklist, one item per tick — the plan file is
# both a prompt anchor the agent maintains AND the loop's completion signal (`[plan] file`, loop.py).
_PLAN_CONFIG_TEMPLATE = """\
# loopkit.toml — plan-driven backlog mode. One loop works through IMPLEMENTATION_PLAN.md, one item a
# tick, committing + verifying as it goes; DONE = every item checked AND the acceptance gate passes.
goal = "Complete every item in IMPLEMENTATION_PLAN.md, verifying each one as you go."
repo = "."
branch = "loopkit/plan"          # never main/master (Ch 16)

[agent]
adapter = "claude-code"          # mock | claude-code | codex | claude-api | openai-api
max_cost_usd = 20.0              # a whole backlog costs more than one task — raise the ceiling (Ch 14)

[prompt]
anchors = ["PROMPT.md", "IMPLEMENTATION_PLAN.md"]   # the plan is an anchor the agent reads + maintains

[plan]
file = "IMPLEMENTATION_PLAN.md"  # the checklist; the run is NOT done while any `- [ ]` item is open

[gate]
iteration  = "python -m pytest -q"          # fast, per-tick: the item just finished broke nothing
acceptance = "python -m pytest tests/ -q"   # held-out, whole-project: the finished checklist must pass this

[stops]
max_iter = 60                    # a backlog needs many more ticks than one task (Ch 13)
no_progress_after = 4
plan_stall_after = 8             # halt if no checklist item completes for 8 ticks (a wedged item
                                 # still churns files, so no_progress can't see it — this watches the
                                 # done-count). Raise it if your items are large; keep it < max_iter.

[safety]
protected_paths = ["tests/"]     # the loop may not touch these (Ch 9 + 16)
require_clean_tree = true
allow_branches = ["loopkit/*"]

# [remote]                       # opt-in OUTWARD edge (Ch 16): at DONE, push the branch + open one draft
# enabled = true                 # PR. OFF by default — the whole backlog lands as a single review.
# open_pr = true
"""

_PLAN_PROMPT_TEMPLATE = """\
<!--
  PROMPT.md — the FIXED anchor prompt for plan-driven backlog mode (Ch 4-5). Piped to the agent
  verbatim every tick; it does NOT change between ticks. The loop's memory lives on disk —
  IMPLEMENTATION_PLAN.md, git history, specs/ — not in this prompt or a growing conversation.
-->

# Your task this iteration

You are one iteration of an autonomous loop working through a checklist. You have a **fresh context** —
everything you need is on disk, not in your memory. Do exactly one item, verify it, record it, and
stop. The loop invokes you again with a fresh context for the next item.

## Orient (read these first — they are your only memory)

1. Read `IMPLEMENTATION_PLAN.md` — the checklist. `- [ ]` is open, `- [x]` is done: what is left to
   build, and the record of what is finished.
2. Read `specs/` if present — the source of truth for *what* to build and what "done" means per item.
3. Read `AGENTS.md` / `CLAUDE.md` if present — house rules, build/test commands, hard constraints.
4. If a `## Verification feedback` section is appended below, the last tick's gate failed — that is
   your error signal. Fix the cause it names before anything else.

## Do ONE item

- Pick the single most important **open** (`- [ ]`) item. Do only that item. Resist "just one more
  thing" — large changes per tick reintroduce the long-context problems this loop avoids (Ch 5).

## Verify it end to end (Ch 7)

- Run the build, tests, typechecker, linter — whatever proves the item actually works, not just that
  it compiles. If it does not pass, fix it now, this tick, while the producing context is fresh.
- Do not weaken, delete, or skip tests to make a check pass — the loop catches that (Ch 9).

## Record progress (Ch 15)

- In `IMPLEMENTATION_PLAN.md`: change the item you finished from `- [ ]` to `- [x]`, note anything the
  next tick needs, and **prune** resolved notes so the plan stays small. Add a new `- [ ]` item for
  any required work you discovered.

## Stop

- When your one item is done and verified, stop. The loop commits, starts the next tick, and once the
  checklist is empty runs the whole-project acceptance gate before declaring DONE. One verified item.
"""

_PLAN_IMPLEMENTATION_TEMPLATE = """\
# Implementation plan

The loop reads this file every tick, does the single most important **open** item, verifies it, marks
it `- [x]`, and stops — repeating with a fresh context until every item is checked and the acceptance
gate passes. Keep it small and current: mark done, prune resolved notes, add sub-tasks you discover.

`- [ ]` = open (to do)   ·   `- [x]` = done

## Requirements

- [ ] <first requirement — state precisely what "done" means for it>
- [ ] <second requirement>
- [ ] <third requirement>

## Notes / discovered work

<the loop appends what the next iteration needs here; prune it as items resolve>
"""

# CI deployment tier (Phase 5c): run the single loop from the forge's CI on a labelled issue, no
# cluster. The forge is the trigger, the secret store, the identity, and the per-job sandbox; loopkit
# is just the loop. These are the canonical templates `loopkit init --ci <forge>` scaffolds and
# `examples/ci/` mirrors — see docs/part-iii-ci-mode.md. Requires a loopkit.toml in the repo.
_CI_GITHUB_TEMPLATE = """\
# loopkit CI tier — turn a labelled issue into a draft PR, no cluster required.
# Setup: drop this at .github/workflows/loopkit.yml, add the repo secret ANTHROPIC_API_KEY, and keep
# a loopkit.toml in the repo (run `loopkit init`). Label an issue `loopkit` to dispatch the loop;
# request changes on a loopkit PR to dispatch a revise run (the loop resumes the PR's branch).
# One-time: enable Settings → Actions → General → "Allow GitHub Actions to create and approve pull
# requests", else --open-pr fails after the loop reaches DONE (check `gh pr list`, not just the ✓).
name: loopkit
on:
  issues:
    types: [opened, labeled]
  pull_request_review:
    types: [submitted]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number to run loopkit on
        required: true
permissions:
  contents: write          # push the loop's branch
  pull-requests: write     # open the draft PR
  issues: read             # read the issue (manual-dispatch path)
jobs:
  loopkit:
    # Act on labelled issues (the opt-in switch), manual runs, and changes-requested reviews on the
    # loop's OWN PRs — the branch prefix is the containment: the loop only revises PRs it authored.
    if: >-
      github.event_name == 'workflow_dispatch'
      || contains(github.event.issue.labels.*.name, 'loopkit')
      || (github.event_name == 'pull_request_review'
          && github.event.review.state == 'changes_requested'
          && startsWith(github.event.pull_request.head.ref, 'loopkit/'))
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # full history so the loop can branch from + PR against the base
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      # loopkit isn't on PyPI yet — private repo? swap this for the README "Installing loopkit" line
      - run: pip install 'loopkit[claude]'                 # claude-api adapter → the anthropic SDK
      - name: loopkit run (issue event)
        if: github.event_name == 'issues'
        # --branch loopkit/issue-N: each issue gets its own branch + PR (concurrent issues don't collide)
        run: loopkit run --from-event "$GITHUB_EVENT_PATH" --branch "loopkit/issue-${{ github.event.issue.number }}" --adapter claude-api --open-pr
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}   # a repo/org secret (per-repo keying)
          GH_TOKEN: ${{ github.token }}                          # scoped, ephemeral — pushes + opens the PR
      - name: loopkit run (revise — a review requested changes)
        if: github.event_name == 'pull_request_review'
        # No --branch: the run resumes the PR's own head branch from the event; the push updates the PR.
        run: loopkit run --from-event "$GITHUB_EVENT_PATH" --adapter claude-api --open-pr
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GH_TOKEN: ${{ github.token }}
      - name: loopkit run (manual dispatch)
        if: github.event_name == 'workflow_dispatch'
        run: loopkit run --from-issue "${{ inputs.issue }}" --branch "loopkit/issue-${{ inputs.issue }}" --adapter claude-api --open-pr
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GH_TOKEN: ${{ github.token }}
"""

# Subscription variant: `claude-code` billed to a Claude Code OAuth token, not a metered API key.
# Shipped as examples/ci/github-actions-claude-code.yml (a drift test keeps them identical).
_CI_GITHUB_CLAUDE_CODE_TEMPLATE = """\
# loopkit CI tier (Claude Code subscription) — a labelled issue → a draft PR, no cluster required.
# This variant bills your Claude Code SUBSCRIPTION via an OAuth token, not a metered API key.
# Setup:
#   1. Create the token:    claude setup-token
#   2. Add the repo secret: gh secret set CLAUDE_CODE_OAUTH_TOKEN   (do NOT set ANTHROPIC_API_KEY —
#      claude-code defaults to the subscription and withholds an API key)
#   3. Let Actions open PRs (one-time): Settings → Actions → General → Workflow permissions →
#      "Allow GitHub Actions to create and approve pull requests" (else --open-pr fails after DONE).
#   4. Keep a loopkit.toml in the repo (run `loopkit init`). Label an issue `loopkit` to dispatch;
#      request changes on a loopkit PR to dispatch a revise run (the loop resumes the PR's branch).
name: loopkit
on:
  issues:
    types: [opened, labeled]
  pull_request_review:
    types: [submitted]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number to run loopkit on
        required: true
permissions:
  contents: write          # push the loop's branch
  pull-requests: write     # open the draft PR
  issues: read             # read the issue (manual-dispatch path)
jobs:
  loopkit:
    # Act on labelled issues (the opt-in switch), manual runs, and changes-requested reviews on the
    # loop's OWN PRs — the branch prefix is the containment: the loop only revises PRs it authored.
    if: >-
      github.event_name == 'workflow_dispatch'
      || contains(github.event.issue.labels.*.name, 'loopkit')
      || (github.event_name == 'pull_request_review'
          && github.event.review.state == 'changes_requested'
          && startsWith(github.event.pull_request.head.ref, 'loopkit/'))
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # full history so the loop can branch from + PR against the base
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: npm install -g @anthropic-ai/claude-code      # the agent binary (claude-code adapter)
      # loopkit isn't on PyPI yet — private repo? swap this for the README "Installing loopkit" line
      - run: pip install loopkit                           # claude-code is a CLI adapter — no provider SDK
      - name: loopkit run (issue event)
        if: github.event_name == 'issues'
        # --branch loopkit/issue-N: each issue gets its own branch + PR (concurrent issues don't collide)
        run: loopkit run --from-event "$GITHUB_EVENT_PATH" --branch "loopkit/issue-${{ github.event.issue.number }}" --adapter claude-code --open-pr
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}   # subscription, not a billed key
          GH_TOKEN: ${{ github.token }}                                     # scoped, ephemeral — push + PR
      - name: loopkit run (revise — a review requested changes)
        if: github.event_name == 'pull_request_review'
        # No --branch: the run resumes the PR's own head branch from the event; the push updates the PR.
        run: loopkit run --from-event "$GITHUB_EVENT_PATH" --adapter claude-code --open-pr
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          GH_TOKEN: ${{ github.token }}
      - name: loopkit run (manual dispatch)
        if: github.event_name == 'workflow_dispatch'
        run: loopkit run --from-issue "${{ inputs.issue }}" --branch "loopkit/issue-${{ inputs.issue }}" --adapter claude-code --open-pr
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          GH_TOKEN: ${{ github.token }}
"""

_CI_GITLAB_TEMPLATE = """\
# loopkit CI (GitLab) — one issue -> one draft MR, no cluster.
# Trigger: "Run pipeline" / webhook trigger / schedule, with an ISSUE_IID variable.
# Vars (masked, Protected=OFF unless your branch is protected):
#   ANTHROPIC_API_KEY  — pays the agent (claude-api).
#   GITLAB_TOKEN       — PAT, scopes `api` + `write_repository`, owner with >= Developer role HERE.
#                        Scope AND role are both required, or the push 403s "not allowed to upload code".
#                        Authorizes glab (issue fetch + MR) + the git push; CI_JOB_TOKEN can do neither.
#                        To not clobber a shared GITLAB_TOKEN, set LOOPKIT_GITLAB_PAT (remapped below).
# Runner: docker-executor. A low-pids k8s runner breaks curl DNS ("getaddrinfo() thread failed to start").
# Repo: a loopkit.toml with [remote] pr_base = your default branch.
loopkit:
  image: python:3.13                  # non-slim: has git + curl
  variables: { GIT_STRATEGY: clone }  # fresh clone each run
  rules:
    - if: '$CI_PIPELINE_SOURCE == "web" && $ISSUE_IID'
    - if: '$CI_PIPELINE_SOURCE == "trigger" && $ISSUE_IID'
    - if: '$CI_PIPELINE_SOURCE == "schedule" && $ISSUE_IID'
  before_script:
    - GLAB_VERSION=1.105.0                                     # glab: issue fetch + MR
    - curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.deb" -o /tmp/glab.deb
    - dpkg -i /tmp/glab.deb
    # loopkit isn't on PyPI yet — private repo? swap this for the README "Installing loopkit" line
    - pip install 'loopkit[claude]'                           # claude-api: anthropic SDK
    - git config --system user.name  'loopkit-bot'            # loopkit commits each tick -> needs identity
    - git config --system user.email 'loopkit-bot@users.noreply.gitlab.com'
    - git remote set-url origin "${CI_SERVER_URL}/${CI_PROJECT_PATH}.git"   # drop CI_JOB_TOKEN from origin
    - for k in $(git config --local --name-only --get-regexp 'extraheader' || true); do git config --local --unset-all "$k" || true; done  # + its auth header -> push uses GITLAB_TOKEN
    - git fetch --depth 50 origin "$CI_DEFAULT_BRANCH"        # materialize base ref so the pre-push
    - git branch -f "$CI_DEFAULT_BRANCH" FETCH_HEAD || true   #   secret-scan diffs it (else scans history)
  script:
    - '[ -n "${LOOPKIT_GITLAB_PAT:-}" ] && export GITLAB_TOKEN="$LOOPKIT_GITLAB_PAT" || true'
    - loopkit run --from-issue "$ISSUE_IID" --branch "loopkit/issue-$ISSUE_IID" --provider gitlab --adapter claude-api --open-pr
"""

_CI_GITLAB_CLAUDE_CODE_TEMPLATE = """\
# loopkit CI (GitLab, Claude Code subscription) — one issue -> one draft MR, no cluster. Bills your sub.
# Trigger: "Run pipeline" / webhook trigger / schedule, with an ISSUE_IID variable.
# Vars (masked, Protected=OFF unless your branch is protected):
#   CLAUDE_CODE_OAUTH_TOKEN — `claude setup-token`. Do NOT also set ANTHROPIC_API_KEY.
#   GITLAB_TOKEN            — PAT, scopes `api` + `write_repository`, owner with >= Developer role HERE
#                             (scope AND role, or push 403s). Or set LOOPKIT_GITLAB_PAT (remapped below).
# Runner: docker-executor. A low-pids k8s runner breaks curl DNS ("getaddrinfo() thread failed to start").
# Repo: loopkit.toml with adapter=claude-code, args=["--dangerously-skip-permissions"], [remote] pr_base=default branch.
loopkit:
  image: python:3.13
  variables: { GIT_STRATEGY: clone }
  rules:
    - if: '$CI_PIPELINE_SOURCE == "web" && $ISSUE_IID'
    - if: '$CI_PIPELINE_SOURCE == "trigger" && $ISSUE_IID'
    - if: '$CI_PIPELINE_SOURCE == "schedule" && $ISSUE_IID'
  before_script:
    - curl -fsSL https://deb.nodesource.com/setup_20.x | bash -   # node 20 (claude CLI needs >= 18)
    - apt-get install -y nodejs
    - npm install -g @anthropic-ai/claude-code                    # the agent binary
    - GLAB_VERSION=1.105.0
    - curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.deb" -o /tmp/glab.deb
    - dpkg -i /tmp/glab.deb
    # loopkit isn't on PyPI yet — private repo? swap this for the README "Installing loopkit" line
    - pip install loopkit                                        # claude-code: no provider SDK
    - git config --system user.name  'loopkit-bot'
    - git config --system user.email 'loopkit-bot@users.noreply.gitlab.com'
    - git remote set-url origin "${CI_SERVER_URL}/${CI_PROJECT_PATH}.git"   # drop CI_JOB_TOKEN from origin
    - for k in $(git config --local --name-only --get-regexp 'extraheader' || true); do git config --local --unset-all "$k" || true; done  # + its auth header -> push uses GITLAB_TOKEN
    - git fetch --depth 50 origin "$CI_DEFAULT_BRANCH"          # materialize base ref for the pre-push
    - git branch -f "$CI_DEFAULT_BRANCH" FETCH_HEAD || true     #   secret-scan (else it scans history)
    # claude CLI refuses --dangerously-skip-permissions as ROOT -> run the loop as a non-root user
    - git config --system --add safe.directory "$CI_PROJECT_DIR"
    - useradd -m -u 1001 lk && chown -R lk:lk "$CI_PROJECT_DIR"
  script:
    - '[ -n "${LOOPKIT_GITLAB_PAT:-}" ] && export GITLAB_TOKEN="$LOOPKIT_GITLAB_PAT" || true'
    - su lk -c 'cd "$CI_PROJECT_DIR" && loopkit run --from-issue "$ISSUE_IID" --branch "loopkit/issue-$ISSUE_IID" --provider gitlab --adapter claude-code --open-pr'
"""

_CI_TEMPLATES = {"github": (".github/workflows/loopkit.yml", _CI_GITHUB_TEMPLATE),
                 "gitlab": (".gitlab-ci.yml", _CI_GITLAB_TEMPLATE)}
