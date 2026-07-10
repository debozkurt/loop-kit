"""The hard stops — most of loop engineering is making the loop halt (Chapters 13-14).

A loop has one good terminal (DONE, decided by the acceptance gate) and several bad ones. The
bad ones are evaluated every tick in a fixed precedence so a runaway is impossible:

    BUDGET_CEILING  >  NO_PROGRESS  >  PLAN_STALL  >  ITERATION_CAP

Budget wins because money already spent can't be unspent; no-progress beats the cap because
detecting a stuck loop early is cheaper than waiting out the cap. PLAN_STALL is the plan-mode
counterpart of NO_PROGRESS — it watches the *checklist* rather than the git tree, and is only in
the policy list when plan-driven backlog mode is on. SAFETY is a separate terminal raised by the
protected-path guard (Ch 16). Each stop is a small policy object, so adding another (a wall-clock
deadline, say) is a one-liner.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class StopReason(str, Enum):
    DONE = "done"                       # the held-out acceptance gate passed
    BUDGET_CEILING = "budget_ceiling"
    NO_PROGRESS = "no_progress"
    PLAN_STALL = "plan_stall"           # plan mode: the checklist stopped advancing (Ch 13)
    ITERATION_CAP = "iteration_cap"
    SAFETY = "safety_halt"              # touched a protected path (Ch 16)


@dataclass
class LoopState:
    """Everything a stop policy needs to decide, snapshotted at the end of a tick."""

    iteration: int
    cost_usd: float
    signature: str                     # state_signature this tick (Ch 15)
    signatures: list[str]              # signature history, oldest -> newest
    plan_dones: list[int] | None = None  # plan mode: done-count history, oldest -> newest (None off)


class StopPolicy(Protocol):
    reason: StopReason

    def triggered(self, state: LoopState) -> bool: ...


class BudgetCeiling:
    reason = StopReason.BUDGET_CEILING

    def __init__(self, max_cost_usd: float) -> None:
        self.max_cost_usd = max_cost_usd

    def triggered(self, state: LoopState) -> bool:
        return state.cost_usd >= self.max_cost_usd


class NoProgress:
    reason = StopReason.NO_PROGRESS

    def __init__(self, window: int) -> None:
        self.window = window

    def triggered(self, state: LoopState) -> bool:
        # Stuck = the last `window`+1 signatures are all identical (no state change).
        if len(state.signatures) <= self.window:
            return False
        recent = state.signatures[-(self.window + 1):]
        return len(set(recent)) == 1


class PlanStall:
    """Plan-driven backlog: halt when the *checklist* stops advancing (the plan-mode NoProgress).

    NoProgress watches the git state signature, but a plan-mode agent stuck on one item still edits
    files every tick — the signature changes, so NoProgress can never fire and the run grinds all
    the way to the iteration cap (60 ticks in the scaffold), spending the whole budget on one wedged
    item. This watches the *done-count* instead: if the number of `- [x]` items has not increased
    across the last `window`+1 ticks, the loop is churning without completing anything, so halt —
    the same "detect a stuck loop early" rationale as NoProgress, on the signal that actually moves
    in plan mode. `<=` (not `==`) so a regression (an item getting *un*checked) also counts as stalled.
    """

    reason = StopReason.PLAN_STALL

    def __init__(self, window: int) -> None:
        self.window = window

    def triggered(self, state: LoopState) -> bool:
        dones = state.plan_dones
        # Off plan mode (None) or too early to judge: never fire. Needs window+1 samples so the
        # agent gets `window` no-progress ticks after any completion before we call it stuck.
        if dones is None or len(dones) <= self.window:
            return False
        recent = dones[-(self.window + 1):]
        return recent[-1] <= recent[0]     # no net item completed across the window (or regressed)


class IterationCap:
    reason = StopReason.ITERATION_CAP

    def __init__(self, max_iter: int) -> None:
        self.max_iter = max_iter

    def triggered(self, state: LoopState) -> bool:
        return state.iteration >= self.max_iter


def first_triggered(policies: list[StopPolicy], state: LoopState) -> StopReason | None:
    """Return the reason of the first policy that fires, in the order given (= precedence)."""
    for policy in policies:
        if policy.triggered(state):
            return policy.reason
    return None
