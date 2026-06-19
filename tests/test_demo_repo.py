"""End-to-end against the shipped demo-repo, with real pytest gates (no coding-agent binary).

A scripted MockAgent stands in for the model so the whole loop runs deterministically while the
gates are the real `pytest` invocations the demo ships. This proves three things at once: the
demo fixture embodies the Ch 9 lesson (seen passes with the bug, held-out catches it), the
ShellGate wiring works, and the controller reaches DONE only when the held-out gate passes.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, SafetyConfig, StopsConfig
from loopkit.gate import ShellGate
from loopkit.loop import run_loop
from loopkit.stops import StopReason

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"

_CORRECT = '''\
"""Line-item pricing with a bulk discount."""


def line_total(unit_price: float, quantity: int) -> float:
    subtotal = unit_price * quantity
    if quantity >= 10:
        subtotal *= 0.9
    return round(subtotal, 2)
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _instantiate_demo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    shutil.copytree(DEMO, repo)
    _git(repo, "init", "-q")
    _git(repo, "branch", "-m", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "loopkit-test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed demo")
    return repo


def _gates() -> tuple[ShellGate, ShellGate]:
    py = sys.executable
    return (ShellGate(f"{py} -m pytest tests/seen -q"),
            ShellGate(f"{py} -m pytest tests/holdout -q"))


def _config(repo: Path, **stops) -> Config:
    return Config(goal="fix pricing boundary", repo=str(repo), branch="loopkit/run",
                  gate=GateConfig(iteration="seen", acceptance="holdout"),
                  stops=StopsConfig(**stops),
                  safety=SafetyConfig(protected_paths=["tests/"], require_clean_tree=False))


def test_held_out_gate_blocks_overfit(tmp_path: Path):
    """With the seeded bug untouched: seen passes, held-out fails -> never DONE, overfit flagged."""
    repo = _instantiate_demo(tmp_path)
    iteration, acceptance = _gates()
    cfg = _config(repo, max_iter=10, no_progress_after=2)
    result = run_loop(cfg, MockAgent(behaviors=[]),   # a no-op agent: it changes nothing
                      iteration_gate=iteration, acceptance_gate=acceptance)
    assert result.reason is StopReason.NO_PROGRESS
    assert result.overfit is True


def test_correct_fix_reaches_done(tmp_path: Path):
    """A scripted fix lands on tick 2; only then does the held-out gate pass and the loop finish."""
    repo = _instantiate_demo(tmp_path)
    iteration, acceptance = _gates()

    def apply_fix(workspace: Path) -> str:
        (workspace / "pricing.py").write_text(_CORRECT)
        return "fixed boundary (>=10)"

    agent = MockAgent(behaviors=[lambda ws: "noop", apply_fix])
    cfg = _config(repo, max_iter=6, no_progress_after=5)
    result = run_loop(cfg, agent, iteration_gate=iteration, acceptance_gate=acceptance)
    assert result.reason is StopReason.DONE
    assert result.iterations == 2
