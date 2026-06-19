"""Ch 10 — orchestration: fan out many isolated worker loops, one git worktree each."""
from __future__ import annotations

from pathlib import Path

from rich.table import Table

from ..agent import MockAgent
from ..extensions.orchestrate import Supervisor
from ..gate import CallableGate
from . import Scenario, Stage, demo_config, writes

# Three independent slices of work. Each is a self-contained task with its own goal, branch
# slug, and the file that proves it done — exactly the shape a fan-out feeds on: no task needs
# anything another task produces.
TASKS = [
    {"goal": "Add the greeting helper", "slug": "greet", "file": "greeting.py"},
    {"goal": "Add the farewell helper", "slug": "farewell", "file": "farewell.py"},
    {"goal": "Add the pricing helper", "slug": "pricing", "file": "discount.py"},
]


def _make_agent(task: dict) -> MockAgent:
    """A scripted worker that writes its one file on tick 1 — stands in for a real agent."""
    return MockAgent(behaviors=[writes(task["file"], f"# {task['goal']}\n")])


def _make_gates(task: dict, workspace: Path):
    """Iteration + acceptance both pass once this task's file lands in its own worktree."""
    gate = CallableGate(lambda ws: (ws / task["file"]).exists(),
                        feedback=f"{task['file']} not written yet")
    return gate, gate


def run(stage: Stage) -> None:
    repo = stage.fixture()
    stage.beat("One loop solves one task. Orchestration runs three at once — each in its own "
               "git worktree (a separate working directory backed by the same repo), so three "
               "workers edit in parallel without ever colliding. The single-agent loop is the "
               "worker body, unchanged.")

    cfg = demo_config(repo, max_iter=4, no_progress_after=3)
    supervisor = Supervisor(cfg, make_agent=_make_agent, make_gates=_make_gates, max_workers=3)
    fleet = supervisor.run_fleet(TASKS)

    stage.console.print(_fleet_table(fleet))
    stage.beat(f"{len(fleet.done)}/{len(fleet.workers)} workers reached done, each on its own "
               "[bold]loopkit/run-<slug>[/] branch — and the main checkout never saw a single "
               "one of their files:")

    # The isolation payoff, shown: every worker's file is on its branch, none in the main tree.
    leaked = [t["file"] for t in TASKS if (repo / t["file"]).exists()]
    verdict = "none leaked into main ✓" if not leaked else f"LEAKED: {leaked}"
    stage.console.print(f"  [dim]main working tree:[/] {verdict}")

    stage.beat("That isolation is the whole point: with workers physically separated you can run "
               "[bold]N attempts at the same task[/] and keep the best — the evolutionary strategy "
               "that layers on next, with the Ch 9 selection-inflation guard to keep best-of-N "
               "from just overfitting to noise.")


def _fleet_table(fleet) -> Table:
    table = Table(title="fleet result", header_style="bold")
    table.add_column("task")
    table.add_column("branch")
    table.add_column("terminal")
    table.add_column("iters", justify="right")
    for worker in fleet.workers:
        reason = worker.result.reason.value if worker.result else (worker.error or "error")
        color = "green" if worker.done else "yellow"
        iters = str(worker.result.iterations) if worker.result else "-"
        table.add_row(worker.task["slug"], worker.branch, f"[{color}]{reason}[/]", iters)
    return table


SCENARIO = Scenario(chapter=10, slug="orchestration", title="Fan-out over isolated workers",
                    teaches="Orchestration: many loops run in parallel, one git worktree each, "
                            "with no collisions.",
                    live_supported=False, run=run)
