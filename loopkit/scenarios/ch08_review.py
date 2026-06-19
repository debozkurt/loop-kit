"""Ch 8 — continuous review: a clean review is a precondition for done, not just green tests."""
from __future__ import annotations

from pathlib import Path

from ..agent import MockAgent
from ..extensions.review import CallableReviewHook
from . import CORRECT_PRICING, Scenario, Stage, demo_config, pytest_gates, writes

# Correct pricing — but with a leftover debug print. The tests pass (behaviour is right), so the
# gate is green; review is what catches the artifact the gate never encodes.
PRICING_WITH_DEBUG = '''\
"""Line-item pricing with a bulk discount."""


def line_total(unit_price, quantity):
    print("debug:", unit_price, quantity)   # leftover debugging — should never ship
    subtotal = unit_price * quantity
    if quantity >= 10:
        subtotal *= 0.9
    return round(subtotal, 2)
'''


def _no_debug_artifacts(workspace: Path) -> bool:
    """A clean review: no leftover debug prints or FIXME markers in the changed module."""
    text = (workspace / "pricing.py").read_text()
    return "print(" not in text and "FIXME" not in text


def run(stage: Stage) -> None:
    repo = stage.fixture()
    stage.beat("Green tests are not a clean diff. On tick 1 the agent writes [bold]correct[/] "
               "pricing — the tests pass — but it leaves a debug [italic]print()[/] behind. A "
               "review runs on every commit and catches exactly what the gate doesn't encode.")

    iteration, acceptance = pytest_gates()
    review = CallableReviewHook(_no_debug_artifacts,
                                feedback="Remove the leftover debug print() before this can be "
                                         "accepted — keep the behaviour, drop the artifact.")
    scripted = MockAgent(behaviors=[writes("pricing.py", PRICING_WITH_DEBUG),
                                    writes("pricing.py", CORRECT_PRICING)])
    cfg = demo_config(repo, max_iter=6, no_progress_after=5)
    stage.run(cfg, stage.agent(scripted), iteration_gate=iteration, acceptance_gate=acceptance,
              review_hook=review)

    stage.beat("Tick 1's tests were green but the review blocked it on the debug print and fed "
               "that back; tick 2 removed it, the review cleared, and only then was it done. "
               "Review is the gate for everything the tests can't see — and it runs while the "
               "context that wrote the diff is still fresh (the roborev fix -> re-review loop).")


SCENARIO = Scenario(chapter=8, slug="review", title="Continuous review gates done",
                    teaches="A clean review is a precondition for done: review catches what green "
                            "tests don't, and feeds fixes back while the context is fresh.",
                    live_supported=False, run=run)
