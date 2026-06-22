"""Shared demo/learn scenarios — one format, two front-ends.

A Scenario narrates and runs a loop through a `Stage`. `loopkit demo N` plays it straight
through; `loopkit learn N` plays the same scenario with pauses between beats. By default the
agent is a scripted MockAgent with real pytest gates (deterministic, no tokens, no network);
pass `--live` to use the real claude-code agent on the scenarios that support it.

Defining the Scenario once here is what makes the guided `learn` mode nearly free on top of
`demo` — the only difference is whether `Stage.beat` pauses.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from ..agent import Agent, ClaudeCodeAdapter, MockAgent
from ..config import AgentConfig, Config, GateConfig, SafetyConfig, StopsConfig
from ..gate import Gate, ShellGate
from ..loop import RunResult, run_loop

def demo_src() -> Path:
    """Locate the demo-repo: an explicit env path (set in the container), else a source checkout.

    `examples/` lives at the repo root, not inside the package, so a non-editable install (e.g.
    the Docker image) can't reach it by a package-relative path — the image sets
    LOOPKIT_DEMO_REPO instead.
    """
    env = os.environ.get("LOOPKIT_DEMO_REPO")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent / "examples" / "demo-repo"


PRICING_GOAL =("Implement line_total in pricing.py so a 10% bulk discount applies at "
                "quantity >= 10, per PROMPT.md.")

CORRECT_PRICING = '''\
"""Line-item pricing with a bulk discount."""


def line_total(unit_price, quantity):
    subtotal = unit_price * quantity
    if quantity >= 10:
        subtotal *= 0.9
    return round(subtotal, 2)
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def writes(name: str, content: str) -> Callable[[Path], str]:
    """A MockAgent behavior that writes `content` to `name` in the workspace."""
    def behavior(workspace: Path) -> str:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return f"wrote {name}"
    return behavior


def pytest_gates() -> tuple[ShellGate, ShellGate]:
    """The demo-repo's real gates, pinned to this interpreter so pytest is on hand."""
    py = sys.executable
    return (ShellGate(f"{py} -m pytest tests/seen -q"),
            ShellGate(f"{py} -m pytest tests/holdout -q"))


def demo_config(repo: Path, *, goal: str = PRICING_GOAL, max_iter: int = 10,
                no_progress_after: int = 3, budget: float = 5.0) -> Config:
    return Config(goal=goal, repo=str(repo), branch="loopkit/run",
                  gate=GateConfig(iteration="seen", acceptance="holdout"),
                  agent=AgentConfig(adapter="claude-code", max_cost_usd=budget),
                  stops=StopsConfig(max_iter=max_iter, no_progress_after=no_progress_after),
                  safety=SafetyConfig(protected_paths=["tests/"], require_clean_tree=False,
                                      allow_branches=["loopkit/*"]))


class Stage:
    """The narration + execution surface a scenario uses. demo: no pauses; learn: pauses."""

    def __init__(self, console: Console, *, live: bool, pause: bool, tmps: list[Path]) -> None:
        self.console = console
        self.live = live
        self.pause = pause
        self._tmps = tmps

    def beat(self, markup: str) -> None:
        self.console.print(f"[cyan]›[/] {markup}")
        if self.pause:
            self.console.input("[dim]  ⏎ continue …[/] ")

    def rule(self, label: str) -> None:
        self.console.print(Rule(label, style="dim"))

    def fixture(self) -> Path:
        """A fresh git-initialized copy of the demo-repo in a temp dir (cleaned after the run)."""
        tmp = Path(tempfile.mkdtemp(prefix="loopkit-demo-"))
        repo = tmp / "demo"
        shutil.copytree(demo_src(), repo)
        _git(repo, "init", "-q")
        _git(repo, "branch", "-m", "main")
        _git(repo, "config", "user.email", "demo@loopkit")
        _git(repo, "config", "user.name", "loopkit-demo")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed demo")
        self._tmps.append(tmp)
        return repo

    def agent(self, scripted: MockAgent) -> Agent:
        """The scripted agent by default; the real claude-code agent when --live."""
        return ClaudeCodeAdapter() if self.live else scripted

    def run(self, config: Config, agent: Agent, *, iteration_gate: Gate,
            acceptance_gate: Gate, review_hook=None, skills=None) -> RunResult:
        result = run_loop(config, agent, iteration_gate=iteration_gate,
                          acceptance_gate=acceptance_gate, review_hook=review_hook, skills=skills)
        self.console.print(_result_panel(result))
        return result


def _result_panel(result: RunResult) -> Panel:
    if result.reason.value == "done":
        color = "green"
    elif result.reason.value == "safety_halt":
        color = "red"
    else:
        color = "yellow"
    lines = [f"reason: [{color}]{result.reason.value}[/]",
             f"iterations: {result.iterations}", f"cost: ${result.cost_usd:.2f}"]
    if result.overfit:
        lines.append("[yellow]overfit: iteration gate passed, held-out gate did not[/]")
    return Panel.fit("\n".join(lines), title="result")


@dataclass
class Scenario:
    chapter: int
    slug: str
    title: str
    teaches: str
    live_supported: bool
    run: Callable[[Stage], None]


def _registry() -> dict[int, Scenario]:
    # Imported lazily so chapter modules can import helpers from this package without a cycle.
    from . import (ch05_context, ch07_feedback, ch08_review, ch09_held_out, ch10_orchestration,
                   ch11_evolution, ch12_fleet, ch13_hard_stops, ch14_economics, ch16_safety,
                   ch17_skills, ch20_triggers, ch21_ci, ch22_isolation, ch23_skills_repo,
                   ch24_reliability)
    items = [ch05_context.SCENARIO, ch07_feedback.SCENARIO, ch08_review.SCENARIO,
             ch09_held_out.SCENARIO, ch10_orchestration.SCENARIO, ch11_evolution.SCENARIO,
             ch12_fleet.SCENARIO, ch13_hard_stops.SCENARIO, ch14_economics.SCENARIO,
             ch16_safety.SCENARIO, ch17_skills.SCENARIO,
             # Part III — productionizing the loop into the GitHub/GitLab ecosystem (course Ch 20-22)
             # plus the skills repo (Phase 5b: the Ch 17 flywheel made durable across machines) and
             # the reliability measurement layer (pass^k — discovery vs. reliability).
             ch20_triggers.SCENARIO, ch21_ci.SCENARIO, ch22_isolation.SCENARIO,
             ch23_skills_repo.SCENARIO, ch24_reliability.SCENARIO]
    return {s.chapter: s for s in items}


def available() -> list[Scenario]:
    return sorted(_registry().values(), key=lambda s: s.chapter)


def play(chapter: int, console: Console, *, live: bool = False, pause: bool = False) -> None:
    registry = _registry()
    scenario = registry.get(chapter)
    if scenario is None:
        chapters = ", ".join(str(c) for c in sorted(registry))
        console.print(f"[red]no scenario for chapter {chapter}[/] — available: {chapters}")
        return
    console.print(Panel.fit(f"[bold]Ch {scenario.chapter} — {scenario.title}[/]\n{scenario.teaches}",
                            title="loopkit learn" if pause else "loopkit demo"))
    if live and not scenario.live_supported:
        console.print("[yellow]note:[/] this is a scripted demonstration; --live is not "
                      "applicable. Running the scripted version.")
        live = False
    tmps: list[Path] = []
    stage = Stage(console, live=live, pause=pause, tmps=tmps)
    try:
        scenario.run(stage)
    finally:
        for tmp in tmps:
            shutil.rmtree(tmp, ignore_errors=True)
