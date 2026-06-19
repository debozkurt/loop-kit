# loopkit

A self-governed coding loop you can point at any repository — the runnable form of the
agentic-loops engineering manual. You give it a goal and two gates; it drives a coding agent
toward the goal, tick by tick, with the guardrails that keep an autonomous loop from running
off a cliff: an external verification gate, a **held-out acceptance gate**, three hard stops,
durable git state, and a blast-radius safety envelope.

```
prompt ─▶ agent ─▶ guard ─▶ commit ─▶ review ─▶ iteration gate ─┬─▶ held-out acceptance ─▶ DONE
   ▲                                                            │
   └────────── feedback (gate or review failure) ◀──────────────┘
                    hard stops every tick:  budget ▸ no-progress ▸ cap
```

That's the **single-agent core**. On top of it, the `loopkit/extensions/` layer adds three
opt-in capabilities — a **supervisor** that runs many loops in parallel (blind fan-out and
evolutionary select-and-reseed), **continuous review** that gates done on a clean diff, and a
**skill write-back flywheel** so solved runs teach future ones. Each is `None`-safe: leave it
out and the core behaves exactly as above. See *Beyond one loop* below.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # or: pip install -e .
```

## Quickstart

Point it at a repository:

```bash
cd your-repo
loopkit init                   # scaffolds loopkit.toml + PROMPT.md
loopkit doctor                 # preflight: safe to run here? gates set? agent on PATH?
loopkit run                    # loops to the goal (use --dry-run to rehearse the control flow)
```

Or learn the concepts from the runnable course:

```bash
loopkit demo                   # list the scenarios
loopkit demo 9                 # Ch 9  — the held-out acceptance gate (overfitting)
loopkit demo 8                 # Ch 8  — continuous review gates done
loopkit demo 10                # Ch 10 — fan-out over isolated workers
loopkit demo 11                # Ch 11 — evolutionary search, validated
loopkit demo 17                # Ch 17 — the skill write-back flywheel
loopkit learn 9                # any scenario, narrated, with pauses
loopkit demo 9 --live          # use the real claude-code agent (where a scenario supports it)
```

## The two gates (the heart of it)

A loop that iterates against the only check it has will *overfit* that check — it makes those
exact assertions pass, which is not the same as solving the goal. So loopkit runs two gates:

- the **iteration gate** — fast, in-sample, what the loop optimizes against every tick;
- the **acceptance gate** — held-out, run once on a candidate that passed iteration, against
  checks the loop never optimized against (and may not even read).

The shipped `examples/demo-repo` is built to show this: its visible tests pass *with* a seeded
boundary bug, and only the held-out tests catch it. Run `loopkit demo 9` to watch the held-out
gate refuse to call it done.

## The whole tool is the course

Each module implements one part of the manual and is a named, swappable seam:

| Module | What it is | Chapter |
|---|---|---|
| `config.py` | the one Config object — the whole loop as one file | 18 |
| `agent.py` | the model as a subroutine (`claude-code` · `codex` · `mock`) | 1–3 |
| `prompt.py` | fixed prompt, fresh context, anchor files | 4–5 |
| `gate.py` | the iteration gate and the held-out acceptance gate | 6–7, 9 |
| `stops.py` | the three hard stops + precedence | 13–14 |
| `durability.py` | commit every tick; state signature; resume from git | 15 |
| `safety.py` | blast-radius preflight + protected-path guard | 16 |
| `loop.py` | the controller that wires them — the tick lifecycle | 1–3, 7, 13 |
| `extensions/review.py` | continuous review hook (gates done on a clean diff) | 8 |
| `extensions/orchestrate.py` | supervisor: fan-out + evolutionary, over git worktrees | 10–12 |
| `extensions/skills.py` | skill registry + gated write-back flywheel | 17 |

Terminal precedence: `DONE ▸ SAFETY ▸ BUDGET_CEILING ▸ NO_PROGRESS ▸ ITERATION_CAP`.

## Beyond one loop

The `extensions/` layer scales the single loop up without touching its contracts. Each is a
Python API today (no CLI surface yet) and is `None`-safe — supply it or don't.

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

## Configuration (`loopkit.toml`)

```toml
goal = "Describe exactly what 'done' means."
branch = "loopkit/run"           # never main/master

[agent]
adapter = "claude-code"          # mock | claude-code | codex
max_cost_usd = 5.0               # budget ceiling

[gate]
iteration  = "python -m pytest tests/seen -q"
acceptance = "python -m pytest tests/holdout -q"   # held-out

[stops]
max_iter = 20
no_progress_after = 3

[safety]
protected_paths = ["tests/"]     # the loop may not touch these
require_clean_tree = true
allow_branches = ["loopkit/*"]
```

## Safety defaults (Chapter 16)

loopkit is safe-by-default. It refuses to run on `main`/`master`, wants a clean tree on an
allowed branch, commits every tick to its own branch (never force-pushes), reverts and halts
if the agent touches a protected path, and stops at the budget ceiling regardless of progress.
`loopkit doctor` reports all of this before you run.

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

## Develop

```bash
pip install -e ".[dev]"
pytest -q                        # MockAgent-driven; no coding-agent binary, no tokens
```

## Roadmap

The Part II library is done — the supervisor (fan-out + evolutionary, Ch 10–12), continuous
review (Ch 8), and the skill write-back flywheel (Ch 17) are all implemented and tested. The one
piece still ahead is a **deployable fleet**: worker loops as containers behind a task queue,
with Tilt for multi-service live-reload and an optional dashboard over `FleetResult` /
`EvolutionResult`. A `loopkit fleet` / `evolve` CLI surface (the supervisor is Python-API only
today) is the natural smaller follow-on.
