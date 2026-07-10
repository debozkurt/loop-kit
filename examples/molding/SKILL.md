---
name: loopkit-mold
description: Mold loopkit to a specific repo or issue — detect the stack, propose the config + gates, author and fail-first-verify a held-out oracle, set the safety envelope, and choose which features (skills flywheel, evolve best-of-N, measure calibration, plan mode, worktrees) fit this work. Use when the user wants to set up loopkit on a repo, wire gates/verification for an issue, or says "mold loopkit here".
user-invocable: true
argument-hint: "[repo path] [issue/goal] [--issue N]"
---

# loopkit-mold — configure loopkit for *this* repo/issue

You are molding [loopkit](https://github.com/debozkurt/loop-kit) to a specific repository and piece of
work. loopkit is autonomous at **execution** (goal + two gates → a governed loop drives to done); it is
**not** autonomous at **configuration**. That is your job here: read the repo and the goal, then produce
a *molded instance* — a `loopkit.toml`, the gates, a fail-first-verified held-out oracle, the safety
envelope, and the right feature set — for a human to review before the loop runs.

**You are the molder. loopkit ships the verified building blocks; you supply the judgment.** Do not
hand-roll what `examples/` already provides — compose it. The deterministic, safety-critical, and
verification parts must come from the kit; only the judgment (what "done" means here, which features
fit) is yours.

> **Not the flywheel.** This `loopkit-mold` skill is unrelated to loopkit's *runtime* skill flywheel
> (`loopkit/extensions/skills.py`), which distils lessons across runs. Same word, different thing.

> **Trust boundary (non-negotiable).** The output is a **proposal for human review**, never an
> auto-run. If the goal is an untrusted issue body (CI/unattended), a synthesized oracle/config must
> never be auto-trusted: fail-first verification is mandatory, and the protected-path guard + branch
> guard (never `main`) + budget ceiling still bound blast radius. Same discipline as the flywheel's
> poisoning guards.

## The recipe

Work these steps in order. Each cites the kit block that does the deterministic/verified part.

### 1. Detect the repo (deterministic)

Inspect the repo and decide the *mechanical, safety-critical* config — never guess these:

| Signal | Read from | Becomes |
|---|---|---|
| Test runner | `pyproject.toml`/`tox.ini` → pytest · `package.json` `scripts.test` → that · `go.mod` → `go test ./...` · `Makefile` → a `test`/`verify` target | `[gate].iteration` |
| Protected paths | CI files (`.github/`, `.gitlab-ci.yml`), `chart/`/`helm/`, `migrations/`, lockfiles, and **the gate files themselves** | `[safety].protected_paths` |
| Default branch | `git symbolic-ref refs/remotes/origin/HEAD` (or `git branch`) | `[safety].forbid_branches` includes it; run branch is `loopkit/*` |
| Agent on PATH | `which claude` / `which codex` | `[agent].adapter` |
| House rules | `CLAUDE.md` / `AGENTS.md` if present | `[prompt].anchors` (else you write a `PROMPT.md`) |

Start from [`../gates/loopkit.example.toml`](../gates/loopkit.example.toml) (every knob, commented) and
keep what fits. (When `loopkit detect` lands — Part IV Layer 3 — it prints this proposal for you; until
then, do it by inspection.)

### 2. Frame the goal + a typed Definition of Done

A checklist item or issue is only real to the loop if "done" is *verifiable*. Classify the work into a
**coverage tier**, and the tier prescribes what the acceptance test must assert — see
[`coverage-tiers.md`](coverage-tiers.md) (the `ledger2issues.py COVERAGE_TIER_DOD` pattern,
generalized). Assemble a 4-part DoD (behavior implemented · a tier-keyed test **ships** with the fix ·
the existing suite stays green · observability on new failure paths) + an explicit *out-of-scope* note.
This is the goal loopkit optimizes toward.

### 3. Author + fail-first-verify the held-out oracle

The load-bearing step. The **acceptance** gate is a *held-out* oracle the agent never sees, so passing
it is real evidence — but only if it actually fails on the current (buggy) tree first.

- Copy [`templates/acceptance-oracle.sh`](templates/acceptance-oracle.sh) to `acceptance/<key>/run.sh`
  and write the hidden test it copies in / runs. Keep it **out of the tree the agent edits**.
- **Fail-first verify it** with [`../gates/validate.sh`](../gates/validate.sh): run the oracle against
  the current tree — it MUST fail. An oracle that passes on the buggy tree certifies nothing. Wire it as
  the pre-loop check: `run --validate "GATE_ORACLE='bash acceptance/<key>/run.sh' bash examples/gates/validate.sh"`.
- Chain the structural gate before the oracle with
  [`templates/acceptance-dispatcher.sh`](templates/acceptance-dispatcher.sh) (has-tests → oracle), so a
  fix with no shipped test can't pass.

### 4. Wire the gates (the two-oracle pattern, Ch 9)

- **iteration** (fast, in-sample, every tick): the repo's own suite — **scope it to the touched module**
  when you can (e.g. `pytest tests/listings -q`, not the whole suite) for fast ticks.
- **acceptance** (held-out, once, certifies DONE): the dispatcher from step 3.
- **regression** (optional second oracle): a held-out PASS_TO_PASS set — previously-passing behavior must
  stay green (SWE-bench FAIL_TO_PASS + PASS_TO_PASS).
- An **LLM review** belongs in the *review hook* (`--review`, per-tick feedback) or the *acceptance*
  gate (once) — never the iteration gate (it's nondeterministic; a flaky verdict corrupts every stop).
  See [`../gates/review.sh`](../gates/review.sh) + [`../gates/rubric.md`](../gates/rubric.md).

### 5. Choose features by the routing table

Reach for a feature only when its trigger holds — defaults are single-run:

| Feature | Reach for it when | Block |
|---|---|---|
| `evolve` (best-of-N + re-validation) | the task is ambiguous/hard with several plausible fixes, or calibrated `pass^k` is low | [`../evolve/`](../evolve/) |
| `measure` (pass^k calibration) | a gate might be flaky, or before trusting a whole batch — calibrate a cheap representative task first, then route | [`../skills/`](../skills/) |
| skills flywheel (per-repo/lang dir) | same-class tasks recur (a sweep) — lessons compound | [`../skills/`](../skills/) |
| plan mode (`--plan`) | one coherent multi-step feature — a sequential backlog, bounded by plan-stall | [`../../docs/CONTROL-FILES.md`](../../docs/CONTROL-FILES.md) `[plan]` |
| worktree isolation / fleet | independent tasks in parallel (a bug/finding queue) | [`templates/worktree.sh`](templates/worktree.sh) |
| difficulty → model/adapter | start on a cheap adapter; escalate on no-progress (a cost lever) | `[agent].adapter` |

### 6. Set the safety envelope

Protected paths **must** include the gate's own files (`tests/`, `gate/`, `acceptance/`) — else a run
can "pass" by weakening its own grader (Ch 9, verifier hacking). Branch stays `loopkit/*`, never
`main`/`master`. Keep `require_clean_tree = true` and a real budget ceiling. For a per-issue instance,
unlock only the *minimal* extra path the task needs (a migration task → unlock `migrations/`, nothing
more). Start from [`templates/issue.loopkit.toml`](templates/issue.loopkit.toml).

### 7. (Multi-issue) package per issue with worktrees

For a queue of independent tasks, give each its own isolated clone/worktree reset to the base branch —
[`templates/worktree.sh`](templates/worktree.sh) (the `sequencer.py` prep pattern, generalized). One
molded instance per issue; run them via the fleet (`fleet run --from-issues`) or the in-process
`run_fleet`. Sequential dependent steps in *one* feature → prefer `--plan` instead.

### 8. Human review, then run

Present the full proposed instance (config + gates + oracle + routing + budget) for review. On approval:
`loopkit doctor` (preflight) → `loopkit run` (add `--open-pr` / a `[remote]` block to land a draft PR a
human merges). The loop opens the PR; a human is always the merge authority.

## Which kit block owns what

| Need | Block |
|---|---|
| Every config knob, annotated | [`../gates/loopkit.example.toml`](../gates/loopkit.example.toml) |
| What a gate can be; two-oracle; gate-vs-review-hook | [`../gates/README.md`](../gates/README.md) |
| Fail-first pre-loop check | [`../gates/validate.sh`](../gates/validate.sh) |
| Diff-ships-a-test structural gate | [`../gates/has-tests.sh`](../gates/has-tests.sh) |
| Held-out LLM peer review + rubric | [`../gates/review.sh`](../gates/review.sh) · [`../gates/rubric.md`](../gates/rubric.md) |
| Best-of-N scorer + re-validation | [`../evolve/`](../evolve/) |
| pass^k calibration + flywheel distiller | [`../skills/`](../skills/) |
| CI deployment (issue → draft PR) | [`../ci/`](../ci/) |
| Coverage-tier → typed DoD | [`coverage-tiers.md`](coverage-tiers.md) |
| Per-issue templates (config, oracle, rubric, dispatcher, worktree) | [`templates/`](templates/) |
