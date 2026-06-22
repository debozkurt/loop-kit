"""The reliability measurement layer — `pass^k` over N independent trials of one goal (Part III).

`evolve` (Ch 10-11) is best-of-N: run a goal many ways and *keep the winner*. That answers
"**can** the loop solve this?" (discovery — `pass@k`, which rises with k). The production-relevant
question is the opposite: "**how often** does the loop solve this when I'm not cherry-picking?"
(reliability — `pass^k`, the chance that *all* of k independent trials succeed, which **falls** with
k). tau-bench's headline: a model >60% at `pass^1` can be <25% at `pass^8` — the gap between *can*
and *reliably does* is where agents actually fail in production, and the field under-tools it. This
module is loopkit's measurement of that gap.

The seam it builds on: a `TaskRunner` (the same `Callable[[dict], WorkerOutcome]` the fleet uses)
runs **one isolated trial** of a goal — fresh clone, full `run_loop`, graded by the **held-out
acceptance gate**. So a trial *passes* iff it reached `DONE` (the held-out oracle certified it, not
the agent's own say-so). `measure_reliability` runs the same task N times through that runner, counts
the DONEs, and reports the `pass^k`/`pass@k` curves. Runner-agnostic by design: production passes a
`make_repo_runner`; tests/demos pass a trivial fake — no tokens, no network.

**A number without its harness isn't a measurement.** SWE-bench Verified was retired in 2026 over a
10-20pt swing across scaffolds. So every `ReliabilityReport` carries the **loopkit version**, a
**harness signature** (a hash of the load-bearing setup — gates, adapter, model, iteration cap), and
a **timestamp**: a score is only comparable to another score from the same harness, and the report
says which one produced it. The report is JSON-serializable so it can be stored and re-compared
offline.

Stdlib-only (`math.comb`, `hashlib`, `json`, `dataclasses`) — importing this pulls no extra. The core
keeps no runtime dependency on it; it duck-types the runner's outcome (`.reason`/`.iterations`/
`.cost_usd`), so it never imports the fleet.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from math import comb
from typing import Callable

from ..log import get_logger
from ..stops import StopReason

# A trial passes iff its run reached this terminal — i.e. the held-out acceptance gate certified it.
_PASS = StopReason.DONE.value

# The runner contract is the fleet's `TaskRunner` (Callable[[dict], WorkerOutcome]); we duck-type the
# outcome so this module never imports the fleet. A runner that raises is caught and scored as a fail.
TrialRunner = Callable[[dict], object]


# --------------------------------------------------------------------------------------------
# The estimators — unbiased combinatorial estimators over n trials with c successes.
# --------------------------------------------------------------------------------------------
def pass_at_k(n: int, c: int, k: int) -> float:
    """`pass@k` (discovery): the probability that **at least one** of k trials drawn from the n
    passes. The Codex/HumanEval unbiased estimator `1 - C(n-c, k) / C(n, k)`. Rises with k.

    This is the metric `evolve` implicitly optimizes (keep the best of N) — useful, but it measures
    *can the loop ever do it*, not *will it*.
    """
    if not 1 <= k <= n:
        raise ValueError(f"k must be in 1..n (got k={k}, n={n})")
    if n - c < k:                       # too few failures to fill k slots → some draw must include a pass
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """`pass^k` (reliability): the probability that **all** k trials drawn from the n pass — the
    unbiased estimator `C(c, k) / C(n, k)` (tau-bench). Falls with k.

    This is the production-relevant metric: at `pass^1` it is just the base success rate `c/n`; as k
    grows it is how likely the loop is to succeed on *every* one of k consecutive independent attempts.
    """
    if not 1 <= k <= n:
        raise ValueError(f"k must be in 1..n (got k={k}, n={n})")
    if c < k:                           # fewer successes than k → no all-pass draw exists
        return 0.0
    return comb(c, k) / comb(n, k)


# --------------------------------------------------------------------------------------------
# The report — self-describing (carries its own harness identity), JSON-serializable.
# --------------------------------------------------------------------------------------------
@dataclass
class TrialOutcome:
    """One trial's flat result. `passed` is the only thing the metric needs; the rest is provenance."""

    index: int
    passed: bool
    reason: str                          # the trial's StopReason value, or "error" if the runner raised
    iterations: int = 0
    cost_usd: float = 0.0
    error: str | None = None


@dataclass
class HarnessInfo:
    """What produced the number — so a score is only ever compared against the same harness."""

    loopkit_version: str
    signature: str                       # short hash of the load-bearing params below
    params: dict                         # gates, adapter, model, max_iter, … (human-readable)


@dataclass
class ReliabilityReport:
    """The result of measuring one goal's reliability over N trials — the measurement artifact."""

    goal: str
    target: str
    adapter: str
    model: str
    trials: int
    successes: int
    pass_hat_k: dict[int, float]         # k -> pass^k  (reliability; falls with k)
    pass_at_k: dict[int, float]          # k -> pass@k  (discovery; rises with k)
    harness: HarnessInfo
    timestamp: str                       # ISO-8601; supplied by the caller (no hidden clock)
    total_cost_usd: float = 0.0
    outcomes: list[TrialOutcome] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """`pass^1` == `pass@1` == c/n — the base single-shot success rate."""
        return self.successes / self.trials if self.trials else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # JSON object keys must be strings; the curves are keyed by int k.
        d["pass_hat_k"] = {str(k): v for k, v in self.pass_hat_k.items()}
        d["pass_at_k"] = {str(k): v for k, v in self.pass_at_k.items()}
        d["success_rate"] = self.success_rate
        return d

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def harness_signature(params: dict) -> str:
    """A short, stable hash of the load-bearing measurement setup (gates/adapter/model/iter cap).

    Two reports are comparable only when their signatures match: this is what makes "pass^4 = 0.3" a
    *measurement* rather than a floating number — it pins the harness the number was taken on. The
    sample size (trial count) is deliberately **not** in the signature: it changes the variance of the
    estimate, not the harness identity.
    """
    blob = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def measure_reliability(runner: TrialRunner, task: dict, *, trials: int, timestamp: str,
                        k_max: int | None = None, adapter: str = "", model: str = "",
                        target: str = "", harness_params: dict | None = None,
                        loopkit_version: str | None = None) -> ReliabilityReport:
    """Run `task` `trials` times through `runner`, count the DONEs, and build a `ReliabilityReport`.

    Each call gets a distinct task id (`<id>-t<i>`) so a `make_repo_runner` puts each trial on its own
    branch in its own clone — trials are independent. A runner that raises scores as a failed trial
    (a measurement of reliability must itself not fall over on one bad trial). `timestamp` is passed in
    rather than read from a hidden clock, so the report is reproducible and the caller owns the format.
    """
    log = get_logger("measure")
    if trials < 1:
        raise ValueError(f"trials must be >= 1 (got {trials})")
    base_id = str(task.get("id", "measure"))
    outcomes: list[TrialOutcome] = []

    for i in range(trials):
        trial_task = {**task, "id": f"{base_id}-t{i}"}
        try:
            result = runner(trial_task)
            reason = str(getattr(result, "reason", "error"))
            outcomes.append(TrialOutcome(
                index=i, passed=(reason == _PASS), reason=reason,
                iterations=int(getattr(result, "iterations", 0) or 0),
                cost_usd=float(getattr(result, "cost_usd", 0.0) or 0.0)))
        except Exception as exc:   # noqa: BLE001 — one trial's crash is a failed trial, not a failed run
            log.warn("trial.error", index=i, err=type(exc).__name__)
            outcomes.append(TrialOutcome(index=i, passed=False, reason="error",
                                         error=type(exc).__name__))
        log.info("trial.done", index=i, passed=outcomes[-1].passed, reason=outcomes[-1].reason)

    n = trials
    c = sum(1 for o in outcomes if o.passed)
    top = min(k_max or n, n)
    phk = {k: pass_hat_k(n, c, k) for k in range(1, top + 1)}
    pak = {k: pass_at_k(n, c, k) for k in range(1, top + 1)}

    from .. import __version__ as _v
    version = loopkit_version or _v
    params = dict(harness_params or {})
    params.setdefault("loopkit_version", version)
    harness = HarnessInfo(loopkit_version=version, signature=harness_signature(params), params=params)

    log.info("measure.done", trials=n, successes=c, passHat1=round(phk.get(1, 0.0), 4),
             passHatK=round(phk.get(top, 0.0), 4), sig=harness.signature)
    return ReliabilityReport(
        goal=str(task.get("goal", "")), target=target, adapter=adapter, model=model,
        trials=n, successes=c, pass_hat_k=phk, pass_at_k=pak, harness=harness,
        timestamp=timestamp, total_cost_usd=round(sum(o.cost_usd for o in outcomes), 6),
        outcomes=outcomes)
