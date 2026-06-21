"""Continuous review — review every commit, feed findings back (Chapter 8). [Part II]

The seam: a `ReviewHook` the controller calls after each commit (the `after_commit` attach
point in `loop.run_loop`). A clean review is a *precondition for done* — passing the iteration
and acceptance gates is necessary but doesn't make a diff mergeable. A failing review feeds its
findings straight into the next tick's prompt, so the agent fixes the problem while the context
that produced the diff is still fresh: the roborev fix -> re-review loop.

Why a separate hook rather than another gate: gates answer "is the goal met?" against checks
the loop optimizes. Review answers "is this change *good*?" — style, leftover debug, security,
obvious smells — the things green tests don't encode. Keeping them distinct means a run can be
goal-complete yet still be held back by review, which is exactly the roborev discipline.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Protocol

from .. import secrets
from ..gate import GateResult


class ReviewHook(Protocol):
    def review(self, workspace: Path, commit_message: str) -> GateResult: ...


class CallableReviewHook:
    """Wrap a Python predicate as a review hook — used in tests and Python-native reviews.

    The predicate inspects the committed workspace and returns True for a clean review. On a
    failed review the fixed `feedback` is what the loop carries into the next tick — so write it
    as an instruction the agent can act on, not just a verdict.
    """

    def __init__(self, fn: Callable[[Path], bool], feedback: str = "review found issues") -> None:
        self._fn = fn
        self._feedback = feedback

    def review(self, workspace: Path, commit_message: str) -> GateResult:
        ok = bool(self._fn(workspace))
        return GateResult(ok, None if ok else self._feedback)


class ShellReviewHook:
    """Run a review command in the workspace; exit 0 is a clean review, else fail with output.

    The production path: point it at any reviewer that exits non-zero on problems — a linter, a
    static analyzer, or a coding agent invoked headless (e.g. `claude -p "review the staged
    diff; exit non-zero with findings if anything is wrong"`). The command runs with the just-
    committed change as HEAD, so it can inspect `git diff HEAD~1..HEAD` to review only the delta.
    """

    def __init__(self, command: str, feedback_tail: int = 2000) -> None:
        self.command = command
        self._tail = feedback_tail

    def review(self, workspace: Path, commit_message: str) -> GateResult:
        # Match ShellGate: scrub credentials (the reviewer may run agent-authored code) and keep a
        # python reviewer from littering __pycache__ into a protected path.
        env = {**secrets.current().child_env(), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(self.command, cwd=workspace, shell=True, env=env,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return GateResult(True, None)
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-self._tail:]
        return GateResult(False, f"review command reported problems:\n{tail}")
