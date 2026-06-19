"""Verification gates: the external check that closes the loop (Chapters 6-7, 9).

A gate answers one question about the workspace: pass or fail, and if fail, the diagnostics
to feed back. The loop uses two of them. The *iteration gate* is fast and in-sample — what
the loop optimizes against every tick. The *acceptance gate* is held-out — run once on a
candidate that passed iteration, against checks the loop never optimized against — and it is
what tells you the green was real rather than overfit (the honest twin of gaming the gate,
Chapter 9).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass
class GateResult:
    passed: bool
    feedback: str | None = None    # diagnostics on failure, to feed into the next tick


class Gate(Protocol):
    def check(self, workspace: Path) -> GateResult: ...


class ShellGate:
    """Run a shell command; exit 0 is pass, anything else fails with the output tail."""

    def __init__(self, command: str, feedback_tail: int = 2000) -> None:
        self.command = command
        self._tail = feedback_tail

    def check(self, workspace: Path) -> GateResult:
        proc = subprocess.run(self.command, cwd=workspace, shell=True,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return GateResult(True, None)
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-self._tail:]
        return GateResult(False, tail)


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
