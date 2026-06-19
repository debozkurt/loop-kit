"""The agent: the model as a subroutine the loop invokes (Chapters 1-3).

The loop never speaks to a specific vendor — it calls `Agent.act(prompt, workspace)` and
gets back an `AgentResult`. Swapping Claude Code for Codex (or a deterministic MockAgent for
tests and demos) is a one-line config change. Each adapter is also responsible for the one
thing the loop's economics (Ch 14) depend on: reporting the cost of the tick in dollars,
normalized across vendors.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@dataclass
class AgentResult:
    """The outcome of one invocation. `ok` means the call ran, not that the work is done."""

    ok: bool
    cost_usd: float = 0.0          # normalized cost of this tick (Ch 14)
    summary: str = ""              # short, payload-free description for logs
    raw_tail: str = ""             # last chars of agent output, for feedback only


@runtime_checkable
class Agent(Protocol):
    def act(self, prompt: str, workspace: Path) -> AgentResult: ...


class MockAgent:
    """A deterministic agent for tests, demos, and dry runs.

    Driven by a list of `behaviors`: each is called with the workspace path on its tick and
    may mutate files to simulate the agent's edits, returning a short summary string. When
    the behaviors run out, the agent is a no-op — which the loop reads as 'no progress'. Cost
    is charged per tick, so budget stops (Ch 14) are exercisable without spending a cent.
    """

    def __init__(self, behaviors: list[Callable[[Path], str]] | None = None,
                 cost_per_tick: float = 0.5) -> None:
        self._behaviors = list(behaviors or [])
        self._cost = cost_per_tick
        self._i = 0

    def act(self, prompt: str, workspace: Path) -> AgentResult:
        summary = "noop"
        if self._i < len(self._behaviors):
            summary = self._behaviors[self._i](workspace) or "edit"
        self._i += 1
        return AgentResult(ok=True, cost_usd=self._cost, summary=summary)


class _CLIAdapter:
    """Shared base for agents that shell out to a headless coding-agent CLI."""

    binary: str = ""

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None,
                 prompt_flag: str = "-p") -> None:
        self.model = model
        self.extra_args = list(extra_args or [])
        self.prompt_flag = prompt_flag

    def _command(self, prompt: str) -> list[str]:
        cmd = [self.binary, self.prompt_flag, prompt]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args

    def act(self, prompt: str, workspace: Path) -> AgentResult:
        proc = subprocess.run(self._command(prompt), cwd=workspace,
                              capture_output=True, text=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        return AgentResult(ok=proc.returncode == 0,
                           cost_usd=self._parse_cost(out),
                           summary=f"rc={proc.returncode} outLen={len(out)}",
                           raw_tail=out[-2000:])

    @staticmethod
    def _parse_cost(output: str) -> float:
        # Vendors print cost differently and change it between versions; keep parsing in one
        # place per adapter. Unknown cost is reported as 0.0 (the budget stop then can't fire,
        # which `loopkit doctor` warns about).
        return 0.0


class ClaudeCodeAdapter(_CLIAdapter):
    """`claude -p "<prompt>"` headless. Primary adapter."""

    binary = "claude"


class CodexAdapter(_CLIAdapter):
    """Codex parity: same contract, different binary/flags."""

    binary = "codex"


def build_agent(cfg) -> Agent:
    """Resolve the configured adapter name to a concrete Agent (AgentConfig in)."""
    name = cfg.adapter
    if name == "mock":
        return MockAgent()
    if name == "claude-code":
        return ClaudeCodeAdapter(model=cfg.model, extra_args=cfg.args)
    if name == "codex":
        return CodexAdapter(model=cfg.model, extra_args=cfg.args)
    raise ValueError(f"unknown agent adapter: {name!r} (expected: mock | claude-code | codex)")
