# loopkit Tilt fleet — build plan (Part II, item #4)

> **STATUS: BUILT + RUN LIVE (2026-06-19).** Phases 0–4 implemented, tested, committed on `main`
> (`613bb7c` library/CLI/scenario, `472fac4` deploy shell, `076aadd` docs, `4833f69` bring-up
> fixes). 12 fleet tests + 49 total green; `demo 12` clean. The deterministic core (queue +
> coordinator + worker + the Ch 9 guard) is proven against fakeredis + MockAgent, **and the full
> fleet was brought up live** on the isolated `kind-loopkit` cluster: `tilt up` (UI :10350, redis
> :16379, 3 worker pods) → `make fleet-run` (tasks DONE on isolated branches) → `make fleet-evolve`
> (2 gens, reseed off the gen-0 winner, winner confirmed). Host `~/.kube/config` byte-identical
> throughout. **Only Phase 5 (the optional dashboard) is unbuilt.** What shipped vs. the plan:
> **revalidation runs in the worker** (it holds the candidate's tree), not the coordinator — the
> `evolve` *outcome* is identical, only the gate's location moved; the demo runner's selection
> score is **coarse** (1.0 done / 1.0+unrevalidated overfit / 0.0 else), the nuanced inflation case
> proven in `tests/test_fleet.py` + the Ch 11 scenario. **Live-run sharp edges (see Gotchas):**
> Docker-29 containerd image store breaks `kind load docker-image` → `custom_build` flattens to one
> platform; redis forwards to :16379 to dodge a local `redis-server` on 6379.

**Read this after `part-ii-resume.md`.** It scoped the last Part II item: graduating the
in-process `Supervisor` into a multi-container fleet — worker loops as containers, a Redis task
queue, orchestrated by Tilt on a **dedicated, isolated local kind cluster**.

Scoped interactively 2026-06-19. The decisions below were **locked**; execution is done.

---

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Build env | From `~/Documents/loopkit` (this repo's own Claude env) | loopkit memory + transcript stay with the project (the migration convention). |
| Substrate | **Local kind cluster, fully isolated** (own cluster name + own kubeconfig) | Most realistic / best teaching value; Tilt is k8s-first. Isolation is non-negotiable — must never touch the sre-agent / spacer / glip-bot / RingCentral contexts. |
| Queue | **Redis** | Conventional fleet queue; itself just a container, only a thin Python client dep. |
| Dashboard | Optional, v1-thin, deferrable | Read-only over Redis; build last or skip for the first green fleet. |

The **one new dependency** this introduces is a thin Redis client (`redis-py`). Everything else
stays `typer + rich + pydantic` + stdlib — keep the "thin curated deps" discipline.

---

## ⚠️ Cluster isolation — load-bearing, do not shortcut

The global kubectl/context-safety rules apply in full. The fleet gets a cluster that **cannot
collide** with any other cluster on this machine. The mechanism, in priority order:

1. **Repo-local kubeconfig is the primary guarantee.** Point `KUBECONFIG` at a file inside the
   repo and kind writes/reads ONLY there. The user's `~/.kube/config` (sre-agent, spacer,
   glip-bot, RingCentral, …) is never opened, never merged, never at risk.
   ```
   export KUBECONFIG=$PWD/.kube/loopkit.yaml   # repo-local, gitignored
   ```
2. **Dedicated cluster name.** `kind create cluster --name loopkit` → context `kind-loopkit`,
   cluster `loopkit`. kind clusters are independent Docker containers; nothing is shared.
3. **Dedicated namespace** `loopkit` inside the cluster (defense in depth, even though the whole
   cluster is ours).
4. **Tiltfile pins the context.** First lines of the Tiltfile:
   ```python
   allow_k8s_contexts('kind-loopkit')
   if k8s_context() != 'kind-loopkit':
       fail("refusing to run: expected kind-loopkit, got %s" % k8s_context())
   ```
   Tilt already refuses non-dev contexts by default; this makes the only allowed target explicit.
5. **Explicit flags on any raw kubectl** — equals form per the global rule:
   `--kubeconfig=$PWD/.kube/loopkit.yaml --context=kind-loopkit`. In practice Tilt + kind manage
   the cluster, so raw kubectl is rare.
6. **The shell `kubectl` wrapper is broken here** (`_kube_guard_enforce: command not found` in
   non-interactive shells). Call the real binary: `/opt/homebrew/bin/kubectl`. With the repo-local
   `KUBECONFIG` exported, even that resolves only against the loopkit cluster.
7. **Teardown leaves the host untouched.** `kind delete cluster --name loopkit` removes everything;
   `~/.kube/config` was never written.

Wrap (1)+(2) in `make fleet-up` / `make fleet-down` so the flags can't be forgotten.

---

## Architecture

```
                    ┌──────────────────────────────────────────────────┐
                    │  kind cluster "loopkit"  (context kind-loopkit)    │
                    │  namespace: loopkit                                │
                    │                                                    │
  loopkit fleet ──► │  ┌──────────┐  LPUSH task     ┌──────────────┐    │
  (coordinator,     │  │  redis   │ ◄────────────── │  worker pods  │    │
   runs on host     │  │ (queue + │  BRPOP task     │  (loopkit     │    │
   via Tilt port-   │  │ results) │ ──────────────► │   run_loop on │    │
   forward, or as   │  └──────────┘  HSET result    │   demo-repo)  │    │
   a Job)           │       ▲                       └──────────────┘    │
   REDIS_URL ───────┼───────┘                                           │
                    │  (optional) dashboard pod: reads Redis, renders    │
                    │            FleetResult / EvolutionResult           │
                    └──────────────────────────────────────────────────┘
```

Queue message flow:

```
coordinator         redis:list "tasks"        worker[i]              redis:hash "results"
    |   LPUSH {id,goal,seed}  |                   |                          |
    |------------------------>|                   |                          |
    |                         |     BRPOP         |                          |
    |                         |<------------------|                          |
    |                         |   task ---------->|  run_loop(goal)          |
    |                         |                   |  own branch, ticks       |
    |                         |                   |  HSET {id,done,score,    |
    |                         |                   |        branch,iters}     |
    |                         |                   |------------------------->|
    |  poll results[id] <-----------------------------------------------    |
    |<------------------------------------------------------------------    |
  collect → FleetResult   (evolve: score → keep top-k → revalidate → reseed → gen+1)
```

### How it maps onto existing code (attach at the seams — don't rewrite core)

- **Worker = `run_loop` unchanged.** A container is its own filesystem, so **container isolation
  replaces worktree isolation** — each worker clones (or already bundles) the target repo and works
  on **its own branch** (preserve commit-every-tick durability). The in-process
  `make_worktree`/`_WORKTREE_LOCK` machinery in `extensions/orchestrate.py` is not needed inside a
  worker pod; it remains the in-process path.
- **Reuse the result shapes.** `WorkerResult` / `FleetResult` / `Candidate` / `Generation` /
  `EvolutionResult` from `extensions/orchestrate.py` are the wire format — the coordinator maps the
  same data through Redis instead of `Future`s. Reuse `Scorer` and the `RevalidateFactory`
  held-out gate verbatim.
- **Preserve the selection-inflation guard.** `evolve` keeps best-of-N → re-validate **survivors
  only** on the held-out gate they never competed on, high-score-first, stop at first pass. This is
  the load-bearing Ch 9 correctness property; the fleet must not drop it.
- **New CLI surface** (also clears a backlog item): `loopkit fleet run` and `loopkit fleet evolve`
  as the coordinator entrypoints, talking to Redis via `REDIS_URL`. Today orchestration is
  Python-API only; this makes the supervisor drivable from argv like `run`/`demo`.

---

## Build order (phased, each phase commits + tests + a scenario)

**✅ Phase 0 — isolation substrate.** `.kube/` gitignored; `make fleet-up`/`fleet-down` wrap
`kind create/delete cluster --name loopkit`. The Makefile **exports** the repo-local `KUBECONFIG`
for every recipe (not a per-command flag), and `KUBECTL` defaults to the real binary. *Live `make
fleet-up` not yet run — that's the manual bring-up.*

**✅ Phase 1 — Tilt skeleton + Redis.** `Tiltfile` (context-pinned: `allow_k8s_contexts` +
`fail()` first) + `k8s/redis.yaml` (Namespace + Deployment + Service, namespace `loopkit`, redis
run ephemeral). Static-validated (YAML parses, selectors match, Tiltfile syntax OK).

**✅ Phase 2 — worker image + Deployment.** Reused `Dockerfile`, now `pip install -e '.[fleet]'`
(editable for Tilt live_update + the redis client). Worker entrypoint `loopkit fleet worker`:
`BRPOP` → `run_loop` on a fresh demo-repo clone (own branch) → `HSET` a `WorkerOutcome`.
`k8s/worker.yaml` = 3 pods, `WORKER_NAME` from the pod name for per-pod log correlation.

**✅ Phase 3 — `loopkit fleet run` (blind fan-out).** `Coordinator.run_fleet` enqueues N tasks,
polls the results hash, assembles `FleetResult` (`.done`/`.failed`). Parity with
`Supervisor.run_fleet`, over the queue. **Ch 12** scenario (`ch12_fleet.py`) added — confirmed
against `~/.claude/skills/tutor/docs/loops/12-dynamic-workflows-and-fan-out.md` (the queue is the
Ch 12 *trigger* seam).

**✅ Phase 4 — `loopkit fleet evolve`.** `Coordinator.evolve` runs generations: enqueue population →
collect scored outcomes → keep top-k → confirm the first survivor (high-score-first) the worker
reports passed held-out → reseed. **v1 = prompt-level reseed** (winner's note on the next goal).
Tree-level reseed = **v2** (noted in the scenario, not faked). *Adaptation vs the plan: the
held-out re-validation runs **in the worker** (it has the candidate's filesystem) and rides back as
`WorkerOutcome.revalidated`; the coordinator applies the identical selection rule. Same outcome,
gate relocated — proven by `test_evolve_catches_selection_inflation_over_the_queue`.*

**⬜ Phase 5 — (optional) dashboard.** Thin read-only pod over Redis rendering
`FleetResult`/`EvolutionResult`. Deferred — skip for the first green fleet.

---

## Constraints to preserve (don't regress)

- Contracts intact: `Agent` / `Gate` / `Store`, the three `StopPolicy` stops, the held-out
  acceptance gate, `[loopkit][component]` + run-id logging (add `[loopkit][fleet]` and
  `[loopkit][worker]` components; carry a task id as the correlation id on every line a task
  touches), safe-by-default (never `main`, clean tree, protected paths, budget ceiling).
- Each worker = its own branch, commit-every-tick (durable, resumable).
- Stack stays `typer + rich + pydantic` + stdlib; the only new dep is the thin Redis client.
- **Test-as-you-go with MockAgent (no tokens); log-as-you-go.** The coordinator logic
  (enqueue / collect / score / keep / revalidate / reseed) must be unit-testable **without a
  cluster** — against `fakeredis` (or an in-memory queue fake) + `MockAgent`. The k8s/Tilt layer is
  the thin outer shell, exercised manually via `tilt up` + the fleet demo.
- Add a `scenarios/chNN_*.py` for the fleet concept so `demo`/`learn` stay in step.

---

## Gotchas (some already paid for — keep the fixes)

- **Docker = Colima.** kind uses the active `colima` docker context (already selected). Give the
  Colima VM enough CPU/mem for a cluster + Redis + N workers. See `project_colima_docker`:
  kind images live **in the VM**; `docker system prune` frees VM space but the **host** disk needs
  `fstrim` on `/mnt/lima-colima` (e.g. `colima ssh -- sudo fstrim -v /`) to actually reclaim. kind
  clusters are heavy — `kind delete cluster --name loopkit` when done.
- **Broken kubectl wrapper** (`_kube_guard_enforce` not found) → use `/opt/homebrew/bin/kubectl`,
  `--context=` equals form. Repo-local `KUBECONFIG` makes even that loopkit-only.
- **Tilt → kind image load is BROKEN on Docker 29 (containerd image store).** `kind load
  docker-image` (Tilt's default for `docker_build`) fails with `content digest … not found` —
  Docker's OCI multi-arch export isn't ingestible by the node's `ctr import --all-platforms
  --digests`. Fix (in the Tiltfile): a `custom_build` that flattens to one platform — `docker build`
  → `docker save --platform linux/arm64` → `kind load image-archive` — with `skips_local_docker`.
  redis (not a Tilt build target) is side-loaded the same way; the kind node also can't pull from
  Docker Hub directly here (corp CA / TLS: `x509: certificate signed by unknown authority`), so the
  host pulls + side-loads. Still pin `allow_k8s_contexts('kind-loopkit')` + the `fail()` assertion.
- **Port 6379 is taken by a local `redis-server` on this host.** Tilt forwards the cluster's redis
  to **localhost:16379**; `make fleet-run/evolve` pass `--redis-url redis://localhost:16379`. Never
  let the coordinator default to 6379 here — that's the developer's personal redis, not the fleet's.
- **`make fleet-run/evolve` call `.venv/bin/loopkit`** (`$(LOOPKIT)`), not a bare `loopkit` — the
  CLI isn't on the global PATH.
- **demo-repo packaging:** the worker image bundles it via the Dockerfile + `LOOPKIT_DEMO_REPO`;
  the fleet operates on demo-repo for the teaching scenario. Arbitrary external repos = a later
  extension (needs repo delivery into the worker — clone-from-remote or a shared volume).
- Carried from core: `git status --untracked-files=all`; gates run `PYTHONDONTWRITEBYTECODE=1`
  (already handled in core, inherited by the worker).

---

## First commands when you relaunch (from `~/Documents/loopkit`)

```bash
cd ~/Documents/loopkit && source .venv/bin/activate
echo ".kube/" >> .gitignore            # if not already ignored
mkdir -p .kube
export KUBECONFIG=$PWD/.kube/loopkit.yaml      # repo-local — never touches ~/.kube/config
kind create cluster --name loopkit             # isolated cluster, context kind-loopkit
/opt/homebrew/bin/kubectl --context=kind-loopkit get nodes   # real binary; wrapper is broken
# then: Phase 1 — Tiltfile (context-pinned) + k8s/redis.yaml → `tilt up`
```
