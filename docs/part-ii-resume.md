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
   competed on. Start here.
2. **Continuous review (Ch 8)** — `extensions/review.py`. A `ReviewHook` called after each
   commit; add an `after_commit` hook point to `run_loop`. The roborev fix→re-review loop.
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

## Suggested first step

`extensions/orchestrate.py`: `make_worktree()` + `run_worker()` (wraps `run_loop`) +
`run_fleet()` (bounded pool over independent tasks) + a `Ch 10` scenario + MockAgent tests.
Then layer the evolutionary strategy and the selection-inflation re-validation on top.

## Gotchas already paid for (keep the fixes)

`git status` collapses new untracked dirs → use `--untracked-files=all`. Python gates litter
`__pycache__` into protected paths → gates run `PYTHONDONTWRITEBYTECODE=1` + repo `.gitignore`.
`examples/` isn't packaged on non-editable install → scenarios resolve `LOOPKIT_DEMO_REPO`
(set in the Dockerfile).
