# loopkit — AI primer

One page, structured, canonical: what loopkit is, the module → contract map, the invariants, the
full command + config surface, and where to plug in. Zero narrative by design — for an AI analyzing
the platform and for engineers ramping fast. Facts verified against the code on 2026-07-19;
`loopkit --help` and `loopkit/config.py` are the live truth wherever this page and the code disagree.

## What it is

loopkit is a self-governed coding loop you point at any git repository: declare a goal and two
shell-command gates, and it drives a coding agent toward the goal tick by tick, committing every
tick to an isolated branch. Guardrails are structural, not advisory: an in-sample iteration gate, a
held-out acceptance gate the loop never optimizes against, an on-by-default adversarial review
judge, hard stops (budget / no-progress / iteration cap), and a safety envelope (never `main`,
protected paths, opt-in-only outbound push). The same core loop scales unchanged to a
manifest-driven parallel batch, a Redis-queue fleet, and a Kubernetes control plane.

## Tick lifecycle and terminals

```text
reload anchors (fresh context) → agent.act → safety guard → commit tick
  → iteration gate (fail ⇒ feedback into next tick)
  → review, only behind a GREEN iteration gate (reject ⇒ feedback; verdict sticky per commit)
  → held-out acceptance, once, for a candidate that passed both  → DONE
hard stops evaluated per tick: budget · no-progress · iteration cap · review-stall · plan-stall
```

Terminal reasons (`loopkit/stops.py`, `StopReason`):

| Reason | Meaning |
|---|---|
| `done` | the held-out acceptance gate passed |
| `safety_halt` | the agent touched a protected path — change reverted, run halted (mid-tick terminal) |
| `budget_ceiling` | cumulative real cost reached `agent.max_cost_usd` (judge spend counts) |
| `no_progress` | tree signature unchanged for `stops.no_progress_after` ticks |
| `review_stall` | `stops.review_stall_after` consecutive fresh review REJECTs — a human should look |
| `plan_stall` | plan mode: the checklist done-count stopped advancing (`stops.plan_stall_after`) |
| `iteration_cap` | `stops.max_iter` reached |
| `review_unavailable` | the judge cannot render verdicts — infra failure, not a verdict (mid-tick terminal) |

Precedence when several could fire: `DONE ▸ SAFETY ▸ BUDGET_CEILING ▸ NO_PROGRESS ▸ ITERATION_CAP`;
`safety_halt` and `review_unavailable` are raised mid-tick, outside the end-of-tick policy order.

## Module map

Core (`loopkit/`) — no runtime dependency on `extensions/`:

| Module | Responsibility | Key contract |
|---|---|---|
| `config.py` | the whole run as one pydantic object | `load_config(path) -> Config`; full surface in the config table below |
| `agent.py` | the model as a subroutine — adapters `mock` · `claude-code` · `codex` (CLI) · `claude-api` · `openai-api` (SDK) | `Agent.act(prompt, workspace, *, observer=None) -> AgentResult(ok, cost_usd, summary, raw_tail)`; `MockAgent` = zero-token test double |
| `pricing.py` | per-model price table → exact per-tick `cost_usd` | what makes `budget_ceiling` bite; an unpriced model is flagged by `doctor` |
| `prompt.py` | fixed prompt, fresh context every tick | `prompt.anchors` files reloaded verbatim each tick |
| `gate.py` | iteration + held-out acceptance gates | `Gate.check(workspace) -> GateResult(passed, feedback, cost_usd)`; `ShellGate`: exit 0 = pass, non-zero = fail, stdout tail = feedback |
| `stops.py` | hard stops + precedence | `StopPolicy` over `LoopState`; `first_stop` returns the first firing policy in the order given |
| `durability.py` | git as the durable store | function-based (no class): `commit_progress` every tick, `state_signature`, `ensure_branch`; resume = re-run on the branch |
| `safety.py` | blast-radius preflight + protected-path guard | violation ⇒ revert uncommitted + `safety_halt` |
| `plan.py` | sequential-backlog (plan) mode | checklist file (`plan.file`); progress = done-count; `plan_stall` stop |
| `executor.py` | the tool-execution seam (agent isolation) | `ToolExecutor.dispatch(name, args, workspace) -> (output, ok)` + `run_gate(command, workspace, *, tail)`; `LocalToolExecutor` default |
| `log.py` | always-on payload-free logs | `[loopkit][component]` + run id; ids/lengths/counts only — safe to ship |
| `trace.py` | optional LangSmith full-tree traces | auto-on iff `langsmith` + key present; `None`-safe no-op otherwise; nests via contextvars |
| `secrets.py` | credential store + env scrubbing | `CredentialStore`; child processes get scrubbed envs; secrets redacted in streams |
| `loop.py` | the controller — wires all of the above into the tick lifecycle | `run_loop(...) -> RunResult(reason=StopReason, ...)` |
| `_templates.py` / `cli/` / `scenarios/` | `init` scaffolds · the Typer CLI · runnable teaching scenarios (`demo`/`learn`) | |

Extensions (`loopkit/extensions/`) — each attaches at a seam, `None`-safe:

| Module | Responsibility | Key contract |
|---|---|---|
| `review.py` | continuous review gating done | `ReviewHook.review(workspace, commit_message) -> GateResult`; `ShellReviewHook` wraps a command (exit 0 clean / non-zero problems) |
| `judge.py` | the built-in default judge | used when `[review]` has no `command`; fresh-context, real-defects-only review of the run's cumulative diff |
| `orchestrate.py` | in-process supervisor | `run_fleet` (fan-out over git worktrees) · `evolve` (top-k select; only a re-validated winner reseeds) |
| `fleet.py` | Redis-queue fleet | coordinator `LPUSH` / worker `BRPOP` / `HSET` result; reuses orchestrator result shapes as the wire format |
| `batch.py` | manifest-driven parallel batch | one isolated clone + branch (+ draft PR) per task; `group` serializes, `after` orders, failed dep skips dependents; journal + resume |
| `skills.py` | skill write-back flywheel | `SkillRegistry` Protocol; write-back gated (a done run must clear a write-back gate to mint a skill); `FileSkillRegistry` persists markdown |
| `detect.py` | deterministic stack detection | file markers → proposed `loopkit.toml` with evidence + confidence per fact |
| `mold.py` | mold a finding into a runnable instance | `ShellProposer` contract below; oracle + per-task config, provenance recorded |
| `synth_gate.py` | oracle verification | fail-first (and fail→pass with `--fix`); the only thing that blesses a proposed oracle |
| `measure.py` / `route.py` | reliability calibration → strategy | N trials → pass^k / pass@k report → single run vs escalate to evolve |
| `overlap.py` | task-collision prediction | pairwise intersection of predicted-touch sets; advisory, never a gate |
| `remote.py` / `issues.py` | push + draft PR/MR · issues → tasks | via `gh`/`glab`; cannot push `main`, never force-pushes |
| `triggers.py` | webhook/cron triggers | signed forge events → runs; `IdempotencyStore` Protocol (in-memory / Redis) |
| `cloud.py` / `cloudrun.py` / `creds.py` | Kubernetes control plane | context-pin guard on every mutating command; per-run `run-<id>` namespaces; submitter Secrets in `loopkit-system` |

## Invariants

1. **Repo-agnostic core.** No repo-specific nouns in `loopkit/`; repo particulars live in the
   operator's config/gates/proposer; generality is built for observed needs only (deferred
   generality gets a named trigger, not a speculative knob).
2. **Contracts are reused, never forked**: `Agent` / `Gate` / the git store, the stop policies, the
   held-out acceptance gate, the `[loopkit][component]` + run-id log shape.
3. **The seam rule.** Core keeps no runtime dependency on `extensions/`. Every attach point is
   keyword-only, typing-only import, duck-called, and `None`-safe — `None` = exact prior behavior.
4. **Thin stack.** Core deps are `typer + rich + pydantic`, stdlib-first elsewhere. Heavy wires are
   extras: `[claude]`/`[openai]` (API SDKs; `[agents]` = both), `[trace]` (langsmith), `[fleet]`
   (redis), `[cloud]` (kubernetes), `[all]` (everything), `[dev]` (test tooling). Plain
   `pip install loopkit` pulls none of them.
5. **Safe by default.** Refuses `main`/`master`; wants a clean tree; commits every tick to its own
   branch; never force-pushes; protected-path violation ⇒ revert + halt; budget ceiling regardless
   of progress; nothing leaves the machine unless `[remote].enabled` or `--open-pr`.
6. **Two-layer observability.** Always-on payload-free logs, plus optional full-payload LangSmith
   traces (the access-gated place human-readable I/O + tool calls + per-span cost belong). Cost is
   exact via `pricing.py`. Exception by design: the claude-code adapter's live `[loopkit][agent]`
   step stream carries thoughts/tool targets (secrets redacted, not ship-safe); pin
   `[agent] args = ["--output-format", "json"]` for the quiet payload-free path.
7. **The two gates stay distinct.** The loop optimizes the iteration gate; the acceptance gate is
   held out and run once — `doctor` warns when they are identical.

## Review semantics (on by default)

- No `[review].command` ⇒ the **built-in default judge** reviews every plausibly-done tick, on every
  `run` and every `batch` task, zero config. Design: `default-judge-design.md`.
- Runs only behind a green iteration gate; blocks on correctness, security, incomplete fixes,
  gamed/trivially-passing tests, unrequested contract breaks; style is advisory, never blocking.
- Verdicts sticky per commit: unchanged HEAD is never re-billed; a rejected commit stays rejected
  until the tree changes.
- Judge backend/model derive from `[agent]`; `[review].backend`/`model` override (cheaper or
  cross-vendor judge). `criteria` layers project rubric files onto the bundled checklist — keep
  them under `protected_paths` (the agent must not tune its own grader).
- Judge cannot run ⇒ `review_unavailable` halt. N straight rejects ⇒ `review_stall`.
  Judge spend counts toward `max_cost_usd`.
- Off-switches: `enabled = false` (everywhere), `--no-review` (per invocation); explicit
  `run --review <cmd>` / manifest `review =` overrides. `run` prints the decision;
  `doctor` probes the judge binary/SDK + pricing.
- Standalone: `loopkit review` — exit 0 APPROVE · 1 REJECT · 2 unavailable (wireable as a
  `[review].command` itself).

## Command surface

Live truth: `loopkit --help` (and `<cmd> --help`). One line each.

### Core — one loop

| Command | Does |
|---|---|
| `loopkit init [PATH]` | scaffold `loopkit.toml` + `PROMPT.md` (never overwrites); `--plan` = sequential-backlog variant, `--ci github\|gitlab` = CI-tier workflow files |
| `loopkit detect [REPO]` | deterministically read stack markers → proposed `loopkit.toml` (`--write`, `--force`) with per-fact evidence + confidence |
| `loopkit doctor` | preflight: branch safety, agent runnable, budget priced, gates + judge + tracing status; runs the iteration gate once (advisory verdict; `--no-gate` skips) |
| `loopkit run` | drive the loop to a terminal; key flags: `--repo`, `--dry-run`, `--max-iter`, `--force`, `--sandbox`, `--open-pr`, `--api-key`, `--review <cmd>`/`--no-review`, `--check-gate N` |
| `loopkit review` | run the judge once on the current change; exit 0/1/2 |
| `loopkit measure` | run the goal N times (`--trials`) → pass^k / pass@k reliability report |
| `loopkit route` | turn a reliability report into a strategy: single run vs escalate to evolve |
| `loopkit synth-gate [ORACLE]` | verify a proposed held-out oracle is real: fail-first, and fail→pass with `--fix` |
| `loopkit demo [CH]` / `loopkit learn [CH]` | run / narrate a teaching scenario (omit CH to list) |
| `loopkit executor --socket PATH` | keyless tool-execution sidecar (agent isolation; shared socket with loopkit-core) |

### Batch — many tasks, no infra

| Command | Does |
|---|---|
| `loopkit batch --tasks batch.toml` | one isolated core loop per task, conflict-aware (`group`/`after`), `--jobs N`, `--only id`, journal + resume, draft PR per task |
| `loopkit overlap --tasks <manifest>` | predict task collisions from touch-sets — advisory, never a gate |
| `loopkit mold-batch --tasks plan.toml` | findings → verified fail-first oracle + per-task config → reviewable `batch.toml`; `--out`, `--level`, `--jobs N`; never auto-runs |

### Fleet — Redis queue

| Command | Does |
|---|---|
| `loopkit fleet worker` | long-lived executor: BRPOP task → run loop in an isolated clone → HSET result; `--adapter`, `--target`, `--redis-url`, gate overrides |
| `loopkit fleet run` | coordinator fan-out: `-n` goals or `--from-issues` (`--label`, `--provider`) → `FleetResult` |
| `loopkit fleet evolve` | evolutionary search: `-g` generations × `-p` population, keep `-k`, re-validate survivors on a held-out gate, reseed only a validated winner |

### Cloud — Kubernetes control plane

Every mutating command is guarded by a kube-context pin (wrong context ⇒ refuse).

| Command | Does |
|---|---|
| `loopkit cloud context` / `cloud doctor` | show the active context + guard verdict / preflight the control plane (read-only) |
| `loopkit cloud bootstrap` | one-time: apply `ns/loopkit-system` (Redis, RBAC, NetworkPolicy) |
| `loopkit cloud run` | start a run: build `ns/run-<id>` + coordinator/worker Jobs |
| `loopkit cloud ls` / `status` / `logs` | list runs / one run's phase + workers / pod logs (read-only) |
| `loopkit cloud kill` | delete a run's namespace and everything in it |
| `loopkit cloud schedule` / `schedules` / `unschedule` | CronJob firing `cloud run --in-cluster` / list / delete |
| `loopkit cloud webhook` | serve the forge webhook listener: signed issue events → one guarded in-cluster run |
| `loopkit cloud creds set` / `ls` / `rm` | register a submitter's keys as a Secret / list (names only) / delete |

## Config surface (`loopkit.toml`)

Source of truth: `loopkit/config.py`. Annotated copy-me: `../examples/gates/loopkit.example.toml`;
knob-by-knob guide: `CONTROL-FILES.md`.

| Section | Key | Default | Meaning |
|---|---|---|---|
| top level | `goal` | *(required)* | the one condition the loop drives toward |
| | `repo` | `"."` | target repo — path or git URL |
| | `branch` | `"loopkit/run"` | the run's branch; never `main`/`master` |
| `[agent]` | `adapter` | `"mock"` | `mock` · `claude-code` · `codex` · `claude-api` · `openai-api` |
| | `model` | none | provider default if unset |
| | `max_cost_usd` | `10.0` | the budget ceiling; size for a few ticks, not one |
| | `max_tool_calls` | `25` | per-tick tool-call cap (API adapters) |
| | `args` | `[]` | extra CLI flags for CLI adapters |
| | `use_api_key` | `false` | claude-code: bill the API key instead of the subscription |
| `[prompt]` | `anchors` | `["PROMPT.md"]` | files reloaded verbatim into every tick's fresh context |
| `[plan]` | `file` | none | checklist file; set ⇒ plan (sequential-backlog) mode on |
| `[gate]` | `iteration` | *(required)* | fast, in-sample — runs every tick; any shell command: exit 0 pass, stdout = feedback |
| | `acceptance` | none | held-out — run once before DONE |
| | `regression` | none | optional second held-out oracle: don't break what worked |
| `[stops]` | `max_iter` | `30` | hard iteration cap |
| | `no_progress_after` | `3` | halt after N unchanged-tree ticks |
| | `plan_stall_after` | `6` | plan mode: halt after N ticks with no new done items |
| | `review_stall_after` | `4` | halt after N consecutive fresh review rejects |
| `[safety]` | `protected_paths` | `[]` | the loop may not touch these (guard the verifier: tests, gates, rubrics) |
| | `require_clean_tree` | `true` | refuse to start on uncommitted changes |
| | `allow_branches` | `["loopkit/*"]` | branch allowlist |
| | `forbid_branches` | `["main", "master"]` | branch denylist |
| | `gate_stability_runs` | `0` | preflight: run the iteration gate N× unchanged; refuse on a flaky verdict |
| `[remote]` | `enabled` | `false` | master switch — nothing is pushed unless true (or `run --open-pr`) |
| | `name` | `"origin"` | git remote |
| | `push` | `true` | push the loop branch on DONE |
| | `open_pr` | `false` | open a PR/MR after pushing (needs `gh`/`glab` authed) |
| | `provider` | `"auto"` | `auto` · `github` · `gitlab` |
| | `pr_base` | `"main"` | PR/MR base branch |
| | `draft` | `true` | PRs open as drafts — a human reviews + merges |
| `[trace]` | `enabled` | auto | `None` = on iff `langsmith` + a LangSmith key present |
| | `project` | none | LangSmith project name (falls back to env, then `loopkit`) |
| `[review]` | `command` | none | judge shell command; unset ⇒ the built-in default judge |
| | `enabled` | `true` | `false` = review off everywhere |
| | `backend` / `model` / `args` / `use_api_key` | inherit `[agent]` | judge-only overrides |
| | `criteria` | `[]` | rubric files layered onto the bundled checklist |

## Extension seams — where to plug in

The shape every seam follows: keyword-only parameter, typing-only import, duck-typed call,
`None`-safe (`None` = exact prior behavior).

| Seam | Contract | Plug in when |
|---|---|---|
| Agent adapter | `Agent.act(prompt, workspace, *, observer=None) -> AgentResult` | a new model CLI/SDK; `MockAgent` for tests |
| Gate | `Gate.check(workspace) -> GateResult` — or just any shell command | a non-shell verifier (a Python callable, a service call) |
| Review | `ReviewHook.review(workspace, commit_message) -> GateResult` | a custom judge; or wire a command via `[review].command` |
| Tool executor | `ToolExecutor.dispatch(name, args, workspace)` + `run_gate(...)` | isolating tool execution (the keyless sidecar: `loopkit executor`) |
| Orchestration | `run_fleet(cfg, tasks, make_agent, max_workers)` / `evolve(...)` | many loops in-process, over git worktrees |
| Skills | `SkillRegistry` Protocol; write-back stays gated | distillation/rendering of solved runs into future prompts |
| Mold proposer | `ShellProposer`: command runs with CWD = repo, scrubbed env + `MOLD_TASK_ID` / `MOLD_TIER` / `MOLD_TIER_ASSERTION` / `MOLD_GOAL_FILE` / `MOLD_ORACLE_DIR` / `MOLD_PROBE_FILE` / `MOLD_TOUCHES_FILE`; exit 0 = proposed; output untrusted until `synth-gate` verifies | authoring held-out oracles with an agent's judgment during `mold-batch` |
| Triggers | `IdempotencyStore` Protocol (in-memory / Redis) | webhook/cron dedup in the cloud tier |

## Pointers

| For | Read |
|---|---|
| First run + onboarding | `../README.md` (Quickstart) → `../examples/walkthrough/` |
| Your repo end to end (remote sync, issues, CI, batch) | `USING-ON-YOUR-REPO.md` |
| Every knob, annotated | `CONTROL-FILES.md` · `../examples/gates/loopkit.example.toml` |
| How it's built (living wiki + module ownership) | `architecture/README.md` |
| Why two gates, review-by-default, judge design | `../README.md` §"The two gates" · `default-judge-design.md` |
| Operating a run / failures / billing | `OPERATING.md` · `TROUBLESHOOTING.md` · `BILLING.md` |
| The concepts course (chapters `demo N` maps to) | the *Agentic Loops* manual — https://github.com/debozkurt/loop-guide |
