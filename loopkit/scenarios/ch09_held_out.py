"""Ch 9 — the held-out acceptance gate: passing the visible tests isn't solving the goal."""
from __future__ import annotations

from ..agent import MockAgent
from . import CORRECT_PRICING, Scenario, Stage, demo_config, pytest_gates, writes


def run(stage: Stage) -> None:
    repo = stage.fixture()
    stage.beat("The visible tests pass even with the seeded bug — they miss the boundary at "
               "quantity 10. Watch the held-out acceptance gate refuse to call it done on tick 1, "
               "then accept the real fix on tick 2.")
    iteration, acceptance = pytest_gates()
    scripted = MockAgent(behaviors=[lambda ws: "noop (visible tests already pass)",
                                    writes("pricing.py", CORRECT_PRICING)])
    cfg = demo_config(repo, max_iter=6, no_progress_after=5)
    stage.run(cfg, stage.agent(scripted), iteration_gate=iteration, acceptance_gate=acceptance)
    stage.beat("A green iteration gate is necessary, not sufficient. The held-out gate is what tells "
               "you the green was real rather than overfit.")


SCENARIO = Scenario(chapter=9, slug="held-out", title="The held-out acceptance gate",
                    teaches="Overfitting the gate: passing the visible tests is not solving the goal.",
                    live_supported=True, run=run)
