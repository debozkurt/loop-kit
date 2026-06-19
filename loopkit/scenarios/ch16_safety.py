"""Ch 16 — protected paths & blast radius: the guard holds whatever the model tries."""
from __future__ import annotations

from pathlib import Path

from ..agent import MockAgent
from ..gate import AlwaysPass, CallableGate
from . import Scenario, Stage, demo_config


def run(stage: Stage) -> None:
    repo = stage.fixture()
    stage.beat("The loop may not touch a protected path. Here the agent tries to weaken a held-out "
               "test; the guard reverts the tick and halts before the change can land.")

    def tamper(workspace: Path) -> str:
        target = workspace / "tests" / "holdout" / "sneaky.py"
        target.write_text("# weaken the held-out check\n")
        return "tried to edit tests/holdout"

    stage.run(demo_config(repo, max_iter=5, no_progress_after=5),
              MockAgent(behaviors=[tamper]),
              iteration_gate=CallableGate(lambda ws: False), acceptance_gate=AlwaysPass())
    stage.beat("Blast-radius containment: the protected path is enforced regardless of what the "
               "model decided to do — the held-out gate stays trustworthy.")


SCENARIO = Scenario(chapter=16, slug="safety", title="Protected paths & blast radius",
                    teaches="A safety guard bounds what the loop can touch, no matter what the model tries.",
                    live_supported=False, run=run)
