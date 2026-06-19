"""Skills and the write-back flywheel (Chapter 17). [Part II]

The seam: a SkillRegistry the prompt draws from, plus a write-back edge that distills a
successful run into a reusable, named skill — gated, never ungated (Ch 17 / 19). v1 runs
without skills; Part II adds the flywheel. Attach points in the core: prompt assembly (read)
and the after-DONE path in run_loop (write-back).
"""
from __future__ import annotations

from typing import Protocol


class SkillRegistry(Protocol):
    def render(self) -> str: ...                 # skills injected into the prompt
    def write_back(self, run_result) -> None: ...  # distill a successful run into a skill


def default_registry():
    raise NotImplementedError("skills and the write-back flywheel are Part II (Ch 17).")
