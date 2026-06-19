"""Orchestration — a supervisor over many worker loops (Chapters 10-12). [Part II]

The seam: a Supervisor that dispatches independent slices to isolated worker loops (git
worktrees), with blind fan-out and evolutionary (select + reseed) strategies. v1 ships the
single-agent core; this is the headline of Part II. The interface is fixed here so the core's
`run_loop` becomes the worker body unchanged.
"""
from __future__ import annotations

from typing import Protocol


class Supervisor(Protocol):
    def run_fleet(self, tasks: list[dict]) -> list[object]: ...


def run_fleet(*args, **kwargs):
    raise NotImplementedError(
        "orchestration is Part II (loops curriculum Ch 10-12). v1 ships the single-agent "
        "core; run_loop is the future worker body. Run multiple loops yourself for now."
    )
