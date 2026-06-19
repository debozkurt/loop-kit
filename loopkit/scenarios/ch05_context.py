"""Ch 5 — context reset: a fresh, fixed context each tick beats a growing, rotting one."""
from __future__ import annotations

from ..config import Config, GateConfig
from ..prompt import build_prompt
from . import PRICING_GOAL, Scenario, Stage, demo_src


def run(stage: Stage) -> None:
    cfg = Config(goal=PRICING_GOAL, repo=str(demo_src()), gate=GateConfig(iteration="x"))
    stage.beat("The ralph discipline rebuilds the prompt from anchors + the last feedback every "
               "tick, so context stays a fixed size instead of growing with history.")

    fresh: list[int] = []
    growing: list[int] = []
    transcript = ""
    for tick in range(1, 6):
        feedback = f"attempt {tick - 1} failed: assertion error (tick {tick})" if tick > 1 else None
        prompt = build_prompt(cfg, feedback)
        fresh.append(len(prompt))
        transcript += (feedback or "") + "\n" + "...model output...\n" * 8   # naive accumulation
        growing.append(len(prompt) + len(transcript))

    stage.console.print("  [bold]tick   fresh (ralph)   growing (naive history)[/]")
    for i, (f, g) in enumerate(zip(fresh, growing), start=1):
        stage.console.print(f"  {i:>4}   {f:>12}   {g:>22}")
    stage.beat("Fresh context is flat; naive history climbs every tick until it rots or overflows. "
               "Durable state lives in the anchor files, not the conversation.")


SCENARIO = Scenario(chapter=5, slug="context-reset", title="Context reset",
                    teaches="A fresh, fixed context each tick beats a growing, rotting one.",
                    live_supported=False, run=run)
