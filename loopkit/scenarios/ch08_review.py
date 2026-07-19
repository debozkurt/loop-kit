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

    # ---- Act 2: the BUILT-IN default judge — review with zero configuration ---------------------
    stage.beat("Since the default-judge work, review is [bold]on by default[/]: with no [review] "
               "command configured the loop runs the bundled adversarial judge — fresh clean "
               "context, real-defects-only, nonce'd verdict, judged only behind a [italic]green[/] "
               "iteration gate. Same fix→re-review discipline, zero setup. Here it runs with a "
               "scripted judge backend (no tokens): REJECT on the debug print, APPROVE on the fix.")

    from ..config import AgentConfig, ReviewConfig
    from ..extensions.judge import DefaultReviewHook

    repo2 = stage.fixture()
    verdicts = iter(["REJECT — pricing.py:5 leftover debug print() ships to production",
                     "APPROVE"])

    def scripted_judge(prompt: str, target) -> tuple[str, float]:
        # Echo the per-call nonce back — the anti-spoof grammar the real judge enforces.
        nonce = prompt.rsplit("VERDICT[", 1)[1].split("]")[0]
        return f"VERDICT[{nonce}]: {next(verdicts)}", 0.0

    judge = DefaultReviewHook(ReviewConfig(), AgentConfig(adapter="claude-code"), repo2,
                              "correct line-item pricing with a bulk discount",
                              runner=scripted_judge)
    scripted2 = MockAgent(behaviors=[writes("pricing.py", PRICING_WITH_DEBUG),
                                     writes("pricing.py", CORRECT_PRICING)])
    cfg2 = demo_config(repo2, max_iter=6, no_progress_after=5)
    stage.run(cfg2, stage.agent(scripted2), iteration_gate=pytest_gates()[0],
              acceptance_gate=pytest_gates()[1], review_hook=judge)

    stage.beat("Identical arc, no hand-written review hook: the bundled judge rejected the debug "
               "print, the verdict fed back, the fix cleared it. Verdicts are sticky per commit "
               "(an unchanged HEAD is never re-billed), a judge that can't run halts the loop "
               "(REVIEW_UNAVAILABLE — infra failure is not a rejection), and N straight rejections "
               "stop it for a human (REVIEW_STALL). `--no-review` opts out; a [review] command "
               "overrides.")


SCENARIO = Scenario(chapter=8, slug="review", title="Continuous review gates done",
                    teaches="A clean review is a precondition for done: review catches what green "
                            "tests don't, and feeds fixes back while the context is fresh — and "
                            "with the built-in default judge it runs with zero configuration.",
                    live_supported=False, run=run)
