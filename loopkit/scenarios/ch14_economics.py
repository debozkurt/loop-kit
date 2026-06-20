"""Ch 14 — the economics: the 2×2 adapter matrix and the cost that makes the budget stop bite.

The agent is a swappable strategy — CLI or API, Claude or OpenAI. The API adapters run the
edit/bash loop in-process, so they get the provider's **native token usage**, which `pricing.py`
turns into an exact dollar cost per tick. That real number is what lets the budget ceiling (Ch 13-14)
fire mid-run instead of being decorative. Both beats below are token-free: a scripted `claude-api`
backend returns canned turns + usage, so the dollars are real arithmetic on fake tokens.
"""
from __future__ import annotations

from ..agent import ClaudeAPIAdapter, _ToolCall, _Turn
from ..pricing import Usage
from . import CORRECT_PRICING, Scenario, Stage, demo_config, pytest_gates


class _SolveBackend:
    """A scripted claude-api backend that fixes the bulk-discount bug, then stops — charging
    realistic token usage so the run shows a genuine (token-free) dollar cost."""

    model = "claude-opus-4-8"

    def __init__(self) -> None:
        self._n = 0

    def complete(self, transcript, tools) -> _Turn:
        self._n += 1
        if self._n == 1:
            return _Turn(text="Applying the 10% discount at quantity >= 10.",
                         tool_calls=[_ToolCall("call_1", "write_file",
                                              {"path": "pricing.py", "content": CORRECT_PRICING})],
                         usage=Usage(input_tokens=1500, output_tokens=320))
        return _Turn(text="Done — boundary handled.", tool_calls=[],
                     usage=Usage(input_tokens=1800, output_tokens=60))


class _SpendBackend:
    """Edits every tick but never solves; each model call burns ~$1.50 on Opus, so a $2 ceiling is
    crossed on the second tick — the budget stop biting on real cost."""

    model = "claude-opus-4-8"

    def __init__(self) -> None:
        self._n = 0

    def complete(self, transcript, tools) -> _Turn:
        i = self._n
        self._n += 1
        if i % 2 == 0:
            return _Turn(text="", tool_calls=[_ToolCall(f"c{i}", "write_file",
                                                       {"path": f"notes_{i}.md", "content": "WIP"})],
                         usage=Usage(input_tokens=300_000, output_tokens=2000))
        return _Turn(text="still working", tool_calls=[], usage=Usage(output_tokens=20))


def run(stage: Stage) -> None:
    stage.beat("The agent is a swappable strategy — a 2×2 matrix: {CLI, API} × {Claude, OpenAI}, "
               "plus the token-free mock. CLI adapters shell out and we parse cost from output; "
               "API adapters run the tool loop in-process and get native usage.")
    stage.beat("Native usage matters: pricing.py turns input/output/cache tokens into an exact "
               "cost_usd per tick, which is summed into the budget ceiling. That's what makes the "
               "budget stop actually bite (Ch 14) instead of trusting a hand-set number.")

    stage.rule("claude-api solves the demo — cost computed from token usage (zero tokens spent)")
    repo = stage.fixture()
    seen, holdout = pytest_gates()
    stage.run(demo_config(repo, budget=5.0), ClaudeAPIAdapter(backend=_SolveBackend()),
              iteration_gate=seen, acceptance_gate=holdout)

    stage.rule("same adapter, a $2.00 ceiling — the budget stop bites mid-run")
    repo = stage.fixture()
    seen, holdout = pytest_gates()
    stage.run(demo_config(repo, budget=2.0, no_progress_after=99),
              ClaudeAPIAdapter(backend=_SpendBackend(), max_tool_calls=4),
              iteration_gate=seen, acceptance_gate=holdout)

    stage.beat("The first run finishes for a few cents; the second is halted by the ceiling once "
               "the real per-tick cost crosses $2. Money already spent can't be unspent — so budget "
               "outranks every other bad terminal (DONE > SAFETY > BUDGET > NO_PROGRESS > CAP).")


SCENARIO = Scenario(chapter=14, slug="economics",
                    title="The 2×2 adapter matrix & real cost accounting",
                    teaches="Native token usage → exact per-tick cost → the budget ceiling bites.",
                    live_supported=False, run=run)
