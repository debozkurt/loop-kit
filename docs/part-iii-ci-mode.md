# CI deployment tier — run loopkit from GitHub Actions / GitLab CI (no cluster)

> **Designed, NOT built — the next-session plan (build this BEFORE agent isolation).** The forge's CI
> becomes the trigger, scheduler, secret store, identity, compute, and per-job sandbox — so the single
> loop runs on a real repo with **zero infrastructure** and almost no new code. Additive: touches none
> of the cloud control plane.

## The three deployment tiers (this doc adds the middle one)

| Tier | What runs | Trigger | Secrets | Isolation | For |
|---|---|---|---|---|---|
| **Local** | `loopkit run` on a laptop | a human | local env | the laptop | iterating by hand |
| **CI (this doc)** | `loopkit run` in a CI job | forge issue / cron / manual | **CI-native** (Actions/GitLab secrets or OIDC) | the **ephemeral runner** | hands-off issue→PR, no cluster |
| **Cloud fleet** | coordinator + worker Jobs on DOKS | CLI / CronJob / webhook | per-submitter resolver + **sidecar** ([`part-iii-agent-isolation.md`](part-iii-agent-isolation.md)) | namespace + container split | many concurrent runs, `evolve`, multi-tenant |

The CI tier is the **single-loop** tier — one issue → one `loopkit run` → one draft PR. The *fleet*
(concurrent/`evolve`/shared-queue) stays the cloud tier's job; don't try to run the fleet in a CI job.

## Why it's nearly free

The core is already CI-agnostic and the hard parts exist:

- The loop, adapters, gates, durability, and **`remote.sync_done`** (push branch + draft PR, with
  `Closes #N`) are forge-neutral and already shipped.
- The issue→goal mapping is **already written**: `triggers.parse_event` parses a GitHub `issues`
  payload (which Actions hands you verbatim at `$GITHUB_EVENT_PATH`), `parse_gitlab_event` the GitLab
  one, and `issues.fetch_issues` fetches by number via `gh`/`glab`. CI mode is glue over these.

## New code (minimal — ~one session)

Three small additions to the **single-loop `loopkit run`** path (not the fleet):

1. **`--from-event <path>`** — read a forge issue-event JSON and set `cfg.goal` from it. Reuses
   `parse_event` (GitHub) / `parse_gitlab_event` (GitLab); the goal is `title + "\n\n" + body` (the
   exact builder `event_to_run_spec` already uses). Captures the issue number.
2. **`--from-issue <number>`** — fetch one issue by number via `gh`/`glab` (reuse `issues.py`) and set
   the goal. The universal/manual path (GitLab has no native issue→pipeline trigger; this + scheduled
   cover it), and a clean local convenience too.
3. **`--open-pr`** — a per-run override that enables `remote` (push + **draft** PR) for this invocation,
   so the CI template is turnkey without editing the repo's `loopkit.toml`. Pass the captured issue
   number into `remote.sync_done(issue=N)` so the PR auto-closes the issue on merge.

Everything else (the branch-only push, the held-out gate, the protected-path guard, the cost/budget
stop) already applies unchanged — loopkit's safety envelope holds; the runner supplies the sandbox the
cloud tier hand-builds.

## Workflow templates loopkit ships (copied into the user's repo)

**GitHub Actions** — `.github/workflows/loopkit.yml` (the clean, native path):

```yaml
on:
  issues: { types: [opened, labeled] }
  workflow_dispatch: { inputs: { issue: { required: false } } }
permissions: { contents: write, pull-requests: write, issues: read }   # push + draft PR
jobs:
  loopkit:
    if: github.event_name != 'issues' || contains(github.event.issue.labels.*.name, 'loopkit')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: '3.13' }
      - run: pip install 'loopkit[claude,remote]'
      - run: loopkit run --from-event "$GITHUB_EVENT_PATH" --adapter claude-api --open-pr
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}   # a repo/org secret (per-repo keying)
          GH_TOKEN: ${{ github.token }}                          # scoped, ephemeral — pushes + opens the PR
```

**GitLab CI** — `.gitlab-ci.yml` (GitLab has **no native issue→pipeline trigger**, so this is manual +
scheduled + optional webhook-trigger, documented honestly):

```yaml
loopkit:
  image: python:3.13-slim
  rules:
    - if: '$CI_PIPELINE_SOURCE == "web"'        # manual run, pass ISSUE_IID as a variable
    - if: '$CI_PIPELINE_SOURCE == "trigger"'    # webhook → trigger token (issue payload available)
  script:
    - pip install 'loopkit[openai,remote]'      # or [claude]; claude-api needs no binary in CI
    - loopkit run --from-issue "$ISSUE_IID" --adapter claude-api --open-pr
  # ANTHROPIC_API_KEY + GITLAB_TOKEN as masked CI/CD variables
```

(claude-code works in either if you install the `claude` binary + auth it; **`claude-api` is the
lower-friction default in CI** — `pip install` + a key, no binary.)

## Secrets & identity (the tier's whole appeal)

- **Secrets are CI-native** — Actions/GitLab masked secrets or OIDC. **No resolver, no k8s Secrets, no
  shred** — that complexity is the *cloud* tier's, and it stays there. The cloud tier keeps the
  per-submitter resolver + the sidecar split; the CI tier deliberately doesn't.
- **Identity / cost attribution is per-repo, not per-submitter.** CI secrets are repo/env-scoped, so a
  run spends the *repo's* key, attributed to the run. Per-submitter cost-capping is a cloud-tier
  feature; document the difference rather than fake it.
- **Containment is the runner.** Each CI job is a throwaway sandbox — the Ch 16 blast-radius isolation
  is provided by the forge, not hand-built. loopkit's own controls (protected paths, branch-only,
  draft PR, held-out gate) still apply.

## Where it slots

**Phase 5c — CI tier.** Independent of 5b (skills) and 6 (isolation); additive (no cloud code touched).
**Build it first** (chosen): it's usable today without a cluster and is the cheapest accessibility win,
and it's the most *teachable* realization of Ch 12 (triggers) + Ch 16 (containment) — a no-infra way a
student runs loopkit on a real repo.

## Build order

1. `loopkit run` gains `--from-event` / `--from-issue` / `--open-pr` (glue over `parse_event` /
   `issues.fetch_issues` / `remote.sync_done(issue=N)`) → token-free tests (MockAgent + a canned event
   file; assert the goal + that `--open-pr` flips remote on and threads the issue number).
2. Ship the two workflow templates (a `loopkit init --ci [github|gitlab]` scaffold, or `examples/ci/`).
3. Docs: a `USING-IN-CI.md` (or a section in `USING-ON-YOUR-REPO.md`) + the three-tier table in the
   architecture wiki.

## Curriculum hook (backlog)

This tier is the most accessible, no-infra realization of **Ch 12 (triggers)** + **Ch 16
(containment)** — so the three-tier model + the "use the platform's primitives first" framing should
become an **intro module on ecosystem integration** in the loops curriculum (it repairs Part III's
drop of the course's runnable-scenario teaching form). Tracked as a future-work note; build the module
from this doc's three-tier table + workflow templates.

## Acceptance

- **Token-free:** `loopkit run --from-event <canned GitHub issues payload>` with `MockAgent` builds the
  right goal, runs the loop, and (with `--open-pr`) calls `remote.sync_done` with the issue number — no
  network. GitLab `--from-issue` path mocked the same way.
- **Live (optional):** drop the template into a throwaway repo, label an issue `loopkit`, watch Actions
  open a draft PR that closes the issue on merge.
