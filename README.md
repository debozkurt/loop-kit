# loopkit

A self-governed coding loop you can point at any repository — the runnable form of the
[*Agentic Loops* engineering manual](https://github.com/debozkurt/loop-guide). You give it a goal
and two gates; it drives a coding agent toward the goal, tick by tick, with the guardrails that keep
an autonomous loop from running off a cliff: an external verification gate, a **held-out acceptance
gate**, three hard stops, durable git state, and a blast-radius safety envelope.

> **Paired with the manual.** loopkit is the reference implementation of the *Agentic Loops* manual —
> a written course on self-governed coding loops
> ([github.com/debozkurt/loop-guide](https://github.com/debozkurt/loop-guide)). The manual teaches
> each concept against a minimal, stdlib-only harness; loopkit is those same patterns built out as a
> product, and the chapter numbers throughout this README (and in `loopkit demo N`) map to its
> chapters. If you're new to the ideas, read the manual for the concepts and the capstone; loopkit is
> where you run them at production scale.

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#1b1b1b','primaryColor':'#2b2b2b','primaryTextColor':'#e6e6e6','primaryBorderColor':'#5a5a5a','lineColor':'#8a8a8a','fontSize':'13px'}}}%%
flowchart LR
  P["prompt"] --> A["agent"] --> G["guard"] --> C["commit"] --> R["review"] --> I["iteration gate"] --> H["held-out acceptance"] --> D(["DONE"])
  I -- "feedback" --> P
  H -- "feedback" --> P
  C --> S["hard stops<br/>budget · no-progress · cap"]
```

That's the **single-agent core**. On top of it, the `loopkit/extensions/` layer adds three
opt-in capabilities — a **supervisor** that runs many loops in parallel (blind fan-out and
evolutionary select-and-reseed), **continuous review** that gates done on a clean diff, and a
**skill write-back flywheel** so solved runs teach future ones. Each is `None`-safe: leave it
out and the core behaves exactly as above. See *Beyond one loop* below.

## Where to go

| You want to… | Go to |
|---|---|
| **Run it now** — watch a loop reach `DONE` | [`examples/walkthrough/`](examples/walkthrough/) |
| **Use it on your repo** | [`docs/USING-ON-YOUR-REPO.md`](docs/USING-ON-YOUR-REPO.md) |
| **Learn the concepts** — the written manual | [the *Agentic Loops* manual](https://github.com/debozkurt/loop-guide) |
| **Run the concepts** — the manual's labs, in code | `loopkit demo` · [`loopkit/scenarios/`](loopkit/scenarios/) |
| **Understand how it's built** | [`docs/architecture/`](docs/architecture/README.md) |
| **All docs · all examples** | [`docs/README.md`](docs/README.md) · [`examples/`](examples/) |

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # or: pip install -e .
```

The core is just `typer + rich + pydantic`. Everything heavy is an optional extra, imported lazily:
`[claude]`/`[openai]` (the API-adapter SDKs), `[trace]` (LangSmith tracing), `[fleet]` (Redis).
`pip install loopkit` pulls none of them; `[agents]` = both API SDKs, `[all]` = everything.

## Quickstart

Point it at a repository:

```bash
cd your-repo
loopkit init                   # scaffolds loopkit.toml + PROMPT.md
loopkit init --plan            # OR: plan-driven backlog mode — a checklist the loop works through
loopkit doctor                 # preflight: safe to run here? gates set? agent on PATH?
loopkit run                    # loops to the goal (use --dry-run to rehearse the control flow)
loopkit measure -n 10          # reliability: run the goal 10× → pass^k / pass@k (harness-stamped)
loopkit synth-gate <cmd>       # is this held-out oracle real? fail-first verify it (--fix ⇒ fail→pass)
loopkit detect                 # read the stack off file markers → a proposed loopkit.toml (--write to save)
loopkit route --from-report r.json   # measured pass^k → run once, or escalate to evolve (best-of-N)
```

Or let a coding-agent copilot mold loopkit *for* your repo — detect the stack, wire the gates + a
fail-first oracle, pick the features that fit — with the [`loopkit-mold` skill](examples/molding/)
(Part IV; [design](docs/part-iv-molding-kit.md)). Its two load-bearing primitives are code, not a prompt:
`loopkit detect` reads the mechanical, safety-critical config (test runner, protected paths, default
branch, adapter) off file markers so nothing is guessed, and `loopkit synth-gate` proves a proposed
acceptance oracle actually reproduces the goal (fails on the buggy tree) before you trust it — the
determinism and verification a copilot can't self-supply.

Or learn the concepts from the runnable course:

```bash
loopkit demo                   # list the scenarios
loopkit demo 9                 # Ch 9  — the held-out acceptance gate (overfitting)
loopkit demo 8                 # Ch 8  — continuous review gates done
loopkit demo 10                # Ch 10 — fan-out over isolated workers
loopkit demo 11                # Ch 11 — evolutionary search, validated
loopkit demo 17                # Ch 17 — the skill write-back flywheel
loopkit demo 20                # Ch 20 — triggers as infrastructure (a signed webhook → one run)
loopkit demo 21                # Ch 21 — the CI deployment tier (an issue → a draft PR, no cluster)
loopkit demo 24                # Ch 24 — reliability: pass^k falls while pass@k rises
loopkit learn 9                # any scenario, narrated, with pauses
loopkit demo 9 --live          # use the real claude-code agent (where a scenario supports it)
```

Chapters 20–21 are the **Part III** ecosystem labs — running the loop inside GitHub/GitLab. The
companion teaching module is [`docs/part-iii-ecosystem.md`](docs/part-iii-ecosystem.md).

## The two gates (the heart of it)

A loop that iterates against the only check it has will *overfit* that check — it makes those
exact assertions pass, which is not the same as solving the goal. So loopkit runs two gates:

- the **iteration gate** — fast, in-sample, what the loop optimizes against every tick;
- the **acceptance gate** — held-out, run once on a candidate that passed iteration, against
  checks the loop never optimized against (and may not even read).

The shipped `examples/demo-repo` is built to show this: its visible tests pass *with* a seeded
boundary bug, and only the held-out tests catch it. Run `loopkit demo 9` to watch the held-out
gate refuse to call it done.

## The whole tool mirrors the manual

Each module implements one part of the [manual](https://github.com/debozkurt/loop-guide) and is a named, swappable seam (its chapter in the last column):

| Module | What it is | Chapter |
|---|---|---|
| `config.py` | the one Config object — the whole loop as one file | 18 |
| `agent.py` | the model as a subroutine — the 2×2 matrix: `claude-code`·`codex` (CLI), `claude-api`·`openai-api` (SDK), `mock` | 1–3 |
| `pricing.py` | per-model cost table → exact per-tick `cost_usd` (makes the budget stop bite) | 14 |
| `prompt.py` | fixed prompt, fresh context, anchor files | 4–5 |
| `gate.py` | the iteration gate and the held-out acceptance gate | 6–7, 9 |
| `stops.py` | the three hard stops + precedence | 13–14 |
| `durability.py` | commit every tick; state signature; resume from git | 15 |
| `safety.py` | blast-radius preflight + protected-path guard | 16 |
| `log.py` / `trace.py` | two-layer observability: payload-free logs **+** optional full-tree LangSmith traces | 14–15 |
| `loop.py` | the controller that wires them — the tick lifecycle | 1–3, 7, 13 |
| `extensions/review.py` | continuous review hook (gates done on a clean diff) | 8 |
| `extensions/orchestrate.py` | supervisor: fan-out + evolutionary, over git worktrees | 10–12 |
| `extensions/skills.py` | skill registry + gated write-back flywheel | 17 |

Terminal precedence: `DONE ▸ SAFETY ▸ BUDGET_CEILING ▸ NO_PROGRESS ▸ ITERATION_CAP`.

## Beyond one loop

The `extensions/` layer scales the single loop up without touching its contracts. The in-process
orchestration/review/skills hooks are `None`-safe Python APIs — supply them or don't; the
deployable fleet (below) adds a `loopkit fleet` CLI surface.

**Orchestration — `Supervisor`.** Runs many worker loops over independent tasks, each in its
own **git worktree**: a separate working directory backed by the one object store, so parallel
workers can't collide on files while every commit still lands recoverably in the repo. Two
strategies share that base:

- *fan-out* (`run_fleet`) — N independent tasks, each to its own isolated worker; one crash is
  contained, never sinks the fleet.
- *evolution* (`evolve`) — N attempts at the **same** goal per generation, keep the top-k,
  reseed the winner into the next generation. Critically, only a **re-validated** winner reseeds:
  best-of-N inflates the top score (the winner's curse), so the kept winner must pass a held-out
  gate it never competed on — Ch 9's lesson applied at the fleet scale.

**Continuous review — `ReviewHook`.** After each commit, a review runs on the fresh diff; a
clean review is a *precondition for done*. Green tests are not a clean diff — review catches what
the gate doesn't encode (leftover debug, smells, security), and a failing review feeds its
findings into the next tick so the agent fixes it while the producing context is fresh.

**Skill write-back flywheel — `SkillRegistry`.** A solved run is distilled into a named skill,
rendered back into future runs' prompts, so gains compound. Write-back is **gated, never
ungated**: reaching done makes a run acceptable, not automatically worth learning from — a thin
win can distill into a skill that poisons every later prompt, so only a run that clears a
write-back gate mints one. `FileSkillRegistry` persists skills as markdown, the durable flywheel.

```python
from loopkit.config import load_config
from loopkit.extensions.orchestrate import run_fleet
from loopkit.agent import build_agent

cfg = load_config("loopkit.toml")
fleet = run_fleet(cfg, tasks=[{"goal": "...", "slug": "a"}, {"goal": "...", "slug": "b"}],
                  make_agent=lambda task: build_agent(cfg.agent), max_workers=4)
print(len(fleet.done), "of", len(fleet.workers), "reached done")
```

**From findings to PRs — the manifest pipeline (CLI).** For a backlog of related fixes, the
molding kit builds what each task needs (a fail-first-verified oracle + a per-task config) and
`loopkit batch` runs one governed core loop per task. Dotted = optional/advisory. Details, knobs,
and the full pipeline diagram: [`docs/USING-ON-YOUR-REPO.md`](docs/USING-ON-YOUR-REPO.md).

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#1b1b1b','primaryColor':'#2b2b2b','primaryTextColor':'#e6e6e6','primaryBorderColor':'#5a5a5a','lineColor':'#8a8a8a','fontSize':'13px'}}}%%
flowchart LR
  F["findings / issues"] --> M["loopkit mold-batch<br/>fail-first oracle +<br/>config per task"]
  M --> B["batch.toml"]
  B -.-> OV["loopkit overlap<br/>conflict check"]
  B --> R["loopkit batch<br/>N isolated core loops"]
  R --> P["draft PR per task<br/>journal · --resume"]
```

## The deployable fleet (Chapter 12)

The same `Supervisor` graduated off the single process: each worker becomes its own **container**
(isolation goes from logical — git worktrees — to **physical**: its own filesystem, clone, and
branch), and the in-memory handoff becomes a **Redis queue**. The coordinator `LPUSH`es tasks and
polls a results hash; workers `BRPOP` a task, run `run_loop`, and `HSET` the outcome. The queue is
also the *trigger* seam — a worker is indifferent to what woke it, so a cron, a webhook, or a human
pushing one task drives the same loop.

`extensions/fleet.py` reuses the orchestrator's result shapes (`WorkerResult` / `FleetResult` /
`Candidate` / `Generation` / `EvolutionResult`) as the wire format, and **preserves the Ch 9
selection-inflation guard** at fleet scale: `evolve` keeps best-of-N, then confirms the highest-
scoring survivor that *also* passed a held-out check it never competed on (run in the worker, since
only it has the candidate's tree). Only a re-validated winner reseeds, so a lucky overfit can't
compound. The coordinator/worker logic is fully testable with **no cluster and no tokens** — against
`fakeredis` (or an `InMemoryQueue`) + a `MockAgent`.

```bash
# Local, isolated kind cluster (repo-local kubeconfig — never touches ~/.kube/config):
make fleet-up                 # create the kind-loopkit cluster
tilt up                       # build + deploy redis + 3 worker pods; port-forward redis
make fleet-run                # coordinator: blind fan-out over the queue
make fleet-evolve             # coordinator: evolutionary search (the Ch 9 guard, deployed)
make fleet-down               # delete the cluster

loopkit demo 12               # the fleet, in-process (no cluster): the teaching scenario
```

The worker's default `--adapter mock` solves the bundled demo-repo with **zero tokens**, so the
fleet goes green on `tilt up` without credentials. Swap to `claude-code` (plus a mounted key) for a
live fleet. Redis is the one optional dependency (`pip install 'loopkit[fleet]'`); the core stays
`typer + rich + pydantic`.

## Configuration (`loopkit.toml`)

The whole run is one object. A minimal config:

```toml
goal   = "Describe exactly what 'done' means."
branch = "loopkit/run"                            # never main/master

[agent]
adapter = "claude-code"                           # mock | claude-code | codex | claude-api | openai-api
max_cost_usd = 5.0                                # budget ceiling — bites on real cost (see `doctor`)

[gate]
iteration  = "python -m pytest tests/seen -q"
acceptance = "python -m pytest tests/holdout -q"  # held-out — run once before DONE

[safety]
protected_paths = ["tests/"]                      # the loop may not touch these

[review]
command = "bash review-judge.sh"                  # adversarial judge run after each advancing tick
```

**`[review]` is opt-OUT.** When enabled (`enabled = true`, the default), an adversarial review runs on
**every** `run` and **every** `batch` task — a clean review (exit 0) is a precondition for DONE, a
failing one feeds back so the agent self-corrects. This closes the failure mode where an opt-in gate is
silently *forgotten* and half-fixes/gamed tests ship green. `command` names the judge (point it at an
LLM judge whose wrapper exits non-zero on problems); leave it unset to use the **built-in default
judge** (planned — see `docs/default-judge-design`). Turn review off everywhere with `enabled = false`,
or for a single invocation with `--no-review`; an explicit `run --review <cmd>` (or a manifest
`review =`) overrides the command. `run` prints the decision (`review: on/off — <reason>`) and
`loopkit doctor` shows it, so an accidentally-off review is never silent.

Every other knob — `[stops]`, the optional second `regression` oracle, `[remote]` sync, `[trace]`,
gate-stability preflight — is annotated in [`docs/CONTROL-FILES.md`](docs/CONTROL-FILES.md); the
fully-commented reference is [`examples/gates/loopkit.example.toml`](examples/gates/loopkit.example.toml).

## Use it on your own repo

loopkit isn't tied to the demo — point it at any project. The four fields that define a run:
`goal`, `gate.iteration`, `gate.acceptance`, `safety.protected_paths`.

```bash
cd ~/code/my-project
loopkit init .                 # writes loopkit.toml + PROMPT.md (never overwrites)
loopkit doctor                 # preflight: branch safe? agent on PATH? gates set?
loopkit run --dry-run          # rehearse the control flow — no agent, no tokens
loopkit run                    # real run: commits every tick to its branch, drives to DONE
loopkit measure -n 10          # how *reliably* does it solve this goal? pass^k over 10 trials
```

From there, three outward edges — all opt-in, all off by default, each walked through end-to-end in
[`docs/USING-ON-YOUR-REPO.md`](docs/USING-ON-YOUR-REPO.md):

- **Sync to a forge** — a `[remote]` block (or one-off `loopkit run --open-pr`) pushes the loop's
  branch and opens a **draft** PR/MR. The Ch 16 guard holds at this edge: it *cannot* push to `main`
  and never force-pushes. Needs `gh`/`glab` authenticated.
- **Drive the fleet from issues** — label a backlog and `loopkit fleet run --from-issues` turns each
  open issue into a task on its own branch (`loopkit/issue-N` → a PR that closes it).
- **Hands-off in CI, no cluster** — `loopkit init --ci github|gitlab`; a labelled issue starts a CI
  job that runs one loop and opens a draft PR. The forge is the trigger, secrets, identity, sandbox.

## Steering files: the `.md` control surface

Every worthwhile behaviour is steered by a file you can read and edit. Deep dive:
[`docs/CONTROL-FILES.md`](docs/CONTROL-FILES.md).

| File | Steers | Chapter |
|---|---|---|
| `loopkit.toml` | the whole run: goal, gates, stops, safety, `[remote]` (the master switch) | Ch 18 |
| `PROMPT.md` | the task spec in prose — reloaded into a **fresh context every tick** | Ch 4–5 |
| `CLAUDE.md` / `AGENTS.md` | standing conventions + guardrails the agent must obey, anchored each tick | Ch 16–17 |
| `tests/seen/` vs `tests/holdout/` | the **two gates** — the in-sample check vs the held-out spec | Ch 6–7, 9 |
| `<skill>.md` (skills dir) | lessons distilled from solved runs, rendered back into future prompts | Ch 17 |

The leverage order: most steering happens in `PROMPT.md` (what to do) and the **gates** (what "done"
means) — they are the loop's goal and its grader. `CLAUDE.md`/`AGENTS.md` hold the rules that apply
to *every* task; skills accumulate what past runs learned.

## Safety defaults (Chapter 16)

loopkit is safe-by-default. It refuses to run on `main`/`master`, wants a clean tree on an
allowed branch, commits every tick to its own branch (never force-pushes), reverts and halts
if the agent touches a protected path, and stops at the budget ceiling regardless of progress.
`loopkit doctor` reports all of this before you run.

## Observability — live stream, logs, traces

**Watch a run live.** The claude-code adapter streams by default, so loopkit emits one
`[loopkit][agent]` line per agent step — thought, tool call, tool result — as it happens:

```text
[loopkit][agent] agent.think Scoping the notes queryset to the owner … idx=1 len=212
[loopkit][agent] agent.tool  idx=2 tool=Edit arg=src/apps/notes/views/preferences.py
[loopkit][agent] agent.result idx=3 isError=False outLen=0
[loopkit][loop]  agent.done tick=1 costUsd=7.60
```

`tail -f` a run (or `grep '\[loopkit\]\[agent\]'`) and you see it work. ⚠️ These step lines carry the
model's thoughts + tool targets (secrets redacted, but **not ship-safe**) — a deliberate trade for
visibility. Pin `[agent] args = ["--output-format", "json"]` to revert claude-code to the quiet,
payload-free buffered path.

**Layers, by design:** the **loop** logs (`[loopkit][loop]`) stay always-on and payload-free
(ids/lengths/counts — safe to ship); the **agent** step stream above is the live, payload-bearing view;
and **traces** are the optional full-detail record. `loopkit batch` also persists each task's raw event
stream to a durable `<manifest_dir>/<task>.activity.jsonl` (survives the worktree — replay/debug after
the fact). See [`docs/OPERATING.md`](docs/OPERATING.md).

**Traces (LangSmith).** Enable `loopkit[trace]` and tracing auto-activates when a LangSmith key is
present, emitting a full run tree — `loopkit run → tick → agent → llm/tool → gates` — with the
human-readable I/O of every step, all tool calls, and exact `cost_usd`/usage/model metadata per span
(the same cost `pricing.py` feeds the budget stop). The access-gated trace backend is where full
payloads belong.

```bash
pip install -e ".[trace]"                  # optional; pulls langsmith
export LANGSMITH_API_KEY=...               # (and LANGSMITH_PROJECT to name the project)
loopkit run                                # traces appear automatically; loopkit doctor shows status
```

It's a clean no-op without the extra/key, nests via LangSmith contextvars (no tracer threaded
through any contract), and the fleet inherits it — each worker's run traces, tagged by task id.

## Sandboxed runs (Docker)

```bash
docker build -t loopkit .
docker run --rm loopkit demo 13                       # a scenario, fully isolated
docker run --rm -v "$PWD":/work -w /work loopkit run --dry-run   # rehearse against your repo
```

The container gives you a reproducible environment and OS-level blast-radius containment. Note
the gate runs the *target project's* toolchain, so a real run against your repo needs that
toolchain (and, for a live agent, the agent binary + credentials) available in the container —
extend the image for your stack. See the Dockerfile.

## Command reference

Everything `loopkit` exposes. Run any command with `--help` for the live version.

### Core — one loop

**`loopkit init [PATH]`** — scaffold a starter `loopkit.toml` + `PROMPT.md` in `PATH` (default `.`;
never overwrites).

**`loopkit doctor`** — preflight: is this repo safe to point the loop at, *and is the gate set up to
actually work?* Reports branch safety, the agent (CLI binary on PATH, or API SDK + key), whether the
**budget** can bite (model is priced), the **gates**, and **tracing** status. It also **runs the
iteration gate once** on the current tree and reports the verdict — a gate that *already passes* means
the loop may instantly (falsely) declare DONE, a gate that *fails* is the healthy start, a *broken*
command is flagged — and warns when the acceptance gate is identical to the iteration gate (which
defeats the held-out check, Ch 9). Exits non-zero if preflight fails (the gate verdict is advisory).
- `-c, --config PATH` — config file (default `loopkit.toml`).
- `--no-gate` — skip running the gate (e.g. when it's slow); all the static checks still run.

**`loopkit run`** — run the loop until it reaches a terminal (DONE / a hard stop).
- `-c, --config PATH` — config file (default `loopkit.toml`).
- `--repo TEXT` — override the target repo (the config's `repo`).
- `--dry-run` — exercise the control flow with no agent (no tokens).
- `--max-iter INTEGER` — override `stops.max_iter`.
- `--force` — run even if preflight fails.
- `--sandbox` — run inside the loopkit Docker container (OS-level containment, Ch 16).
- *With `[remote] enabled`*, a DONE run then pushes its branch + opens a PR/MR.

**`loopkit demo [CHAPTER]`** — run a chapter's scenario straight through (omit `CHAPTER` to list).
- `--live` — use the real claude-code agent where the scenario supports it.

**`loopkit learn [CHAPTER]`** — the same scenarios, narrated, with a pause between beats.
- `--live` — as above.

Run `loopkit demo` (no chapter) for the current list — scenarios track the course chapters and grow
with it; `loopkit/scenarios/README.md` maps each one. A good first three: `loopkit demo 9` (the
held-out gate), `loopkit demo 12` (the fleet), `loopkit demo 14` (the 2×2 adapter matrix + real cost
making the budget stop bite).

### Fleet — many loops (Chapter 12)

**`loopkit fleet worker`** — the executor: BRPOP a task, run the loop in an isolated clone, HSET the
result. Long-lived (a pod or a host process). No `--target` → the bundled demo-repo, token-free.
- `--redis-url TEXT` — Redis to drain (env `REDIS_URL`, default `redis://localhost:6379`).
- `--adapter TEXT` — `mock` (no tokens) | `claude-code` | `codex` (default `mock`).
- `--max-iter INTEGER` — per-task iteration cap (default 6).
- `--target TEXT` — repo path/URL to operate on (env `LOOPKIT_TARGET`; default demo-repo).
- `--gate-iteration TEXT` / `--gate-acceptance TEXT` — override the target's gates.
- `--name TEXT` — worker tag in logs (env `WORKER_NAME`; pods set it from the pod name).

**`loopkit fleet run`** — coordinator: enqueue tasks and collect a `FleetResult`.
- `-n, --tasks INTEGER` — how many independent tasks to fan out (default 3).
- `--redis-url TEXT` — env `REDIS_URL`, default `redis://localhost:6379`.
- `--goal TEXT` — override the per-task goal.
- `--from-issues` — source one task per open GitHub/GitLab issue.
- `--target TEXT` — repo to read issues from (default cwd; used with `--from-issues`).
- `--label TEXT` — only issues with this label become tasks.
- `--provider TEXT` — `auto` | `github` | `gitlab` (default `auto`).

**`loopkit fleet evolve`** — coordinator: evolutionary search with the Ch 9 selection-inflation
guard (keep top-k → re-validate survivors on a held-out gate → reseed only a validated winner).
- `-g, --generations INTEGER` (default 2), `-p, --population INTEGER` (default 4),
  `-k, --keep INTEGER` (default 2), `--redis-url TEXT`.

### `make` targets — cluster lifecycle (Tiltfile + kind)

The fleet runs on a **dedicated, isolated** local kind cluster; the Makefile exports a repo-local
`KUBECONFIG` for every recipe, so your other clusters are never touched.

| Target | What |
|---|---|
| `make fleet-up` | create the isolated `kind-loopkit` cluster, show its nodes |
| `make tilt-up` / `make tilt-down` | `tilt up` / `tilt down` (build + deploy redis + workers; UI on :10350) |
| `make fleet-run` | coordinator fan-out over the cluster (redis on `localhost:16379`) |
| `make fleet-evolve` | coordinator evolutionary search |
| `make fleet-nodes` | show the cluster's nodes (verifies context + repo-local kubeconfig) |
| `make fleet-down` | delete the cluster (host `~/.kube/config` was never written) |
| `make test` | the unit suite (fakeredis + MockAgent — no cluster, no tokens) |
| `make demo` | the fleet teaching scenario (`loopkit demo 12`) |
| `make help` | list targets |

Full cluster walkthrough: [`docs/archive/part-ii-tilt-fleet-plan.md`](docs/archive/part-ii-tilt-fleet-plan.md).
Using it on your own repo: [`docs/USING-ON-YOUR-REPO.md`](docs/USING-ON-YOUR-REPO.md).

## Documentation

Index: [`docs/README.md`](docs/README.md). Operator quickstarts:

- **[All examples](examples/)** — four runnable dirs, each answering a different question
  (walkthrough · demo-repo · gates · CI), mapped in [`examples/README.md`](examples/README.md).
- **[Walkthrough](examples/walkthrough/)** — copy-this-run-it-see-`DONE` on the bundled demo repo.
- **[Billing & credentials](docs/BILLING.md)** — `claude-code` runs on your **subscription** by default;
  `--api-key` opts into billing; `doctor` shows which pays; bound subscription runs with `--max-iter`.
- **[Operating a run](docs/OPERATING.md)** — tell a *silent* run from a hung one, never edit a live
  tree, how to resume, where output goes per tier.
- **[Troubleshooting](docs/TROUBLESHOOTING.md)** — `cost: $0.00`, `rc=1`, `safety_halt`, flaky gate, …
- **[Gates](examples/gates/)** — a gate is *any shell command*; ready-to-copy two-oracle kits
  (test split · docs/structural · peer-LLM-review).

## Develop

```bash
pip install -e ".[dev]"          # core + pytest + fakeredis + truststore;  add [fleet]/[claude]/[openai]/[trace] as needed
pytest -q                        # MockAgent + injected fakes; no agent binary, no SDK keys, no tokens
```

## Roadmap

**Done:** the single-agent core; the Part II library (supervisor fan-out + evolutionary Ch 10–12,
continuous review Ch 8, skill write-back Ch 17); the **dev fleet** (Redis queue, worker containers,
Tilt on an isolated kind cluster, verified live); **real-target** support — `loopkit run --repo`
/ `fleet worker --target` (any repo), `[remote]` push + draft PR via `gh`/`glab`, and
`fleet run --from-issues` (GitHub/GitLab issues → tasks); and **Part III Phase 0** — the **2×2
adapter matrix** (`claude-api`/`openai-api` + `codex` alongside `claude-code`), real per-adapter
**cost parsing** (`pricing.py`) so the budget stop bites, and a **full-tree LangSmith tracing** layer
(`trace.py`, auto-on, verified live).

**Next — a production, cloud-deployable system (Part III).** Phase 0 (adapters + cost + tracing) is
done; the next phase is the **amd64 worker image → GHCR** pipeline, then the DOKS cluster foundation,
per-run **`Job`s** + namespaces, **`CronJob`/webhook** triggers, and **Secrets** — running many jobs
in production with per-run namespacing, budget ceilings, and shipped logs/traces. Safety (Ch 16) must
hold at cloud scale: least-privilege ServiceAccounts, the pre-tool-use hook, branch-only pushes. The
full plan + current state: [`docs/part-iii-resume.md`](docs/part-iii-resume.md) and the architecture
wiki [`docs/architecture/`](docs/architecture/README.md).

Smaller enhancements: an optional **dashboard** over `FleetResult`/`EvolutionResult`; **tree-level
reseed** for `fleet evolve` (today's is prompt-level — tree-level needs the winner's tree on a
shared volume); finer fleet scoring (`_grade` → held-out pass fraction).

**Multi-task shapes — the backlog + batch work.** loopkit points at work three ways: **one task**
(`loopkit run`), a **sequential backlog** in one loop (`loopkit init --plan` — one checklist, one item
per tick, bounded by a plan-stall stop), and a **parallel batch** of independent tasks. Status:

- **Plan mode → GA:** ✅ plan-stall detection (halts when the *done-count* stalls, not just the git
  diff — the guard `no_progress` is blind to in plan mode; groundwork for a `NEEDS_HUMAN` terminal).
  Still to do: incremental / per-item draft PRs (v1 lands one PR at the end); an optional **planner**
  (decompose a one-line goal → the checklist). See
  [`docs/USING-ON-YOUR-REPO.md`](docs/USING-ON-YOUR-REPO.md) and the resume doc.
- **The no-infra batch CLI:** ✅ **`loopkit batch --tasks tasks.toml`** — a TOML manifest drives the
  in-process fleet (no Redis, no separately-started workers): one isolated clone + branch (+ draft
  PR) per task, with **conflict-aware scheduling** (`group` serializes predicted collisions; `after`
  encodes dependencies, and a failed dependency skips its dependents). Per-task configs and the
  `--review`/`--validate` seams ride along. `loopkit demo 28` is the runnable lab.
- **Unattended batch molding (Part IV, Layer 5):** ✅ **`loopkit mold-batch --tasks plan.toml`** —
  for a batch with no copilot per task: detect → tier-typed oracle proposal (mechanical skeleton +
  an optional proposer seam) → mandatory isolated fail-first verification → route → per-task
  configs, ending in a reviewable `batch.toml` for `loopkit batch`. `loopkit demo 29` is the lab.

## License

[MIT](LICENSE) © Derrick Bozkurt. Fork it freely — bring your own agent/git/cloud credentials
(nothing sensitive is baked in; see [`.env.example`](.env.example) for the keys a run looks for).
