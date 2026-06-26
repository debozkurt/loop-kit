"""The reliability measurement layer — pass^k / pass@k over N trials of one goal.

Two layers of test: the **estimators** (pure combinatorics, exact known values + the monotonicity
that is the whole point — pass^k falls, pass@k rises) and the **harness** (`measure_reliability`
counts DONEs, tolerates a crashing trial, and emits a self-describing report). The integration test
drives the *real* `make_repo_runner` against the bundled demo-repo with a flaky scripted agent, so
the seam the CLI uses is proven end-to-end — no tokens, no network.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from loopkit.extensions.measure import (ReliabilityReport, harness_signature, measure_reliability,
                                        pass_at_k, pass_hat_k)
from loopkit.stops import StopReason

TS = "2026-06-22T00:00:00+00:00"          # a fixed timestamp (the report takes the clock as input)


# --- the estimators ------------------------------------------------------------------------
def test_known_values_n10_c6():
    # n=10 trials, c=6 successes. pass^1 == pass@1 == c/n.
    assert pass_hat_k(10, 6, 1) == pytest.approx(0.6)
    assert pass_at_k(10, 6, 1) == pytest.approx(0.6)
    # pass^2 = C(6,2)/C(10,2) = 15/45; pass@2 = 1 - C(4,2)/C(10,2) = 1 - 6/45.
    assert pass_hat_k(10, 6, 2) == pytest.approx(15 / 45)
    assert pass_at_k(10, 6, 2) == pytest.approx(1 - 6 / 45)


def test_reliability_falls_and_discovery_rises():
    # The headline property: pass^k is non-increasing in k, pass@k non-decreasing.
    n, c = 8, 5
    phk = [pass_hat_k(n, c, k) for k in range(1, n + 1)]
    pak = [pass_at_k(n, c, k) for k in range(1, n + 1)]
    assert phk == sorted(phk, reverse=True) and phk[0] > phk[-1]      # falls
    assert pak == sorted(pak) and pak[-1] > pak[0]                    # rises


def test_edge_cases_all_pass_all_fail_and_too_few_successes():
    assert pass_hat_k(5, 5, 5) == pytest.approx(1.0)                  # every trial passed
    assert pass_at_k(5, 5, 3) == pytest.approx(1.0)
    assert pass_hat_k(5, 0, 1) == pytest.approx(0.0)                  # none passed
    assert pass_at_k(5, 0, 3) == pytest.approx(0.0)
    assert pass_hat_k(5, 2, 3) == pytest.approx(0.0)                  # fewer successes than k


def test_k_out_of_range_raises():
    for bad in (0, 6):
        with pytest.raises(ValueError):
            pass_hat_k(5, 3, bad)
        with pytest.raises(ValueError):
            pass_at_k(5, 3, bad)


def test_harness_signature_is_stable_and_sensitive():
    a = {"adapter": "claude-api", "gate_acceptance": "pytest tests/holdout"}
    assert harness_signature(a) == harness_signature(dict(a))         # order/identity independent
    assert harness_signature(a) != harness_signature({**a, "gate_acceptance": "pytest other"})


# --- the harness over a fake runner (no real loop) -----------------------------------------
def _runner(passing_indices: set[int]):
    """A fake TaskRunner: DONE on the given trial indices, ITERATION_CAP otherwise. Records ids."""
    seen: list[str] = []

    def run(task: dict):
        seen.append(task["id"])
        i = int(task["id"].rsplit("-t", 1)[1])
        reason = StopReason.DONE.value if i in passing_indices else StopReason.ITERATION_CAP.value
        return SimpleNamespace(reason=reason, iterations=3, cost_usd=0.01)

    run.seen = seen      # type: ignore[attr-defined]
    return run


def test_measure_counts_dones_and_builds_curves():
    runner = _runner({0, 1, 2, 5, 6, 9})                             # 6 of 10
    report = measure_reliability(runner, {"id": "g", "goal": "fix it"}, trials=10, timestamp=TS,
                                 adapter="claude-api", model="claude-opus-4-8", target="repo")
    assert report.successes == 6 and report.trials == 10
    assert report.success_rate == pytest.approx(0.6)
    assert report.pass_hat_k[1] == pytest.approx(0.6)
    assert report.pass_hat_k[10] == pytest.approx(0.0)               # not all 10 passed
    assert report.pass_at_k[10] == pytest.approx(1.0)               # at least one in any draw of 10
    # Each trial got a distinct id so make_repo_runner would isolate it on its own branch.
    assert len(set(runner.seen)) == 10 and all(s.startswith("g-t") for s in runner.seen)


def test_a_crashing_trial_is_a_failure_not_an_abort():
    def runner(task: dict):
        if task["id"].endswith("-t1"):
            raise RuntimeError("boom")
        return SimpleNamespace(reason=StopReason.DONE.value, iterations=1, cost_usd=0.0)

    report = measure_reliability(runner, {"id": "g", "goal": "x"}, trials=3, timestamp=TS)
    assert report.successes == 2                                     # the crash counts as a fail
    crashed = next(o for o in report.outcomes if o.index == 1)
    assert crashed.passed is False and crashed.reason == "error" and crashed.error == "RuntimeError"


def test_report_json_roundtrip_is_self_describing():
    import json
    report = measure_reliability(_runner({0}), {"id": "g", "goal": "x"}, trials=2, timestamp=TS,
                                 harness_params={"adapter": "mock"})
    data = json.loads(report.to_json())
    assert data["pass_hat_k"]["1"] == pytest.approx(0.5)            # string keys in JSON
    assert data["success_rate"] == pytest.approx(0.5)
    assert data["harness"]["loopkit_version"] and data["harness"]["signature"]
    assert data["timestamp"] == TS


def test_trials_must_be_positive():
    with pytest.raises(ValueError):
        measure_reliability(_runner(set()), {"id": "g"}, trials=0, timestamp=TS)


# --- cost per accepted change (the economically honest unit cost) ---------------------------
def test_cost_per_accepted_divides_spend_by_accepted_not_attempted():
    # 10 trials @ $0.01 each = $0.10 spent; only 6 reached DONE. You paid for every attempt, but the
    # honest unit cost is spend / *accepted* — $0.10/6, not $0.10/10.
    report = measure_reliability(_runner({0, 1, 2, 5, 6, 9}), {"id": "g", "goal": "x"},
                                 trials=10, timestamp=TS)
    assert report.total_cost_usd == pytest.approx(0.10)
    assert report.cost_per_accepted == pytest.approx(0.10 / 6)


def test_cost_per_accepted_is_none_when_nothing_accepted():
    # Undefined, not zero: spend with no accepted change is pure waste, and 0 would hide that.
    report = measure_reliability(_runner(set()), {"id": "g", "goal": "x"}, trials=3, timestamp=TS)
    assert report.successes == 0 and report.cost_per_accepted is None


def test_cost_per_accepted_serializes():
    import json
    report = measure_reliability(_runner({0}), {"id": "g", "goal": "x"}, trials=2, timestamp=TS)
    data = json.loads(report.to_json())
    assert data["cost_per_accepted"] == pytest.approx(0.02)          # $0.02 spent / 1 accepted


# --- end to end over the real make_repo_runner (token-free flaky agent) ---------------------
_WRONG = '''\
"""Line-item pricing (no discount — fails the seen gate)."""


def line_total(unit_price, quantity):
    return round(unit_price * quantity, 2)
'''


def test_measure_over_make_repo_runner_with_a_flaky_agent(tmp_path: Path):
    # The seam the CLI uses: each trial is a full isolated run_loop on the demo-repo, graded by the
    # real held-out gate. A scripted agent solves on a fixed subset of trials (keyed off the trial id
    # measure assigns), so the pass/fail mix is deterministic and the DONE count is exact — no tokens.
    from loopkit.agent import MockAgent
    from loopkit.extensions.fleet import make_repo_runner
    from loopkit.scenarios import CORRECT_PRICING, demo_src

    solves = {0, 2}                                                  # 2 of 3 trials reach DONE

    def factory(task: dict):
        i = int(task["id"].rsplit("-t", 1)[1])
        body = CORRECT_PRICING if i in solves else _WRONG
        return MockAgent(behaviors=[lambda ws, _b=body: (ws / "pricing.py").write_text(_b) and "wrote"
                                    or "wrote"])

    import sys
    py = sys.executable
    runner = make_repo_runner(
        str(demo_src()), mode="copy", max_iter=4,
        gate_iteration=f"{py} -m pytest tests/seen -q",
        gate_acceptance=f"{py} -m pytest tests/holdout -q",
        protected_paths=("tests/",), agent_factory=factory)
    report = measure_reliability(runner, {"id": "pricing", "goal": "fix the bulk discount"},
                                 trials=3, timestamp=TS, target=str(demo_src()))
    assert report.successes == 2
    assert report.pass_hat_k[1] == pytest.approx(2 / 3)
    assert report.pass_hat_k[3] == pytest.approx(0.0)               # not all three solved
    assert isinstance(report, ReliabilityReport)


# --- the measure CLI resolves a relative repo (regression) ----------------------------------
def test_measure_cli_resolves_a_relative_repo(git_repo: Path, tmp_path: Path, monkeypatch):
    """Regression: `measure` with the default relative `repo = "."` must resolve to an absolute path
    before handing it to the runner. Each trial clones into its own temp scratch (a different cwd), so
    a relative `.` would `git clone .` from the empty scratch dir, fail every trial, and the harness
    would silently report pass^k = 0 for a perfectly solvable goal. `run`/`doctor` resolve via
    `repo_path()`; measure must match.
    """
    import json
    import subprocess

    from typer.testing import CliRunner

    from loopkit.cli import app

    # Both oracles `true` ⇒ a no-op MockAgent reaches DONE every trial, so a correct (resolved) clone
    # gives pass^k = 1. The pre-fix bug made every trial reason="error" instead.
    (git_repo / "loopkit.toml").write_text(
        'goal = "reliability over a relative repo"\nrepo = "."\nbranch = "loopkit/run"\n'
        '[agent]\nadapter = "mock"\n'
        '[gate]\niteration = "true"\nacceptance = "true"\n'
        '[safety]\nprotected_paths = []\n')
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "cfg"], cwd=git_repo, check=True, capture_output=True)

    nocreds = tmp_path / "nocreds"
    nocreds.mkdir()
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))           # don't scrub the dev env
    for var in ("LANGSMITH_API_KEY", "LANGSMITH_TRACING", "LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(git_repo)                                    # so repo_path() resolves "." here

    out = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["measure", "-c", "loopkit.toml", "-n", "3", "--out", str(out)])
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert report["successes"] == 3                                # all three clones resolved + solved
    assert report["pass_hat_k"]["1"] == 1.0
    assert all(o["reason"] != "error" for o in report["outcomes"])  # no swallowed clone failure
