"""Ch 13 — the three hard stops, and the precedence between them."""
from __future__ import annotations

from ..agent import MockAgent
from ..gate import AlwaysPass, CallableGate
from . import Scenario, Stage, demo_config, writes

_NEVER = CallableGate(lambda ws: False)     # an iteration gate that never passes -> never DONE
_PASS = AlwaysPass()


def run(stage: Stage) -> None:
    stage.beat("Three bad terminals, one precedence: BUDGET > NO_PROGRESS > ITERATION_CAP. "
               "Each is forced below with a different misbehaving agent.")

    stage.rule("no-progress — an idle agent")
    repo = stage.fixture()
    stage.run(demo_config(repo, max_iter=10, no_progress_after=2),
              MockAgent(behaviors=[]), iteration_gate=_NEVER, acceptance_gate=_PASS)

    stage.rule("budget — spends but never finishes")
    repo = stage.fixture()
    busy = MockAgent(behaviors=[writes(f"f{i}.txt", str(i)) for i in range(30)], cost_per_tick=0.5)
    stage.run(demo_config(repo, max_iter=20, no_progress_after=99, budget=1.0),
              busy, iteration_gate=_NEVER, acceptance_gate=_PASS)

    stage.rule("iteration cap — real progress, but never done")
    repo = stage.fixture()
    slow = MockAgent(behaviors=[writes(f"g{i}.txt", str(i)) for i in range(30)], cost_per_tick=0.01)
    stage.run(demo_config(repo, max_iter=3, no_progress_after=99, budget=100.0),
              slow, iteration_gate=_NEVER, acceptance_gate=_PASS)

    stage.beat("Budget halts even mid-progress; no-progress catches a stall before the cap; the cap "
               "is the backstop when neither fires.")


SCENARIO = Scenario(chapter=13, slug="hard-stops", title="The three hard stops",
                    teaches="Every loop needs an iteration cap, no-progress detection, and a budget ceiling.",
                    live_supported=False, run=run)
