"""Ch 17 — skills and the write-back flywheel: a solved run teaches the next one."""
from __future__ import annotations

from pathlib import Path

from ..agent import AgentResult
from ..extensions.skills import InMemorySkillRegistry, Skill
from . import CORRECT_PRICING, Scenario, Stage, demo_config, pytest_gates

# The naive attempt: the demo-repo's seeded bug (`> 10`). It passes the visible tests but misses
# the boundary at quantity == 10, so the held-out gate fails it.
NAIVE_PRICING = '''\
"""Line-item pricing with a bulk discount."""


def line_total(unit_price, quantity):
    subtotal = unit_price * quantity
    if quantity > 10:          # misses the boundary at exactly 10
        subtotal *= 0.9
    return round(subtotal, 2)
'''

# The lesson distilled from a successful run. The marker the agent recognises is ">= 10".
BOUNDARY_SKILL = Skill(name="pricing-boundary",
                       guidance="When implementing line_total, apply the bulk discount at "
                                "quantity >= 10 (inclusive) — the boundary at exactly 10 is the "
                                "held-out case the visible tests miss.")


class SkillSeekingAgent:
    """A prompt-aware stand-in: it writes the correct fix only once it *knows* the boundary rule.

    It learns that rule one of two ways — from the held-out feedback after an overfit tick (the
    distinctive phrase "fit the visible tests"), or, for free, from the `pricing-boundary` skill
    a past run rendered into its prompt. Those two markers are the signal precisely because
    neither appears in the static anchor (PROMPT.md states the spec in other words), so an
    unhelped first tick genuinely doesn't know. The skill path is the flywheel: the lesson
    arrives before the mistake instead of after it.
    """

    def act(self, prompt: str, workspace: Path, *, observer=None) -> AgentResult:
        knows_boundary = "pricing-boundary" in prompt or "fit the visible tests" in prompt
        content = CORRECT_PRICING if knows_boundary else NAIVE_PRICING
        (workspace / "pricing.py").write_text(content)
        return AgentResult(ok=True, cost_usd=0.5,
                           summary="wrote correct" if knows_boundary else "wrote naive")


def _distill_boundary(run_result, workspace: Path, goal: str):
    """Distil the boundary lesson from a solved pricing run (a real distiller would ask the agent)."""
    return BOUNDARY_SKILL if "pricing" in goal.lower() or "line_total" in goal else None


def run(stage: Stage) -> None:
    iteration, acceptance = pytest_gates()
    # Gated write-back: only a run that clears the held-out gate may mint a skill. Ungated, the
    # flywheel would learn from overfit runs too — and then teach the bug to every future run.
    registry = InMemorySkillRegistry(write_back_gate=acceptance, distill=_distill_boundary)

    stage.beat("[bold]Run A — no skills yet.[/] The agent writes the naive version, the held-out "
               "gate rejects it, and only then does it learn the boundary rule and fix it. Two "
               "ticks. On done, that lesson is distilled into a skill — but only because the run "
               "cleared the [italic]write-back gate[/]; an overfit run would mint nothing.")
    repo_a = stage.fixture()
    cfg_a = demo_config(repo_a, max_iter=6, no_progress_after=5)
    result_a = stage.run(cfg_a, SkillSeekingAgent(), iteration_gate=iteration,
                         acceptance_gate=acceptance, skills=registry)

    learned = ", ".join(s.name for s in registry.skills) or "nothing"
    stage.beat(f"The registry now holds: [green]{learned}[/]. Same lesson, now reusable.")

    stage.beat("[bold]Run B — same registry, fresh repo.[/] This time the boundary skill is "
               "rendered into the prompt from the start, so the agent gets it right on tick 1. "
               "No overfit detour. That is the flywheel: a solved run made the next one cheaper.")
    repo_b = stage.fixture()
    cfg_b = demo_config(repo_b, max_iter=6, no_progress_after=5)
    result_b = stage.run(cfg_b, SkillSeekingAgent(), iteration_gate=iteration,
                         acceptance_gate=acceptance, skills=registry)

    stage.beat(f"Run A took [yellow]{result_a.iterations}[/] ticks learning the rule; run B took "
               f"[green]{result_b.iterations}[/], handed it. Gains compound across runs — and the "
               "write-back gate is what keeps the thing being compounded honest.")


SCENARIO = Scenario(chapter=17, slug="skills", title="The write-back flywheel",
                    teaches="Distil a solved run into a skill, render it into future prompts — "
                            "gains compound; gate write-back so the flywheel never learns junk.",
                    live_supported=False, run=run)
