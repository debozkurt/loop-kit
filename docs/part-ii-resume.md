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

Orchestration (Ch 10–12) and continuous review (Ch 8) are done. Next is the **skills +
write-back flywheel (Ch 17)** — `extensions/skills.py`. The seam is typed: a `SkillRegistry`
with `render() -> str` (skills injected into the prompt) and `write_back(run_result)` (distill
a successful run into a named skill). Two attach points in the core: read in
`prompt.build_prompt` (render the registry into the assembled prompt — add an optional
`skills: str` arg threaded from `run_loop`, kept None for v1), and write in `run_loop`'s DONE
path (call `write_back` after the acceptance gate passes). **Gated, never ungated** (Ch 17/19):
write-back must run through a gate/review before a run can mint a reusable skill, or the
flywheel learns junk. Add a `Ch 17` scenario + MockAgent tests (a skill rendered into the
prompt changes behaviour; a successful gated run writes one back; an ungated/failed run does
not). After that: the Tilt deployable fleet (Ch ?, the last Part II item).

Wiring notes carried forward:
- `Supervisor.evolve` re-validates *survivors only* (high-score-first, stops at first pass).
  Widen here if you ever want every candidate's held-out verdict surfaced (e.g. a dashboard).
- `run_loop`'s `review_hook` runs **only on a real commit** and gates the done-check; a None
  hook is exact v1 behaviour. The skills read-attach should follow the same opt-in shape.

## Gotchas already paid for (keep the fixes)

`git status` collapses new untracked dirs → use `--untracked-files=all`. Python gates litter
`__pycache__` into protected paths → gates run `PYTHONDONTWRITEBYTECODE=1` + repo `.gitignore`.
`examples/` isn't packaged on non-editable install → scenarios resolve `LOOPKIT_DEMO_REPO`
(set in the Dockerfile).
