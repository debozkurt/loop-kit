# loopkit — Part II resume

**Read this first when picking up loopkit Part II.** It is the single source of truth for the
next phase; the auto-memory `project_loopkit` only points here.

## Where v1 left off (done)

The **single-agent core is complete, tested, and containerized** — 4 commits on `main`,
10 tests green (MockAgent-driven, no tokens), Docker image builds and demos run in-container.
Architecture mirrors the curriculum, one module per part:

```
config.py(18)  agent.py(1-3)  prompt.py(4-5)  gate.py(6-7,9)  stops.py(13)
durability.py(15)  safety.py(16)  loop.py(controller)  cli.py  log.py
extensions/  scenarios/  examples/demo-repo/  tests/
```

Controller tick: `prompt → agent → protected-path guard → commit → iteration gate →
held-out acceptance gate → hard stops`. Terminal precedence
`DONE > SAFETY > BUDGET > NO_PROGRESS > CAP`.

Run it: `source .venv/bin/activate && pytest -q`; `loopkit demo|learn <ch>`;
`docker build -t loopkit . && docker run --rm loopkit demo 9`.

## What Part II adds (the seams already exist)

Interfaces are defined in `loopkit/extensions/`; Part II implements them and adds the marked
controller attach points. **Do not rewrite the core contracts — attach at the seams.**

1. **Orchestration (Ch 10–12)** — `extensions/orchestrate.py`. A `Supervisor` over worker
   loops in **git worktrees** (isolation); `run_loop` becomes the worker body unchanged. Two
   strategies: blind **fan-out** (N independent tasks) and **evolutionary** (score, keep top-k,
   reseed winners into the next generation's prompts). Carry the **selection-inflation** guard
   from Ch 9: when keeping best-of-N, re-validate the winner on the held-out gate it never
   competed on.
   - ✅ **Fan-out done** (commit `1c23220`; Ch 10 scenario + 7 tests). `make_worktree`/
     `remove_worktree` (serialized creation via `_WORKTREE_LOCK`, branch survives teardown),
     `Supervisor.run_fleet` (bounded `ThreadPoolExecutor`, one crash contained per
     `WorkerResult.error`), `run_fleet` convenience, `WorkerResult`/`FleetResult` (`.done` /
     `.failed`). Per-worker config via `base_config.model_copy`.
   - ✅ **Evolutionary done** (Ch 11 scenario + 4 tests, 21 total green). `Supervisor.evolve(
     base_task, *, generations, population, keep, score, revalidate, candidate_task)`. Per
     generation: `_dispatch` N attempts at the same goal → `Scorer` ranks → keep top-k →
     `_first_revalidated` walks survivors high-score-first and confirms the first to pass the
     held-out `RevalidateFactory` gate. **Only a re-validated winner reseeds** the next
     generation (tree-level: next gen branches off the winner; prompt-level: seed note appended
     to the goal). `Candidate`/`Generation`/`EvolutionResult` with `.winner`, `.inflated`,
     `.inflation_caught`. The selection-inflation guard is proved by
     `test_selection_inflation_is_caught_by_revalidation` (the 1.0-score "memorizer" loses to
     the 0.9 "solver" that passes held-out).
2. **Continuous review (Ch 8)** — `extensions/review.py`. A `ReviewHook` called after each
   commit; add an `after_commit` hook point to `run_loop`. The roborev fix→re-review loop.
   - ✅ **Done** (Ch 8 scenario + 6 tests, 27 total green). `ReviewHook` Protocol +
     `CallableReviewHook` / `ShellReviewHook` (mirror `CallableGate`/`ShellGate`). `run_loop`
     gained `review_hook=None` (typing-only import, duck-called — core keeps no runtime dep on
     the extension) and the attach point: after `commit_progress`, **only on a real commit**, a
     failing review sets `feedback` and makes `review_ok=False`, which *skips the entire
     iteration/acceptance gate block* — so green-but-unreviewed work can never be declared done,
     and the findings reach the next tick's prompt. `Stage.run` forwards `review_hook` for
     scenarios. Verified in `demo 8`: tick 1 review fails → gates skipped → tick 2 clears → done.
3. **Skills + write-back flywheel (Ch 17)** — `extensions/skills.py`. A `SkillRegistry`
   rendered into the prompt (attach in `prompt.build_prompt`) + `write_back` after DONE (attach
   in `loop.run_loop`'s done path). Gated — never ungated write-back.
   - ✅ **Done** (Ch 17 scenario + 10 tests, 37 total green). `Skill` + `SkillRegistry` Protocol
     + `InMemorySkillRegistry` / `FileSkillRegistry` (durable, one .md per skill) over a
     `_BaseRegistry` whose `_vet` enforces the gate→distil→dedupe policy. Read edge:
     `build_prompt(config, feedback, skills)` renders the block before feedback; `run_loop`
     threads `skills.render()` each tick (`skillsLen` logged). Write edge: `run_loop`'s DONE path
     calls `skills.write_back(done, repo, config.goal)`. **Gated via the registry's own
     `write_back_gate`** — `test_write_back_is_gated_out_when_gate_fails` proves a failed gate
     learns nothing. Pluggable `Distiller` (default = provenance only; real one asks the agent).
     `demo 17` is the two-run flywheel: run A learns the boundary in 2 ticks + writes it back,
     run B reads the skill and nails it in 1.
4. **Tilt + deployable fleet** — worker loops as containers, a queue, optionally a dashboard.
   This is where Tilt earns its keep (multiple live-reloaded services). Honor the global
   kubectl/context-safety rules if any k8s lands.

## Constraints to preserve (don't regress these)

- Reuse the contracts: `Agent` / `Gate` / `Store`, the three `StopPolicy` stops, the held-out
  acceptance gate, the `[loopkit][component]` + run-id logging, safe-by-default (never `main`,
  clean tree, protected paths, budget ceiling).
- Each parallel worker = its own branch/worktree, commit-every-tick (durable, resumable).
- Stack stays **typer + rich + pydantic**, stdlib-first elsewhere.
- **Test-as-you-go with MockAgent (no tokens); log-as-you-go.** Add a scenario for each new
  concept (`scenarios/chNN_*.py`) so `demo`/`learn` keep pace.

## Suggested next step

Three of the four Part II workstreams are done — orchestration (Ch 10–12), continuous review
(Ch 8), and skills + write-back (Ch 17). The library is feature-complete for the curriculum;
all four extension seams are now real. The **last item is the Tilt deployable fleet** (#4
below): worker loops as containers, a queue feeding tasks, optionally a dashboard reading
`FleetResult`/`EvolutionResult`. This is the one piece that leaves the single-process model —
honor the global kubectl/context-safety rules if any k8s lands, and lean on Tilt for the
multi-service live-reload. It's more infra than library, so scope it deliberately; the
single-binary core is done and shouldn't need changes to be containerized (`loopkit run` already
runs sandboxed — see `cli.py` `_run_sandboxed`).

If not doing the fleet next, the highest-value library polish is a `loopkit fleet`/`evolve` CLI
surface (today orchestration is Python-API only) so the supervisor is drivable from argv like
`run`/`demo`.

Wiring notes carried forward (load-bearing seams, don't regress):
- `run_loop` keyword-only opt-ins, each None = exact v1: `review_hook` (Ch 8, gates done, runs
  only on a real commit), `skills` (Ch 17, rendered each tick + gated write-back on DONE). Any
  new core attach point should follow the same shape: typing-only import, duck-called, None-safe.
- `Supervisor.evolve` re-validates *survivors only* (high-score-first, stops at first pass).
  Widen here if you ever want every candidate's held-out verdict surfaced (e.g. a dashboard).

## Gotchas already paid for (keep the fixes)

`git status` collapses new untracked dirs → use `--untracked-files=all`. Python gates litter
`__pycache__` into protected paths → gates run `PYTHONDONTWRITEBYTECODE=1` + repo `.gitignore`.
`examples/` isn't packaged on non-editable install → scenarios resolve `LOOPKIT_DEMO_REPO`
(set in the Dockerfile).
