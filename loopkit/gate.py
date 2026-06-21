"""Verification gates: the external check that closes the loop (Chapters 6-7, 9).

A gate answers one question about the workspace: pass or fail, and if fail, the diagnostics
to feed back. The loop uses two of them. The *iteration gate* is fast and in-sample — what
the loop optimizes against every tick. The *acceptance gate* is held-out — run once on a
candidate that passed iteration, against checks the loop never optimized against — and it is
what tells you the green was real rather than overfit (the honest twin of gaming the gate,
Chapter 9).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:                                  # typing-only: avoids a gate↔executor import cycle
    from .executor import ToolExecutor


@dataclass
class GateResult:
    passed: bool
    feedback: str | None = None    # diagnostics on failure, to feed into the next tick


class Gate(Protocol):
    def check(self, workspace: Path) -> GateResult: ...


class ShellGate:
    """Run a shell command; exit 0 is pass, anything else fails with the output tail.

    The command itself runs through a `ToolExecutor` (default `LocalToolExecutor`, in-process). The
    cloud worker injects a `RemoteToolExecutor` so the held-out gate — which runs agent-authored tests
    — executes in the keyless executor sidecar, not in the key-holding loopkit-core (Phase 6). The
    credential-free env + `PYTHONDONTWRITEBYTECODE` handling lives in the executor's `run_gate`.
    """

    def __init__(self, command: str, feedback_tail: int = 2000,
                 *, executor: "ToolExecutor | None" = None) -> None:
        self.command = command
        self._tail = feedback_tail
        self._executor = executor

    def check(self, workspace: Path) -> GateResult:
        executor = self._executor
        if executor is None:
            from .executor import LocalToolExecutor   # deferred — breaks the import cycle
            executor = LocalToolExecutor()
        return executor.run_gate(self.command, workspace, tail=self._tail)


class CallableGate:
    """Wrap a Python predicate as a gate — used in tests and Python-native checks."""

    def __init__(self, fn: Callable[[Path], bool], feedback: str = "callable gate failed") -> None:
        self._fn = fn
        self._feedback = feedback

    def check(self, workspace: Path) -> GateResult:
        ok = bool(self._fn(workspace))
        return GateResult(ok, None if ok else self._feedback)


class AlwaysPass:
    """The default acceptance gate when none is configured — i.e. no held-out check.

    Using it makes the missing safety net explicit in the logs (`gate.acceptance passed=True`
    every time) rather than silently skipping the held-out step.
    """

    def check(self, workspace: Path) -> GateResult:
        return GateResult(True, None)
