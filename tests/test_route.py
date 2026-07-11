"""Reliability-gated routing — turn a measured pass^k into single-run vs evolve escalation.

Two layers: the **decision rule** as a pure function (every branch — reliable→single, unreliable→evolve,
never-solved→flagged, the population sizing, the k bar) and the **CLI contract** through `CliRunner`
over the free `--from-report` path (no trials run). The load-bearing properties: the rule reuses
`measure`'s estimators (single source of truth for the math), the emitted command is turnkey, and the
decision carries the measurement's harness signature so it's auditable. No tokens, no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopkit.extensions.route import (DEFAULT_MAX_POPULATION, EVOLVE, SINGLE, RouteDecision,
                                      decide_route, decision_signature, route_from_report, size_population)

TS = "2026-07-11T00:00:00+00:00"


# --- the decision rule --------------------------------------------------------------------
def test_reliable_task_routes_to_a_single_run():
    d = decide_route(trials=10, successes=9, timestamp=TS)          # pass^1 = 0.9 ≥ 0.9
    assert d.strategy == SINGLE and d.escalated is False
    assert d.command == "loopkit run" and d.pass_hat_k == pytest.approx(0.9)
    assert d.population == 1 and d.pass_at_population is None


def test_unreliable_task_escalates_to_evolve_with_a_sized_population():
    d = decide_route(trials=10, successes=3, timestamp=TS)          # pass^1 = 0.3 < 0.9
    assert d.strategy == EVOLVE and d.escalated is True
    assert d.command.startswith("loopkit fleet evolve") and f"-p {d.population}" in d.command
    assert d.population > 1 and d.pass_at_population is not None


def test_a_harder_task_gets_a_bigger_population():
    easy = decide_route(trials=10, successes=6, timestamp=TS)       # base 0.6
    hard = decide_route(trials=10, successes=2, timestamp=TS)       # base 0.2
    assert hard.population > easy.population                        # lower base rate ⇒ more attempts


def test_never_solved_escalates_to_the_cap_and_is_flagged_honestly():
    d = decide_route(trials=8, successes=0, timestamp=TS)
    assert d.strategy == EVOLVE and d.population == DEFAULT_MAX_POPULATION
    # the honest signal: escalation can't manufacture a capability the loop never showed
    assert "NEVER solved" in d.reason and "manufacture" in d.reason


def test_k_raises_the_reliability_bar():
    # 9/10 clears the single-run bar (pass^1 = 0.9) but not "3 independent runs all pass".
    single = decide_route(trials=10, successes=9, timestamp=TS, k=1)
    strict = decide_route(trials=10, successes=9, timestamp=TS, k=3)
    assert single.strategy == SINGLE and strict.strategy == EVOLVE
    assert strict.k == 3 and strict.pass_hat_k < single.pass_hat_k


def test_threshold_moves_the_boundary():
    # 7/10 (pass^1 = 0.7): single under a lax bar, evolve under a strict one.
    assert decide_route(trials=10, successes=7, timestamp=TS, threshold=0.6).strategy == SINGLE
    assert decide_route(trials=10, successes=7, timestamp=TS, threshold=0.8).strategy == EVOLVE


def test_generations_and_keep_flow_into_the_command():
    d = decide_route(trials=10, successes=2, timestamp=TS, generations=3, keep=1)
    assert "-g 3" in d.command and "-k 1" in d.command


# --- population sizing --------------------------------------------------------------------
def test_size_population_finds_the_smallest_n_clearing_the_target():
    n, odds = size_population(0.5, 0.95, 8)
    assert 1.0 - 0.5 ** n >= 0.95 and 1.0 - 0.5 ** (n - 1) < 0.95    # smallest such n
    assert odds == pytest.approx(1.0 - 0.5 ** n)


def test_size_population_returns_the_cap_when_the_target_is_unreachable():
    n, odds = size_population(0.0, 0.95, 8)                          # a zero base rate never clears it
    assert n == 8 and odds == 0.0


def test_size_population_rejects_a_bad_cap():
    with pytest.raises(ValueError):
        size_population(0.5, 0.95, 0)


# --- input validation ---------------------------------------------------------------------
def test_decide_route_rejects_bad_counts():
    with pytest.raises(ValueError):
        decide_route(trials=0, successes=0, timestamp=TS)
    with pytest.raises(ValueError):
        decide_route(trials=5, successes=6, timestamp=TS)           # successes > trials
    with pytest.raises(ValueError):
        decide_route(trials=5, successes=1, timestamp=TS, k=6)      # k > trials


# --- provenance ---------------------------------------------------------------------------
def test_signature_is_stable_and_sensitive():
    a = decision_signature(10, 3, 0.9, 1, 0.95, 8)
    assert a == decision_signature(10, 3, 0.9, 1, 0.95, 8)
    assert a != decision_signature(10, 4, 0.9, 1, 0.95, 8)          # different measurement
    assert a != decision_signature(10, 3, 0.8, 1, 0.95, 8)          # different threshold


def test_decision_json_is_self_describing():
    d = decide_route(trials=10, successes=3, timestamp=TS, measured_on="harness123")
    data = json.loads(d.to_json())
    assert data["strategy"] == EVOLVE and data["timestamp"] == TS
    assert data["measured_on"] == "harness123" and data["signature"] and data["loopkit_version"]


# --- route_from_report (the free path) ----------------------------------------------------
def test_route_from_report_pulls_counts_and_harness_signature():
    report = {"goal": "fix X", "trials": 10, "successes": 8, "harness": {"signature": "sig-xyz"}}
    d = route_from_report(report, timestamp=TS)
    assert d.trials == 10 and d.successes == 8 and d.goal == "fix X"
    assert d.measured_on == "sig-xyz"                               # decision tied to the measurement


def test_route_from_report_rejects_a_non_report():
    with pytest.raises(ValueError):
        route_from_report({"not": "a report"}, timestamp=TS)


def test_route_from_report_respects_threshold_override():
    report = {"trials": 10, "successes": 7}
    assert route_from_report(report, timestamp=TS, threshold=0.6).strategy == SINGLE
    assert route_from_report(report, timestamp=TS, threshold=0.8).strategy == EVOLVE


# --- the CLI contract (free --from-report path; no trials run) -----------------------------
def _run_cli(args: list[str], monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from loopkit.cli import app
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir(exist_ok=True)
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))
    return CliRunner().invoke(app, ["route", *args])


def _write_report(tmp_path: Path, *, trials: int, successes: int) -> Path:
    p = tmp_path / "report.json"
    p.write_text(json.dumps({"goal": "demo goal", "trials": trials, "successes": successes,
                             "harness": {"signature": "abc"}}))
    return p


def test_cli_routes_a_reliable_report_to_single(tmp_path: Path, monkeypatch):
    report = _write_report(tmp_path, trials=10, successes=10)
    result = _run_cli(["--from-report", str(report)], monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "single" in result.output and "loopkit run" in result.output


def test_cli_routes_an_unreliable_report_to_evolve_and_writes_the_decision(tmp_path: Path, monkeypatch):
    report = _write_report(tmp_path, trials=10, successes=2)
    out = tmp_path / "decision.json"
    result = _run_cli(["--from-report", str(report), "--out", str(out), "--threshold", "0.9"],
                      monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "evolve" in result.output and "loopkit fleet evolve" in result.output
    data = json.loads(out.read_text())
    assert data["strategy"] == EVOLVE and data["measured_on"] == "abc"


def test_cli_errors_on_an_unreadable_report(tmp_path: Path, monkeypatch):
    result = _run_cli(["--from-report", str(tmp_path / "nope.json")], monkeypatch, tmp_path)
    assert result.exit_code == 1 and "could not read" in result.output


def test_cli_inline_route_needs_a_held_out_gate(tmp_path: Path, monkeypatch):
    # Without --from-report it calibrates via measure, which requires a held-out acceptance gate.
    cfg = tmp_path / "loopkit.toml"
    cfg.write_text('goal = "x"\nrepo = "."\n[gate]\niteration = "true"\n')
    result = _run_cli(["-c", str(cfg)], monkeypatch, tmp_path)
    assert result.exit_code == 1 and "held-out" in result.output


def test_dataclass_shape_is_stable():
    d = RouteDecision(strategy=SINGLE, escalated=False, reason="r", command="loopkit run", trials=1,
                      successes=1, k=1, pass_hat_k=1.0, pass_at_population=None, threshold=0.9,
                      population=1, generations=2, keep=2, goal="g", signature="s", measured_on=None,
                      loopkit_version="0.1.0", timestamp=TS)
    assert d.to_dict()["command"] == "loopkit run"
