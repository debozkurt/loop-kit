# CI deployment tier — run loopkit from GitHub Actions / GitLab CI (no cluster)

> **🟢 Built (Phase 5c, 2026-06-21).** The forge's CI is the trigger, scheduler, secret store,
> identity, compute, and per-job sandbox — so the single loop runs on a real repo with **zero
> infrastructure** and almost no new code. Additive: touched none of the cloud control plane. Shipped:
> `loopkit run --from-event/--from-issue/--open-pr` + `--adapter` (glue over `parse_event` /
> `issues.fetch_issue` / `remote.sync_done(issue=N)`), `loopkit init --ci github|gitlab`, the two
> workflow templates (`examples/ci/`), and a GitLab-token fix so `glab`/git push authenticate through
> the Phase-5a hygiene. Token-free tests (`test_ci.py` + parser/fetch units). **Since live-proven** by
> a consumer repo running the full issue→draft-PR flow on GitHub-hosted runners (see *Acceptance*),
> which also drove the later `--branch` per-issue isolation + concurrency.

## The three deployment tiers (this doc adds the middle one)

| Tier | What runs | Trigger | Secrets | Isolation | For |
|---|---|---|---|---|---|
| **Local** | `loopkit run` on a laptop | a human | local env | the laptop | iterating by hand |
| **CI (this doc)** | `loopkit run` in a CI job | forge issue / cron / manual | **CI-native** (Actions/GitLab secrets or OIDC) | the **ephemeral runner** | hands-off issue→PR, no cluster |
| **Cloud fleet** | coordinator + worker Jobs on DOKS | CLI / CronJob / webhook | per-submitter resolver + **sidecar** ([`part-iii-agent-isolation.md`](part-iii-agent-isolation.md)) | namespace + container split | many concurrent runs, `evolve`, multi-tenant |

The CI tier is the **single-loop** tier — each fire is one issue → one `loopkit run` → one draft PR.
Multiple issues *can* run **concurrently** (each on its own `loopkit/issue-N` branch — see
[Multiple PRs in flight](#multiple-prs-in-flight-per-issue-branches)); they're still N independent
single-loops, not a coordinated fleet. Shared-queue / `evolve` / cross-run coordination stays the cloud
tier's job — don't try to run the *fleet* in a CI job.

## Why it's nearly free

The core is already CI-agnostic and the hard parts exist:

- The loop, adapters, gates, durability, and **`remote.sync_done`** (push branch + draft PR, with
  `Closes #N`) are forge-neutral and already shipped.
- The issue→goal mapping is **already written**: `triggers.parse_event` parses a GitHub `issues`
  payload (which Actions hands you verbatim at `$GITHUB_EVENT_PATH`), `parse_gitlab_event` the GitLab
  one, and `issues.py` fetches issues via `gh`/`glab`. CI mode is glue over these.

## New code (what shipped) 🟢

Small additions to the **single-loop `loopkit run`** path (not the fleet):

1. **`--from-event <path>`** — read a forge issue-event JSON and set `cfg.goal` from it. Reuses the
   webhook parsers via a new **`triggers.parse_event_payload(payload)`** that auto-detects the forge by
   body shape (GitLab carries a top-level `object_kind`; GitHub doesn't) — there are no HTTP headers on
   disk to read the event type from, and no signature to verify (the forge already authenticated the
   trigger). The goal is `title + "\n\n" + body` (the same builder the webhook path uses). Captures the
   issue number.
2. **`--from-issue <number>`** (+ **`--provider`**) — fetch one issue by number via a new
   **`issues.fetch_issue`** (`gh issue view` / `glab issue view --output json`, the single-object
   counterpart of `fetch_issues`). The universal/manual path (GitLab has no native issue→pipeline
   trigger; this + scheduled cover it), and a clean local convenience too.
3. **`--open-pr`** — a per-run override that flips `[remote]` on (push + **draft** PR) for this
   invocation, so the CI template is turnkey without editing the repo's `loopkit.toml`. The captured
   issue number is threaded into `remote.sync_done(issue=N)` so the PR auto-closes the issue on merge.
4. **`--adapter`** on `run` — override the configured adapter (the templates pass `claude-api`, which
   needs no binary in CI). Plus **`loopkit init --ci github|gitlab`**, which scaffolds the workflow file
   alongside the starter `loopkit.toml` + `PROMPT.md`.
5. **GitLab credential fix** (`secrets.GIT_ENV` += `GITLAB_TOKEN`; `remote.CRED_HELPER` GitHub→GitLab
   fallback) so `glab` (issue fetch + MR) and the git push authenticate through the Phase-5a hygiene —
   loopkit's own forge subprocess gets the token, the agent's scrubbed shell still gets none.
6. **`--branch <name>`** — override the configured `branch` for one run, so each issue lands on its
   own `loopkit/issue-N` branch instead of a shared one (see
   [Multiple PRs in flight](#multiple-prs-in-flight-per-issue-branches)). Mirrors `--max-iter` /
   `--adapter`; applied **before** preflight, so the override is still validated against
   `allow`/`forbid_branches` (it can't smuggle in `main`).

Everything else (the branch-only push, the held-out gate, the protected-path guard, the cost/budget
stop) applies unchanged — loopkit's safety envelope holds; the runner supplies the sandbox the cloud
tier hand-builds.

## Workflow templates loopkit ships

The canonical templates live in [`examples/ci/`](../examples/ci/) and are what `loopkit init --ci
github|gitlab` writes into a repo (a test keeps them byte-identical). The fastest path:

```bash
loopkit init --ci github     # writes .github/workflows/loopkit.yml + a starter loopkit.toml/PROMPT.md
loopkit init --ci gitlab     # writes .gitlab-ci.yml
```

**GitHub Actions** — `.github/workflows/loopkit.yml` fires on `issues: [opened, labeled]` (the job's
`if:` gates on the `loopkit` label) and takes `--from-event "$GITHUB_EVENT_PATH"`; a `workflow_dispatch`
with an issue number takes the `--from-issue` path instead. `ANTHROPIC_API_KEY` is a repo/org secret;
the push + PR use the job's scoped, ephemeral `github.token`.

> **One-time GitHub setup — let Actions open PRs.** GitHub blocks the `github.token` from *creating*
> PRs by default, **independent of** the `permissions:` block. Enable it once per repo/org:
> *Settings → Actions → General → Workflow permissions →* ☑ *Allow GitHub Actions to create and approve
> pull requests* (or `gh api -X PUT repos/<owner>/<repo>/actions/permissions/workflow -F can_approve_pull_request_reviews=true`).
> Skip it and `--open-pr` fails with `pr.failed … not permitted to create or approve pull requests`
> **even though the loop reached DONE and pushed the branch** — the run still exits 0, so check
> `gh pr list`, not just the green checkmark. (Why it's fenced separately: stops a compromised workflow
> from opening + self-approving a PR as a privilege-escalation path. Alternative: open the PR with a
> user PAT / GitHub App token instead of `github.token`.)

**GitLab CI** — `.gitlab-ci.yml` has no
native issue→pipeline trigger, so it fires on a manual *Run pipeline* (pass `ISSUE_IID`), a webhook →
trigger token, or a schedule, and takes `--from-issue "$ISSUE_IID" --provider gitlab`; supply
`ANTHROPIC_API_KEY` + a `GITLAB_TOKEN` (PAT, `api` scope) as masked CI/CD variables. The `GITLAB_TOKEN`
PAT is what authorizes the MR + push — GitLab's `CI_JOB_TOKEN` can't open MRs (no project setting to
flip; the PAT *is* the grant). There's a subscription variant too — **`gitlab-ci-claude-code.yml`**
(`claude-code` + `CLAUDE_CODE_OAUTH_TOKEN`). Both GitLab templates **self-install** their runner deps in
`before_script` — `glab` (issue fetch + MR), plus Node + the `claude` CLI for the claude-code variant —
and handle the GitLab runtime sharp edges a live run surfaced: `GIT_STRATEGY: clone`, a `git` identity,
stripping the non-pushable `CI_JOB_TOKEN` (origin + auth header) so the `GITLAB_TOKEN` PAT pushes,
materializing the base ref for the pre-push secret-scan, and (claude-code only, since the CLI refuses
`--dangerously-skip-permissions` as root) running the loop as a **non-root** user. Full list:
[`TROUBLESHOOTING.md` → GitLab CI gotchas](TROUBLESHOOTING.md#gitlab-ci--runner--push-gotchas-issuemr-worker).

Both default to `--adapter claude-api` — **the lower-friction CI choice** (`pip install` + a key, no
binary to install or auth). See [`examples/ci/README.md`](../examples/ci/README.md) for the full setup.

## Multiple PRs in flight (per-issue branches)

A repo's `loopkit.toml` names **one** fixed `branch`, so by default every run lands on the *same*
branch — fine for one PR at a time, but two issues firing at once would collide on it and clobber each
other's PR. Pass **`--branch loopkit/issue-$ISSUE`** (the shipped GitHub templates do) and each issue
gets its own branch → its own draft PR, so any number of issues can be in flight at once:

```
issue #5   → loopkit/issue-5  → PR "Closes #5"   ┐ concurrent: separate runners,
issue #6   → loopkit/issue-6  → PR "Closes #6"   ┘ separate branches, no collision
re-fire #5 → loopkit/issue-5  → updates the SAME PR (idempotent per issue)
```

`--branch` overrides `config.branch` for the run, **before** preflight, so the override is validated
like the configured value (it must match `allow_branches`, e.g. `loopkit/*`, and can't be `main`). The
**harness owns the capability; the workflow owns the naming** (`issue-$ISSUE` is the template's choice,
not loopkit's). This is still N *independent* single-loops — they don't share a queue or coordinate
(that's the cloud fleet). Each concurrent run also gets a **unique log/trace correlation id**
(`run=<state-sig>-<uuid>`), so two runs off the same base commit never intermix their lines in an
aggregated log or LangSmith project.

The same applies on **GitLab** — its template passes `--branch loopkit/issue-$ISSUE_IID` so each issue
gets its own branch → its own draft **MR**. (`--branch` is forge-neutral; only the issue identifier
differs: GitHub's `issue.number` vs GitLab's `$ISSUE_IID`.)

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

**Phase 5c — CI tier. 🟢 Built.** Independent of 5b (skills) and 6 (isolation); additive (no cloud code
touched). Built first (chosen): usable today without a cluster, the cheapest accessibility win, and the
most *teachable* realization of Ch 12 (triggers) + Ch 16 (containment) — a no-infra way a student runs
loopkit on a real repo.

## Build order (done)

1. ✅ `loopkit run` gained `--from-event` / `--from-issue` / `--open-pr` / `--adapter` (glue over
   `triggers.parse_event_payload` / `issues.fetch_issue` / `remote.sync_done(issue=N)`) → token-free
   tests (`tests/test_ci.py`: MockAgent + a canned event file; the goal is set, `--open-pr` flips remote
   on and threads the issue number, mutual-exclusion + non-issue payloads refuse cleanly).
2. ✅ Shipped the two workflow templates via `loopkit init --ci github|gitlab` **and** `examples/ci/`
   (a drift-guard test keeps them identical).
3. ✅ Docs: `examples/ci/README.md` (the using-in-CI guide), the three-tier table in the architecture
   wiki marked Built, and this doc.

## Curriculum hook (backlog)

This tier is the most accessible, no-infra realization of **Ch 12 (triggers)** + **Ch 16
(containment)** — so the three-tier model + the "use the platform's primitives first" framing should
become an **intro module on ecosystem integration** in the loops curriculum (it repairs Part III's
drop of the course's runnable-scenario teaching form). Tracked as a future-work note; build the module
from this doc's three-tier table + workflow templates.

## Acceptance

- ✅ **Token-free (met):** `loopkit run --from-event <canned GitHub issues payload>` with `MockAgent`
  builds the right goal, runs the loop, and (with `--open-pr`) calls `remote.sync_done` with the issue
  number — no network. The GitLab `--from-event`/`--from-issue` paths are mocked the same way; the
  `init --ci` scaffold + the examples drift-guard round it out (21 tests, 219 → 240 green).
- ✅ **Live (proven):** a consumer repo ran the full flow end-to-end on GitHub-hosted runners —
  labelled issue → claude-code agent → in-loop gate → DONE → pushed branch → **Actions-opened draft
  PR** that closes the issue on merge. Also exercised live: the **general (task-agnostic) gate**, the
  **`--branch` per-issue isolation**, and **concurrent** issues producing independent PRs on separate
  branches. Surfaced (and fixed) two real gaps in the process: the Actions *create-PR* permission must
  be enabled (see the setup note above), and the run correlation id had to be made unique per run.
- ✅ **Live on GitLab (proven):** the same flow ran end-to-end on a self-hosted GitLab docker runner
  (issue → claude-code agent → gate → DONE → push → **glab-opened draft MR**), plus `--branch`
  per-issue isolation and **two concurrent issues** producing independent MRs. Surfaced 7 GitLab-runner
  sharp edges (now baked into the templates + [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#gitlab-ci--runner--push-gotchas-issuemr-worker)):
  k8s-runner pids limit · claude-as-root · missing git identity · secret-scan-on-history · reused-dir
  ownership · detached base ref · `CI_JOB_TOKEN` overriding the PAT (scope **and** role both required).
- ⧗ **loopkit hardening backlog** (gaps the GitLab run exposed in core, not just templates): run the
  claude-code CLI **non-root from inside loopkit**; set a **fallback git identity** when none is
  configured; make the pre-push secret-scan **resolve/fetch its base** (or fail loud) instead of
  silently scanning full history.
