# loopkit — Part II resume

**Read this first when picking up loopkit Part II.** It is the single source of truth for the
next phase; the auto-memory `project_loopkit` only points here.

> **Current state (2026-06-19):** **All four Part II workstreams are done.** Orchestration
> (fan-out + evolutionary), continuous review, the skill write-back flywheel, **and now the Tilt
> deployable fleet (#4)** are implemented, tested, and committed on `main` — **49 tests green**,
> all five `demo 8/10/11/12/17` clean. The fleet's deterministic core (queue + coordinator +
> worker + the Ch 9 guard at fleet scale) is proven against fakeredis + MockAgent (no cluster, no
> tokens); the kind/Tilt/k8s shell is built + static-validated, with `tilt up` as the one manual
> bring-up. Last 2 commits: `613bb7c` fleet library/CLI/scenario · `472fac4` fleet deploy shell
> (kind + Tilt + k8s). Part II is feature-complete.

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

## Part II workstreams (status log)

The four extension seams and their status. **Do not rewrite the core contracts — attach at the
seams.** Items 1–3 are done (details + commit ids inline); item 4 is the remaining work.

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
4. **Tilt + deployable fleet (Ch 12)** — `extensions/fleet.py` + `Tiltfile` + `k8s/` + `Makefile`.
   - ✅ **Done** (commits `613bb7c` library/CLI/scenario, `472fac4` deploy shell; Ch 12 scenario +
     12 tests, 49 total green). The in-process `Supervisor` graduates to a queue-driven fleet:
     isolation goes from logical (worktrees) to **physical** (each worker its own container/repo),
     and the in-memory `Future` becomes a **Redis queue** (`Queue` Protocol → `RedisQueue` lazy
     redis import + `InMemoryQueue` dep-free fake). `WorkerOutcome` is the flat JSON wire form of
     the reused `WorkerResult`/`Candidate` shapes. `Worker` = `pop → run_loop → put` (task id as
     correlation id, one crash contained to an `error` outcome). `Coordinator.run_fleet` (fan-out)
     + `Coordinator.evolve` (generational). **The Ch 9 selection-inflation guard is preserved**:
     the held-out check runs **in the worker** (only it has the candidate's tree) and rides back as
     `WorkerOutcome.revalidated`; the coordinator confirms the first survivor high-score-first that
     passed it — identical *outcome* to `Supervisor.evolve`, only *where* the gate runs moves.
     **v1 reseed is prompt-level**; tree-level (branch off the winner) needs a shared volume = v2.
   - **CLI:** `loopkit fleet worker|run|evolve` over `REDIS_URL` (redis deferred into the commands;
     core CLI loads without the `[fleet]` extra). `make_demo_runner` is the container's real runner
     (clone demo-repo → `run_loop` → real pytest gates); `--adapter mock` solves it with **zero
     tokens**, so the fleet goes green on `tilt up` without credentials.
   - **Deploy shell (the thin outer layer, manual via `tilt up`):** `Makefile` exports a repo-local
     `KUBECONFIG` for every recipe (the isolation guarantee — `~/.kube/config` never touched);
     `Tiltfile` pins `allow_k8s_contexts('kind-loopkit')` + a `fail()` guard; `k8s/redis.yaml`
     (namespace + queue) + `k8s/worker.yaml` (3 pods, `WORKER_NAME` from the pod name). redis is an
     optional `[fleet]` dep; fakeredis is dev-only. **Not yet brought up live** — `make fleet-up &&
     tilt up` is the remaining manual verification (heavy on Colima — see the plan's gotchas).

## Constraints to preserve (don't regress these)

- Reuse the contracts: `Agent` / `Gate` / `Store`, the three `StopPolicy` stops, the held-out
  acceptance gate, the `[loopkit][component]` + run-id logging, safe-by-default (never `main`,
  clean tree, protected paths, budget ceiling).
- Each parallel worker = its own branch/worktree, commit-every-tick (durable, resumable).
- Stack stays **typer + rich + pydantic**, stdlib-first elsewhere.
- **Test-as-you-go with MockAgent (no tokens); log-as-you-go.** Add a scenario for each new
  concept (`scenarios/chNN_*.py`) so `demo`/`learn` keep pace.

## Suggested next step

> **Part II is feature-complete (2026-06-19).** All four workstreams done; 49 tests green; demos
> 8/10/11/12/17 clean. The fleet's deterministic core is proven (fakeredis + MockAgent); the
> kind/Tilt/k8s shell is built + static-validated. **The single remaining action is the live
> bring-up:** `make fleet-up && tilt up`, then `make fleet-run` / `make fleet-evolve`. It's heavy
> on Colima (kind cluster + image build + redis) — see [`tilt-fleet-plan.md`](tilt-fleet-plan.md)
> gotchas; tear down with `make fleet-down` when done.

**Live bring-up checklist** (the one thing not yet exercised):
1. `make fleet-up` — creates the isolated `kind-loopkit` cluster against the repo-local
   `KUBECONFIG`; verifies nodes with the real kubectl binary. Confirm `~/.kube/config` untouched.
2. `export KUBECONFIG=$PWD/.kube/loopkit.yaml && tilt up` — the Tiltfile's `fail()` guard refuses
   any context but `kind-loopkit`. Watch redis + 3 workers go green; the mock adapter needs no
   tokens. Confirm logs show `[loopkit][worker]` lines tagged with each pod's `WORKER_NAME`.
3. `make fleet-run` (fan-out) / `make fleet-evolve` (generational) on the host — the coordinator
   talks to redis via Tilt's `6379` port-forward. Expect every shard DONE on its own branch.
4. `make fleet-down` to reclaim Colima resources (`colima ssh -- sudo fstrim -v /` if disk tight).

After that, the only open items are **enhancements**, not gaps:
- **Dashboard (Phase 5, optional).** A thin read-only pod over Redis rendering
  `FleetResult`/`EvolutionResult`. Skipped for the first green fleet by design.
- **Tree-level reseed (evolve v2).** Today's fleet `evolve` reseeds *prompt-level* (the winner's
  note rides the next goal). Tree-level (gen+1 branches off the winner's code) needs the winner's
  tree on a shared volume/remote the next generation clones from — a real volume + a clone step.
- **Arbitrary repos.** The worker bundles demo-repo; external repos need clone-from-remote or a
  shared volume into the pod.
- Carried library backlog: package demo-repo as data for PyPI; richer no-progress signal
  (test-pass-count delta); real per-adapter cost parsing so the budget stop bites on live runs.

Wiring notes carried forward (load-bearing seams, don't regress):
- `run_loop` keyword-only opt-ins, each None = exact v1: `review_hook` (Ch 8), `skills` (Ch 17).
  Any new core attach point follows the same shape: typing-only import, duck-called, None-safe.
- `Supervisor.evolve` re-validates *survivors only* (high-score-first, stops at first pass). The
  fleet `Coordinator.evolve` preserves the same *outcome* but moves the held-out check **into the
  worker** (`WorkerOutcome.revalidated`) because the candidate's tree lives in the pod, not the
  coordinator. Widen here if you ever want every candidate's verdict surfaced (e.g. a dashboard).
- The fleet adds two new log components — `[loopkit][fleet]` (coordinator) and `[loopkit][worker]`
  (pods) — with the **task id as the correlation id** on every line a task touches.

## Gotchas already paid for (keep the fixes)

`git status` collapses new untracked dirs → use `--untracked-files=all`. Python gates litter
`__pycache__` into protected paths → gates run `PYTHONDONTWRITEBYTECODE=1` + repo `.gitignore`.
`examples/` isn't packaged on non-editable install → scenarios resolve `LOOPKIT_DEMO_REPO`
(set in the Dockerfile).
