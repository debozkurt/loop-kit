# Troubleshooting & FAQ

Real failure modes and what they mean. Most are loopkit's safety machinery working as intended.

## `cost: $0.00` on a real run

**Usually fine — it means a subscription, not a free lunch.** On the Claude Code *subscription*,
`claude` reports `total_cost_usd` as 0 (usage is drawn from the plan, not billed per token), so the
loop logs `$0`. The work was real; the dollar figure just isn't meaningful.

- **Consequence:** the dollar budget ceiling (`max_cost_usd`) can't bite when the cost reads 0. **Bound
  the run with `--max-iter`** instead.
- **If you expected an API-billed number and got 0:** older loopkit builds also failed to parse the
  CLI's *array* output and read `$0` even on the API. Update loopkit (the parser now handles the array)
  and confirm the billing path with `loopkit doctor`. See [`BILLING.md`](BILLING.md).

## `pr.failed … GitHub Actions is not permitted to create or approve pull requests`

The loop reached **DONE and pushed the branch**, but the `--open-pr` step couldn't open the PR. This is
a GitHub **repo/org policy**, *separate from* the workflow's `permissions:` block: the `github.token`
is fenced off from creating PRs by default (so a compromised workflow can't open + self-approve one).

- **Fix (one-time per repo):** *Settings → Actions → General → Workflow permissions →* ☑ *Allow GitHub
  Actions to create and approve pull requests*, or
  `gh api -X PUT repos/<owner>/<repo>/actions/permissions/workflow -F can_approve_pull_request_reviews=true`.
- **Or** open the PR with a **user PAT / GitHub App token** instead of `github.token` (when org policy
  won't let you flip the setting).
- The branch is already pushed, so nothing is lost — re-run, or open the PR by hand from that branch.
- **GitLab parallel:** there's no setting to flip — GitLab's `CI_JOB_TOKEN` can't open MRs at all, so
  the template requires a `GITLAB_TOKEN` PAT with **`api`** scope (that PAT *is* the authorization). An
  MR-create failure on GitLab means a missing/under-scoped `GITLAB_TOKEN`, not a project toggle.

## A CI run is green but no PR appeared

loopkit exits **0 on `DONE` regardless of whether the outward push/PR succeeded**, so the Actions ✓ can
hide a failed PR step. Almost always the PR-permission policy above (look for `pr.failed` in the log).
**Confirm the deliverable with `gh pr list`, not the job's green check** — the ephemeral runner is gone,
so the only evidence a run produced anything is what left it: a pushed branch + an opened PR.

## `agent.done ok=False rc=1` (the agent failed a tick)

The agent's CLI (`claude`/`codex`) exited non-zero. The loop continues — a bad tick feeds back and the
next tick retries — but if it repeats, check:

- **Out of credits / wrong account.** The classic: the run billed an `ANTHROPIC_API_KEY` that ran out,
  so `claude` errored mid-tick. Confirm the billing path (`loopkit doctor` → `agent` row) — you almost
  certainly want the subscription default (don't pass `--api-key`). See [`BILLING.md`](BILLING.md).
- **Not logged in.** `claude` with no usable credential. Run `claude` once interactively to confirm
  it's authenticated.
- **A tool/permission wall.** Headless `claude-code` needs `args = ["--dangerously-skip-permissions"]`
  in `[agent]` (the gates + protected paths are your safety, not the agent's prompts).

## `reason: safety_halt · touched protected path X`

The loop changed (or *something* changed) a file under `safety.protected_paths` — most often the
verifier (`gate/`, `tests/`). This is the **blast-radius guard** (Ch 16) and the verifier-hacking
defense (Ch 9): the loop may not edit its own grader.

- **If the agent did it:** good — the guard caught an attempt to "pass" by weakening the gate. Tighten
  the goal/prompt so it solves the task instead.
- **If *you* did it:** you edited the working tree of a **live run**. Don't — make edits between runs.
  See [`OPERATING.md`](OPERATING.md) → "Never touch the working tree of a live run". No harm done (the
  tick was reverted, `main` untouched); just re-run.

## `preflight: working tree is dirty`

`doctor`/`run` refuses to start on uncommitted changes — the loop commits every tick and can't tell its
edits from yours. **Commit or stash first** (including your `loopkit.toml`/`PROMPT.md`/`gate/`), or set
`safety.require_clean_tree = false` if you really mean to.

## `preflight: iteration gate is non-deterministic`

`run --check-gate N` (or `safety.gate_stability_runs`) ran the iteration gate N× on the unchanged tree
and got disagreeing verdicts. A flaky per-tick gate corrupts every stop decision (Ch 9). **Fix the
gate's flakiness** (pin seeds, remove time/network dependence), or move the nondeterministic check to
the **acceptance** gate (run once), or override with `--force` if you accept the risk.

## A run looks **hung** (no output for minutes)

Expected. The agent/gate runs as a captured subprocess, so it's silent until it returns — but the loop
now emits `tick.progress phase=… elapsedSec=…` every ~20 s. If you see those, it's working. To
double-check from a second terminal: `pgrep -lf 'claude -p'` and `git -C <repo> log --oneline -3`. Full
playbook in [`OPERATING.md`](OPERATING.md).

## `OSError: AF_UNIX path too long` (the executor sidecar)

`loopkit executor --socket <path>` failed to bind. Unix-domain socket paths have a hard OS limit
(~104 chars on macOS, ~108 on Linux) — your `--socket` path is too long. **Use a short path**, e.g.
`--socket /tmp/lk-exec.sock` (or the in-pod `/run/loopkit/exec.sock`). Not a loopkit bug — the OS limit.

## `git clone failed` during `loopkit measure`

Fixed in current loopkit (the runner now resolves a relative `repo`). On an older build, `measure` with
`repo = "."` cloned from the wrong directory and every trial errored. Update, or pass `--repo` with an
**absolute** path.

## Tracing isn't showing up

`doctor` → `tracing` row tells you why. It auto-activates only when **both** `loopkit[trace]` is
installed **and** a `LANGSMITH_API_KEY` (or `LANGSMITH_TRACING`) is set — otherwise it's a clean no-op.
Behind a corp TLS proxy the uploader may fail cert verification; that's a dev-only concern (loopkit
injects `truststore` if importable — it ships in `[dev]`, never `[trace]`).
