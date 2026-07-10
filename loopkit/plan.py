"""Plan-driven backlog mode (shape #2): drive one loop through a markdown checklist, item by item.

A plan file — GitHub-style task items, `- [ ]` open and `- [x]` done — is BOTH a prompt anchor the
agent reads and maintains AND the loop's completion signal: the run is not DONE while any item is
still open, whatever the gates say. This closes the gap between "one task, iterate to done" and a
whole backlog worked through in a single loop, applying the Ch 4-5 fresh-context discipline across
many items (the plan file is the durable working memory, not a growing conversation).

None-safe by construction: with no `[plan]` file configured the loop never calls in here and behaves
exactly as the single-task loop always has. A plan file that is missing or has no checkbox items
reports nothing to track, so completion falls back to the gates alone — the loop never blocks forever
on a plan that isn't really there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A task item at line start: any list bullet, a checkbox, allowing indentation for nested sub-items.
_OPEN = re.compile(r"^\s*[-*+]\s+\[ \]", re.MULTILINE)
_DONE = re.compile(r"^\s*[-*+]\s+\[[xX]\]", re.MULTILINE)


@dataclass(frozen=True)
class PlanState:
    """How much of the checklist is left, read fresh from disk each tick."""

    open: int
    done: int

    @property
    def total(self) -> int:
        return self.open + self.done

    @property
    def blocks_done(self) -> bool:
        """True while the run must keep going — there are items and at least one is still open.

        An empty or absent plan (total 0) does NOT block: with nothing to track, the gates decide
        DONE on their own, so a stray `[plan]` file can never wedge the loop open forever."""
        return self.open > 0


def read_plan(repo: Path, plan_file: str) -> PlanState:
    """Count open (`- [ ]`) vs done (`- [x]`) checklist items in `repo/plan_file`.

    A missing file, or one with no checkbox items, returns `PlanState(0, 0)` — 'nothing to track'."""
    path = repo / plan_file
    if not path.is_file():
        return PlanState(open=0, done=0)
    text = path.read_text(encoding="utf-8", errors="replace")
    return PlanState(open=len(_OPEN.findall(text)), done=len(_DONE.findall(text)))
