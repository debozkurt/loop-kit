# loopkit

A self-governed coding loop you can point at any repository — the runnable form of the
agentic-loops engineering manual. You give it a goal and two gates; it drives a coding agent
toward the goal, tick by tick, with the guardrails that keep an autonomous loop from running
off a cliff: an external verification gate, a **held-out acceptance gate**, three hard stops,
durable git state, and a blast-radius safety envelope.

```
prompt ─▶ agent ─▶ protected-path guard ─▶ commit ─▶ iteration gate ─┬─▶ held-out acceptance ─▶ DONE
   ▲                                                                  │
   └──────────────── feedback (the gate failure) ◀────────────────────┘
                         hard stops every tick:  budget ▸ no-progress ▸ cap
```

This is the **single-agent core** (Part I). Orchestration, continuous review, and the skill
flywheel are defined seams under `loopkit/extensions/` and land in Part II.

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
loopkit demo 9                 # run the Chapter 9 held-out-gate scenario
loopkit learn 9                # the same scenario, narrated, with pauses
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
| `extensions/` | Part II seams: orchestration, review, skills | 8, 10–12, 17 |

Terminal precedence: `DONE ▸ SAFETY ▸ BUDGET_CEILING ▸ NO_PROGRESS ▸ ITERATION_CAP`.

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

## Roadmap (Part II)

The seams are defined; the implementations are the second lecture: a supervisor over many
worker loops (fan-out + evolutionary select-and-reseed, Ch 10–12), continuous review of every
commit (Ch 8), and the skill write-back flywheel (Ch 17) — plus Tilt for a deployable worker
fleet.
