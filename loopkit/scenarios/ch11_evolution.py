"""Ch 11 — evolutionary search and the selection-inflation guard (Ch 9 at the fleet scale)."""
from __future__ import annotations

from pathlib import Path

from rich.table import Table

from ..agent import MockAgent
from ..extensions.orchestrate import Supervisor
from ..gate import CallableGate
from . import Scenario, Stage, demo_config, writes

# Four attempts at one goal. The selection score is what you'd rank on; the held-out column is
# the truth you don't get to see at selection time. The 'memorizer' games the visible score to
# the top yet fails the held-out check — exactly the candidate best-of-N is most likely to crown.
KINDS = ["memorizer", "solver", "partial", "broken"]
SELECTION = {"memorizer": 1.0, "solver": 0.9, "partial": 0.6, "broken": 0.2}
HELD_OUT = {"memorizer": False, "solver": True, "partial": True, "broken": False}


def _candidate_task(base_task: dict, generation: int, candidate: int, seed_branch):
    task = dict(base_task)
    task["slug"] = f"g{generation}-c{candidate}"
    task["kind"] = KINDS[candidate]
    return task


def _make_agent(task: dict) -> MockAgent:
    return MockAgent(behaviors=[writes("solution.py", f"# {task['kind']} attempt\n")])


def _make_gates(task: dict, workspace: Path):
    gate = CallableGate(lambda ws: (ws / "solution.py").exists())
    return gate, gate


def _score(task: dict, workspace: Path) -> float:
    return SELECTION[task["kind"]]


def _revalidate(task: dict, workspace: Path) -> CallableGate:
    passes = HELD_OUT[task["kind"]]
    return CallableGate(lambda ws: passes)


def run(stage: Stage) -> None:
    repo = stage.fixture()
    stage.beat("Fan-out lets you run [bold]N attempts at one goal[/] and keep the best. But "
               "'keep the best by the visible score' is the same trap as Ch 9, one level up: the "
               "top of N noisy candidates wins partly on skill and partly on luck, so its score "
               "is [italic]inflated[/]. Watch the highest scorer get caught by a held-out gate it "
               "never competed on.")

    cfg = demo_config(repo, max_iter=3, no_progress_after=2)
    supervisor = Supervisor(cfg, make_agent=_make_agent, make_gates=_make_gates, max_workers=4)
    result = supervisor.evolve({"goal": "Implement solution.py"}, generations=1, population=4,
                               keep=2, score=_score, revalidate=_revalidate,
                               candidate_task=_candidate_task)

    gen = result.generations[0]
    stage.console.print(_evolution_table(gen))
    top = gen.survivors[0]
    winner = result.winner
    stage.beat(f"The top selection score was [yellow]{top.task['kind']}[/] at "
               f"{top.score:.1f} — and it [red]failed[/] the held-out gate. Re-validation walked "
               f"down the survivors and confirmed [green]{winner.task['kind']}[/] instead.")
    stage.beat("That is the load-bearing rule of evolutionary search: only a [bold]re-validated[/] "
               "winner reseeds the next generation, so a lucky overfit can never compound. "
               "Best-of-N is a way to overfit; a held-out check is the only honest defence.")


def _evolution_table(gen) -> Table:
    table = Table(title=f"generation {gen.index} — selection vs held-out", header_style="bold")
    table.add_column("candidate")
    table.add_column("selection", justify="right")
    table.add_column("kept")
    table.add_column("held-out")
    table.add_column("outcome")
    survivors = {c.branch for c in gen.survivors}
    winner = gen.confirmed.branch if gen.confirmed else None
    for cand in gen.candidates:
        kept = "yes" if cand.branch in survivors else "—"
        # The held-out verdict is only meaningful for survivors (only they were re-validated).
        held = ("[green]pass[/]" if HELD_OUT[cand.task["kind"]] else "[red]fail[/]") \
            if cand.branch in survivors else "—"
        if cand.branch == winner:
            outcome = "[green]✓ confirmed winner[/]"
        elif cand.branch in survivors:
            outcome = "[yellow]inflated — rejected[/]"
        else:
            outcome = ""
        table.add_row(cand.task["kind"], f"{cand.score:.1f}", kept, held, outcome)
    return table


SCENARIO = Scenario(chapter=11, slug="evolution", title="Evolutionary search, validated",
                    teaches="Best-of-N inflates the winner's score; re-validate on a held-out "
                            "gate or the lucky overfit wins.",
                    live_supported=False, run=run)
