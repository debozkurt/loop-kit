"""Controller tests: every terminal is reachable and reached for the right reason.

Driven by MockAgent + CallableGate, so the whole tick lifecycle runs deterministically with
no coding-agent binary and no tokens spent — which is exactly how the course says to test a
loop (Ch 9: trajectory, convergence, gaming/overfitting the gate).
"""
from __future__ import annotations

from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import AgentConfig, Config, GateConfig, StopsConfig
from loopkit.gate import CallableGate
from loopkit.loop import run_loop
from loopkit.stops import StopReason


def _config(repo: Path, **overrides) -> Config:
    base = dict(goal="make it pass", repo=str(repo), branch="loopkit/test",
                gate=GateConfig(iteration="true"))
    base.update(overrides)
    return Config(**base)


def _writes(name: str, content: str = "ok"):
    def behavior(workspace: Path) -> str:
        (workspace / name).write_text(content)
        return f"wrote {name}"
    return behavior


def test_done_when_acceptance_passes(git_repo: Path):
    agent = MockAgent(behaviors=[_writes("solution.txt")])
    done_gate = CallableGate(lambda ws: (ws / "solution.txt").exists())
    result = run_loop(_config(git_repo), agent, iteration_gate=done_gate, acceptance_gate=done_gate)
    assert result.reason is StopReason.DONE
    assert result.iterations == 1
    assert result.overfit is False


def test_iteration_cap_when_never_satisfied(git_repo: Path):
    behaviors = [_writes(f"f{i}.txt", str(i)) for i in range(10)]
    cfg = _config(git_repo, stops=StopsConfig(max_iter=5, no_progress_after=99))
    result = run_loop(cfg, MockAgent(behaviors=behaviors), iteration_gate=CallableGate(lambda ws: False))
    assert result.reason is StopReason.ITERATION_CAP
    assert result.iterations == 5


def test_no_progress_when_idle(git_repo: Path):
    cfg = _config(git_repo, stops=StopsConfig(max_iter=20, no_progress_after=3))
    # A no-op agent never changes state -> the no-progress sensor fires.
    result = run_loop(cfg, MockAgent(behaviors=[]), iteration_gate=CallableGate(lambda ws: False))
    assert result.reason is StopReason.NO_PROGRESS


def test_budget_ceiling(git_repo: Path):
    behaviors = [_writes(f"f{i}.txt", str(i)) for i in range(10)]
    cfg = _config(git_repo, agent=AgentConfig(max_cost_usd=1.0),
                  stops=StopsConfig(max_iter=20, no_progress_after=99))
    agent = MockAgent(behaviors=behaviors, cost_per_tick=0.5)   # crosses $1.00 on tick 2
    result = run_loop(cfg, agent, iteration_gate=CallableGate(lambda ws: False))
    assert result.reason is StopReason.BUDGET_CEILING
    assert result.iterations == 2


def test_overfit_acceptance_blocks_done(git_repo: Path):
    # The iteration gate passes immediately, but the held-out acceptance gate never does:
    # not DONE, and overfit is flagged. This is the honest twin of gaming the gate (Ch 9).
    behaviors = [_writes(f"f{i}.txt", str(i)) for i in range(10)]
    cfg = _config(git_repo, stops=StopsConfig(max_iter=4, no_progress_after=99))
    result = run_loop(cfg, MockAgent(behaviors=behaviors),
                      iteration_gate=CallableGate(lambda ws: True),
                      acceptance_gate=CallableGate(lambda ws: False, feedback="held-out failing"))
    assert result.reason is StopReason.ITERATION_CAP
    assert result.overfit is True


def test_safety_halt_on_protected_path(git_repo: Path):
    from loopkit.config import SafetyConfig
    cfg = _config(git_repo, safety=SafetyConfig(protected_paths=["tests/holdout/"],
                                                require_clean_tree=False, allow_branches=["loopkit/*"]))

    def touch_protected(ws: Path) -> str:
        target = ws / "tests" / "holdout"
        target.mkdir(parents=True, exist_ok=True)
        (target / "test_secret.py").write_text("assert True")
        return "touched holdout"

    result = run_loop(cfg, MockAgent(behaviors=[touch_protected]),
                      iteration_gate=CallableGate(lambda ws: False))
    assert result.reason is StopReason.SAFETY
