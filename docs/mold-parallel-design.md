# `mold-batch --jobs N` — parallel molding design

Status: **designed, ready to build** (2026-07-19). Reviewed end-to-end against `extensions/mold.py`,
`extensions/batch.py`, `extensions/fleet.py`, `extensions/synth_gate.py`, and the spacer Wave-A
dogfood run. Spacer is the dogfood, not the model: every decision below is stated for arbitrary
repos/job types, with spacer measurements as one worked example.

## Problem

`mold_batch` is serial (`extensions/mold.py::mold_batch` — one `mold_task` at a time). The dominant
cost is the proposer (a fresh-context headless agent; spacer Wave A measured ~13 min/task), so a
43-task batch is hours of mostly-idle wall-clock. The proposer step is embarrassingly parallel; the
verify step may touch a contended resource and is not.

## Design

**Chassis: thread pool, task-per-worker.** `--jobs N` (default 1 = exact current behavior) drives a
`ThreadPoolExecutor`; each worker runs one `mold_task` end to end. NOT `run_batch`/`run_workers`:
the fleet scheduler's `group` semantics gate task *start* (whole-task serialization would put the
proposers back in a line), its `after`/skip semantics are batch-run dependencies that must NOT gate
molding (a `needs-oracle` dep would silently skip its dependents' molding), and `MoldRow` doesn't
fit the `WorkerOutcome` wire shape (no mold status maps to `done`). A pool + a lock is the whole
requirement.

**Verify serialization: per-`group` locks; ungrouped = unserialized.** The verify step (probe +
fail-first through the gate, in an isolated copy) is wrapped in a `threading.Lock` keyed on the
task's `group`; tasks without a group verify fully in parallel. Rationale:

- `group` already means exactly this — "these tasks contend for a shared resource; mutually
  exclude them" (`extensions/batch.py` module docstring) — and batch-time already trusts the same
  declaration. One field, one meaning, honored by every phase that runs gates. No new manifest
  surface.
- The contended resource is whatever the repo has: a docker test DB with fixed host ports, a
  staging environment, a rate-limited external API, a license server — or nothing. Repos with
  hermetic gates (plain `pytest`, `go test`, lint-only) declare nothing and scale linearly.
- Collisions on a non-reentrant gate are not merely slow, they can **false-bless**: the env-liveness
  probe (Q3) samples once *before* fail-first, so a concurrent caller's teardown landing mid
  fail-first fails the oracle for an environmental reason the broken-oracle patterns won't match —
  exactly the class Q3 exists to kill. Serialization at the verify step closes that window for
  every loopkit-initiated gate run.

**Proposer contract line (no machinery):** the `ShellProposer` contract + the molding skill gain
one rule — *never execute the gate or the oracle during proposal; synth-gate verifies it.*
Universal justification: self-testing is redundant spend in every archetype (verification is
mandatory and immediate) and a collision hazard in the contended one. Deliberately a documented
contract, not an exported lock protocol: no self-testing proposer has been observed; build
coordination machinery on the first real collision, not before.

**Durability: incremental single-writer `state.json`.** Today the state file is written once after
the whole loop (`mold_batch` tail) — a crash/Ctrl-C loses the record of everything finished this
invocation. With the pool, the coordinating thread writes `state.json` as each future completes
(single writer, no locking subtleties). Existing mitigation preserved: a filled, FILL-free `run.sh`
skips the proposer on re-run, so even a lost state file never re-pays the proposer.

**Scheduling determinism.** Pre-detect distinct repos serially before dispatch (the `profiles`
cache is not thread-safe); pre-select the `--limit` N unmolded tasks in manifest order before
dispatch (limit semantics unchanged); render `result.rows`, the result table, and the emitted
manifest in manifest order regardless of completion order. `emit_batch_manifest` runs after all
futures join (unchanged).

**`proposer_timeout` knob.** `ShellProposer` hardcodes `timeout=900.0` and the CLI constructs it
with no override. Proposer cost is repo- and prompt-dependent (spacer measured a ~13-min mean —
two minutes under the cap; parallel contention pushes the tail over it, converting slow-but-good
proposals into false `needs-oracle`). Add `[defaults] proposer_timeout` (seconds); default raised
to 1800.

**Probe-staleness diagnostic.** Q3 grew the proposer contract (a second required file). An
integration written before it stalls at `needs-oracle owed=probe.sh` forever, indistinguishable
from a lazy proposer. When a proposer ran, returned ok, filled `run.sh`, but left `probe.sh`
skeleton-untouched, the needs-oracle note says so explicitly: *"proposer appears to predate
MOLD_PROBE_FILE — update it to also fill the env-liveness probe."* New contract requirements must
fail legibly for old integrations.

## Performance model (docs state the model, never one repo's minutes)

```
wall ≈ max( total-propose ÷ jobs ,  largest-group verify chain )
```

Two regimes: hermetic-gate repos (no groups) live entirely in the first term — linear scaling with
jobs. Contended-gate repos floor at their largest group's serial verify chain; past that, the
ceiling is physics (one shared DB runs one suite at a time), and the lever is **repo-side gate
reentrancy** (unique compose project names, no fixed host ports), after which that repo simply
omits `group`. Lock granularity is a design ceiling; the shared resource is a physics ceiling —
loopkit's job is only to let the manifest express which state the repo is in.

Known tail effect, accepted: a worker blocked on a group lock holds its pool slot. Near the end of
a large group some workers idle. A staged propose/verify pipeline would fix it and is not worth its
machinery — it doesn't move the floor.

## New surface (complete list)

- `--jobs N` (CLI, default 1)
- `[defaults] proposer_timeout` (manifest, default 1800)

Everything else is internal behavior or documentation.

## Deferred with intent (triggers named)

| Item | Build when |
|---|---|
| `mutex` field distinct from `group` | a real repo needs group-for-merge-order ≠ shares-a-gate |
| `MOLD_GATE_LOCK` exported to proposers | first observed proposer-initiated gate collision |
| Staged propose/verify pipeline | tail idling measurably moves wall-clock at target jobs |
| Relocatable molded dirs (root indirection for the absolute oracle/validate paths) | first mold-here-run-there (CI) consumer |
| `[stops]` knobs in the rendered config | first run that hits `max_iter` for pace, not progress |

## Tests

- `--jobs 1` byte-identical to current serial behavior (state, manifest, table).
- N-wide: independent tasks complete under one wall-clock bound; results manifest-ordered.
- Recording fake gate: same-group verify intervals never overlap; ungrouped intervals do.
- `after` inert at mold time: a task whose dep stopped `needs-oracle` still molds.
- Crash mid-batch (kill after k completions): `state.json` records exactly k.
- `--limit` preselection in manifest order under parallel completion.
- `proposer_timeout` honored from `[defaults]`; CLI default unchanged elsewhere.
- Probe-staleness note fires only on the ok-run/run.sh-filled/probe-untouched combination.
- `test_cli_surface`: `--jobs` added — EXPECTED update.
