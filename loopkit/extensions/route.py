"""Reliability-gated routing — turn a measured `pass^k` into a run strategy (Part IV, Layer 3→4).

`measure` (Part III) answers *how reliably* the loop solves a goal: `pass^k`, the chance that **all**
of k independent attempts succeed — the production question, which **falls** with k. `evolve`
(Ch 10-11) is the lever for the other side: best-of-N + held-out re-validation, which raises the
chance of *at least one* success (`pass@k`, discovery). The molding decision that connects them is
mechanical: **if a task is reliable enough single-shot, run it once; if not, escalate it to evolve** —
and size the population from how far below the bar it fell. That decision is a *rule*, not a judgment,
which is exactly why it earns code (a judgment-y "is this task hard?" call stays in the `loopkit-mold`
skill; this is the deterministic feedback loop the skill routes through).

`decide_route` is that rule, as a pure function over a measurement:

  - **reliability = `pass^k`** at the chosen k. `>= threshold` ⇒ **single** run (the loop is dependable
    enough that best-of-N would just spend tokens for a result you'd already get).
  - **below threshold** ⇒ **evolve**, with the population sized so the *discovery* odds
    (`1 − (1 − p)^N`, p = the single-shot rate) clear a target — the smallest N that makes "at least
    one of N succeeds" likely, capped so a hard task can't request an unbounded fan-out.
  - **`pass^1 == 0`** (never solved single-shot) ⇒ evolve at the cap, but flagged honestly: escalation
    can't manufacture a capability that isn't there. A zero base rate usually means the goal, gates, or
    oracle are wrong, or the model is under-powered — a routing decision says so rather than pretending
    a bigger fan-out will find a solution the loop has never once produced.

It is **advisory** — it emits the strategy plus the exact command to run, never launching an (expensive)
evolve itself. That matches the kit's line: the primitives *propose*, the molder (or a human) decides,
and the standing guardrails still bound anything that does run. The `RouteDecision` carries the
measurement's harness signature + a decision signature + version + timestamp, so a routing choice is an
auditable record tied to the exact numbers it came from — not a number floating free of its harness.

Stdlib-only; reuses `measure`'s unbiased estimators (`pass_hat_k`/`pass_at_k`) so the math has a single
source of truth. No core/executor/fleet coupling — the `detect.py`/`synth_gate.py`/`measure.py` shape.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Callable

from ..log import get_logger
from .measure import pass_at_k, pass_hat_k

# Strategy names — a stable string callers/tests key off rather than a bool.
SINGLE = "single"        # the loop is reliable enough single-shot: `loopkit run`
EVOLVE = "evolve"        # escalate to best-of-N + held-out re-validation: `loopkit fleet evolve`

# Defaults chosen to line up with `fleet evolve`'s own defaults (generations=2, keep=2) so the emitted
# command is turnkey; the population is *sized*, not defaulted. `target_discovery` is the "at least one
# of N succeeds" bar the population sizing aims to clear.
DEFAULT_THRESHOLD = 0.9          # pass^k at/above this ⇒ single run is dependable enough
DEFAULT_TARGET_DISCOVERY = 0.95  # size the evolve population so pass@N clears this
DEFAULT_MAX_POPULATION = 8       # cap the fan-out — a hard task can't request unbounded attempts
DEFAULT_GENERATIONS = 2          # `fleet evolve` default
DEFAULT_KEEP = 2                 # `fleet evolve` default


@dataclass
class RouteDecision:
    """The routing choice for one goal, derived from its reliability measurement — the provenance record.

    `strategy` is the bottom line (`single` | `evolve`); `command` is the exact thing to run.
    `escalated` is the boolean form (`strategy == EVOLVE`). The `population`/`generations`/`keep` fields
    are only meaningful when escalating (they mirror `fleet evolve`'s knobs). Everything else pins the
    decision to the numbers + harness it came from, so it is auditable and re-comparable.
    """

    strategy: str
    escalated: bool
    reason: str
    command: str                         # the exact command to run (turnkey)
    # the measurement this decision rests on
    trials: int
    successes: int
    k: int
    pass_hat_k: float                    # measured reliability at k (the number the rule tests)
    pass_at_population: float | None     # discovery odds at the sized population (None when single)
    threshold: float
    # evolve sizing (meaningful iff escalated)
    population: int
    generations: int
    keep: int
    # provenance
    goal: str
    signature: str                       # short hash of the decision inputs (rule identity)
    measured_on: str | None              # the underlying measurement's harness signature, if known
    loopkit_version: str
    timestamp: str                       # ISO-8601; supplied by the caller (no hidden clock)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def decision_signature(trials: int, successes: int, threshold: float, k: int,
                       target_discovery: float, max_population: int) -> str:
    """A short, stable hash of the decision inputs — so a stored decision names what it decided over.

    Change the measurement (trials/successes), the bar (threshold/target), or the cap and the signature
    changes; a decision can't be silently reused for different numbers. Mirrors `measure`/`synth_gate`.
    """
    blob = json.dumps({"trials": trials, "successes": successes, "threshold": threshold, "k": k,
                       "target_discovery": target_discovery, "max_population": max_population},
                      sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def size_population(base_rate: float, target_discovery: float, max_population: int) -> tuple[int, float]:
    """Smallest N in 1..cap whose discovery odds `1 − (1 − p)^N` clear `target_discovery` → (N, odds@N).

    Independent-trial discovery is the honest sizing estimate for "run it N ways, keep the best": even
    though `evolve` re-validates and reseeds (so the real odds are a touch different), `1 − (1 − p)^N`
    is the right back-of-envelope for how many attempts it takes to *find* one success at base rate p.
    A zero base rate can never clear the bar, so it returns the cap (the caller flags that honestly).
    """
    if max_population < 1:
        raise ValueError(f"max_population must be >= 1 (got {max_population})")
    best_odds = 0.0
    for n in range(1, max_population + 1):
        odds = 1.0 - (1.0 - base_rate) ** n
        if odds >= target_discovery:
            return n, odds
        best_odds = odds
    return max_population, best_odds        # couldn't clear the bar within the cap → escalate to it


def decide_route(*, trials: int, successes: int, timestamp: str, threshold: float = DEFAULT_THRESHOLD,
                 k: int | None = None, target_discovery: float = DEFAULT_TARGET_DISCOVERY,
                 max_population: int = DEFAULT_MAX_POPULATION, generations: int = DEFAULT_GENERATIONS,
                 keep: int = DEFAULT_KEEP, goal: str = "", measured_on: str | None = None,
                 base_command: str = "loopkit run", loopkit_version: str | None = None) -> RouteDecision:
    """Decide single-run vs evolve from a reliability measurement (`trials` runs, `successes` DONEs).

    Pure and deterministic: `pass^k >= threshold` ⇒ a single run; else escalate to evolve with the
    population sized to clear `target_discovery`. `k` defaults to **1** — the base single-run success
    rate `c/n`, the graded metric a routing decision most naturally tests ("how often does *one* run
    succeed?"); raise `k` to demand multi-attempt reliability ("reliably pass k independent runs", the
    tau-bench production bar — but note `pass^k` at `k == trials` is degenerate: 1.0 only if *every*
    trial passed). `timestamp` is passed in, not read from a hidden clock, so the decision is
    reproducible. Never runs anything — it returns the strategy + the exact command for the molder to run.
    """
    log = get_logger("route")
    if trials < 1:
        raise ValueError(f"trials must be >= 1 (got {trials})")
    if not 0 <= successes <= trials:
        raise ValueError(f"successes must be in 0..{trials} (got {successes})")
    kk = k if k is not None else 1        # base rate c/n by default (graded); --k raises the bar
    if not 1 <= kk <= trials:
        raise ValueError(f"k must be in 1..{trials} (got {kk})")

    reliability = pass_hat_k(trials, successes, kk)
    base_rate = successes / trials
    sig = decision_signature(trials, successes, threshold, kk, target_discovery, max_population)
    from .. import __version__ as _v
    version = loopkit_version or _v

    if reliability >= threshold:
        reason = (f"pass^{kk} = {reliability:.2f} ≥ threshold {threshold:.2f} — the loop is reliable "
                  f"enough single-shot; best-of-N would spend tokens for a result you'd already get.")
        decision = RouteDecision(
            strategy=SINGLE, escalated=False, reason=reason, command=base_command,
            trials=trials, successes=successes, k=kk, pass_hat_k=reliability, pass_at_population=None,
            threshold=threshold, population=1, generations=generations, keep=keep, goal=goal,
            signature=sig, measured_on=measured_on, loopkit_version=version, timestamp=timestamp)
    else:
        population, odds = size_population(base_rate, target_discovery, max_population)
        command = f"loopkit fleet evolve -g {generations} -p {population} -k {keep}"
        if successes == 0:
            reason = (f"pass^1 = 0 over {trials} trials — the loop has NEVER solved this single-shot, so "
                      f"escalation can't manufacture the capability. Escalating to evolve at the cap "
                      f"(p={population}), but first revisit the goal/gates/held-out oracle or the model: "
                      f"a bigger fan-out won't find a solution the loop has never once produced.")
        else:
            reason = (f"pass^{kk} = {reliability:.2f} < threshold {threshold:.2f} — unreliable single-shot. "
                      f"Escalate to evolve: at base rate {base_rate:.2f}, p={population} attempts clear "
                      f"~{odds:.0%} discovery (pass@{population}); the held-out re-validation keeps the "
                      f"selection honest (Ch 9).")
        decision = RouteDecision(
            strategy=EVOLVE, escalated=True, reason=reason, command=command,
            trials=trials, successes=successes, k=kk, pass_hat_k=reliability, pass_at_population=odds,
            threshold=threshold, population=population, generations=generations, keep=keep, goal=goal,
            signature=sig, measured_on=measured_on, loopkit_version=version, timestamp=timestamp)

    log.info("route.decide", strategy=decision.strategy, passHatK=round(reliability, 4), k=kk,
             threshold=threshold, population=decision.population, sig=sig)
    return decision


# The runner contract mirrors `measure`'s — a `Callable[[dict], WorkerOutcome]`; kept here so a caller
# that wants "calibrate then route" in one step has the type, without route importing the fleet.
TrialRunner = Callable[[dict], object]


def route_from_report(report: dict, *, timestamp: str, threshold: float = DEFAULT_THRESHOLD,
                      k: int | None = None, **kwargs) -> RouteDecision:
    """Decide over a saved `ReliabilityReport` JSON dict (the free, no-run path).

    Pulls the trials/successes/goal + the harness signature out of a `measure --out` report and runs
    the rule — so once you've paid to measure a representative task, routing it (and re-routing under a
    different threshold) costs nothing. Duck-types the dict rather than reconstructing the dataclass, so
    it tolerates a report written by any recent measure version.
    """
    try:
        trials = int(report["trials"])
        successes = int(report["successes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a reliability report (missing/!int trials|successes): {exc}") from exc
    harness = report.get("harness") or {}
    measured_on = harness.get("signature") if isinstance(harness, dict) else None
    return decide_route(trials=trials, successes=successes, timestamp=timestamp, threshold=threshold,
                        k=k, goal=str(report.get("goal", "")), measured_on=measured_on, **kwargs)
