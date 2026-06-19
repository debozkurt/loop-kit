"""Ch 7 — the feedback imperative: a closed loop drives toward the target."""
from __future__ import annotations

from ..agent import MockAgent
from . import CORRECT_PRICING, Scenario, Stage, demo_config, pytest_gates, writes

_BROKEN = '''\
"""Line-item pricing (broken: discount and rounding dropped)."""


def line_total(unit_price, quantity):
    return unit_price * quantity
'''


def run(stage: Stage) -> None:
    repo = stage.fixture()
    stage.beat("A closed loop turns the gate's failure into the next tick's input. Tick 1 ships "
               "broken code; watch the gate reject it and tick 2 build on the diagnostics.")
    iteration, acceptance = pytest_gates()
    scripted = MockAgent(behaviors=[writes("pricing.py", _BROKEN),
                                    writes("pricing.py", CORRECT_PRICING)])
    cfg = demo_config(repo, max_iter=6, no_progress_after=5)
    stage.run(cfg, stage.agent(scripted), iteration_gate=iteration, acceptance_gate=acceptance)
    stage.beat("Without the gate, tick 1's broken code would have been the foundation the next tick "
               "built on. The feedback is the loop, not the model.")


SCENARIO = Scenario(chapter=7, slug="feedback", title="The feedback imperative",
                    teaches="A closed loop feeds each gate failure back as the next tick's input.",
                    live_supported=True, run=run)
