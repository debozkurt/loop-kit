"""Continuous review — review every commit, feed findings back (Chapter 8). [Part II]

The seam: a ReviewHook the controller calls after each commit (the marked attach point in
loop.py). v1 ships without it; Part II implements the background reviewer (the roborev
pattern) that reviews each diff and loops fix -> re-review until clean, while the context that
produced the diff is still fresh.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..gate import GateResult


class ReviewHook(Protocol):
    def review(self, workspace: Path, commit_message: str) -> GateResult: ...
