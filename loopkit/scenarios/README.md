# `loopkit/scenarios/` — the runnable teaching labs

These are **runnable code, not docs.** Each `chNN_*.py` is a self-contained scenario that
`loopkit demo`/`loopkit learn` imports and plays — mostly against `MockAgent` (zero tokens, no
network), so they double as the executable companion to the
[*Agentic Loops* manual](https://github.com/debozkurt/loop-guide) — each `chNN` mirrors the manual's
chapter N. They live under
`loopkit/` (not `docs/` or `examples/`) because they import the package's own modules (`agent`,
`loop`, `gate`, …) and ship in the wheel, so `pip install loopkit && loopkit demo 9` works.

Run them:

```bash
loopkit demo            # list every scenario (this table, from the live registry)
loopkit demo 9          # play chapter 9 straight through
loopkit learn 9         # the same, narrated, with a pause between beats
loopkit demo 21 --live  # use the real claude-code agent (scenarios marked ✓ Live below)
```

The registry (`__init__.py` → `_registry()`) is the source of truth; this table mirrors it.

## The map

| Ch | File | Teaches | Live |
|---|---|---|---|
| 5 | `ch05_context.py` | Fresh, fixed context each tick beats a growing, rotting one | |
| 7 | `ch07_feedback.py` | A closed loop feeds each gate failure back as the next tick's input | ✓ |
| 8 | `ch08_review.py` | A clean review is a precondition for done — catches what green tests don't | |
| 9 | `ch09_held_out.py` | The held-out gate: passing the visible tests is not solving the goal | ✓ |
| 10 | `ch10_orchestration.py` | Fan-out: many loops in parallel, one git worktree each, no collisions | |
| 11 | `ch11_evolution.py` | Best-of-N inflates the winner; re-validate on a held-out gate | |
| 12 | `ch12_fleet.py` | The deployable fleet: the loop behind a Redis queue, workers as containers | |
| 13 | `ch13_hard_stops.py` | The three hard stops: iteration cap, no-progress, budget ceiling | |
| 14 | `ch14_economics.py` | The 2×2 adapter matrix → exact per-tick cost → the budget stop bites | |
| 16 | `ch16_safety.py` | Protected paths bound the blast radius no matter what the model tries | |
| 17 | `ch17_skills.py` | The write-back flywheel: distil a solved run into a skill, gated | |
| 20 | `ch20_triggers.py` | Triggers as infrastructure: a signed webhook → exactly one run | |
| 21 | `ch21_ci.py` | The CI tier: an issue → a draft PR, no cluster | ✓ |
| 22 | `ch22_isolation.py` | Agent isolation: the untrusted tool surface in a keyless executor | |
| 23 | `ch23_skills_repo.py` | The skills repo: the flywheel made durable across machines | |
| 24 | `ch24_reliability.py` | Reliability: `pass^k` falls with k while `pass@k` rises | |
| 25 | `ch25_synth_gate.py` | synth-gate: proving a held-out oracle is real (fail-first / fail→pass) | |
| 26 | `ch26_detect.py` | detect: reading a repo's mechanical config deterministically → a proposed `loopkit.toml` | |
| 27 | `ch27_route.py` | route: a measured pass^k → a single-run-vs-`evolve` decision | |
| 28 | `ch28_batch.py` | batch: a manifest of tasks → parallel loops, conflict-aware | |
| 29 | `ch29_mold.py` | mold-batch: many tasks, no copilot per task (Layer 5) | |

## About the numbering gaps

Chapter numbers track the **course** chapters, and not every chapter has a runnable lab, so the
sequence skips: **6, 15, 18, 19** have no scenario (they're conceptual chapters — the inner loop
formalism, durability, anti-patterns, the horizon). The jump from 17 to 20 is deliberate:
loopkit's Part III labs were renumbered from an earlier 18/19 to **20/21** so they mirror the
course's Part VIII chapters (the course's own Ch 18/19 are anti-patterns / where-this-goes-next —
the old numbering collided). 22–24 are loopkit-specific extensions of the Part III material
(isolation, the skills repo, the reliability metric); **25–27** are the Part IV (molding) labs —
`synth-gate` (fail-first oracle verification), `detect` (deterministic repo introspection), and `route`
(reliability-gated `measure`→`evolve` routing); **28–29** are the batch pair — `loopkit batch` (the
no-infra parallel batch, conflict-aware scheduling) and `loopkit mold-batch` (Layer 5: unattended
batch molding, mold-all → one review → run).

## Adding a scenario

Per the project invariant (CLAUDE.md): **add a scenario for each new concept so `demo`/`learn`
keep pace.** Create `chNN_<slug>.py` exposing a module-level `SCENARIO` (a `Scenario` dataclass)
and a `run(stage)` function, then register it in `_registry()`. Keep it `MockAgent`-driven (zero
tokens) unless the concept genuinely needs `--live`.
