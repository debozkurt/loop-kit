"""The loop controller — the tick lifecycle that wires every other module (the spine).

One tick:  prime a fresh context -> invoke the agent -> guard protected paths -> commit ->
run the iteration gate -> on pass, run the held-out acceptance gate before declaring DONE ->
otherwise check the hard stops. The control flow below is the whole single-agent course; every
other module is a swappable part it calls.

Terminal precedence:  DONE  >  SAFETY  >  BUDGET_CEILING  >  NO_PROGRESS  >  ITERATION_CAP.
DONE short-circuits because finished work is the best outcome even on the tick that crosses a
limit; otherwise the bad stops apply in the order the course specifies.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import durability, safety
from .agent import Agent
from .gate import AlwaysPass, Gate, ShellGate
from .log import get_logger
from .prompt import build_prompt
from .stops import BudgetCeiling, LoopState, NoProgress, StopReason, first_triggered


@dataclass
class RunResult:
    reason: StopReason
    iterations: int
    cost_usd: float
    overfit: bool = False          # iteration gate passed but acceptance gate did not, at halt
    detail: str = ""


class _DryResult:
    """A no-op agent result for dry runs — exercises the control flow without spending."""

    ok = True
    cost_usd = 0.0
    summary = "dry_run"
    raw_tail = ""


def run_loop(config, agent: Agent, *, iteration_gate: Gate | None = None,
             acceptance_gate: Gate | None = None, dry_run: bool = False) -> RunResult:
    """Drive the agent toward `config.goal` until a terminal is reached. Returns the terminal."""
    repo = config.repo_path()
    run_id = durability.state_signature(repo)[:8]
    log = get_logger("loop", run_id)

    iteration_gate = iteration_gate or ShellGate(config.gate.iteration)
    acceptance_gate = acceptance_gate or (
        ShellGate(config.gate.acceptance) if config.gate.acceptance else AlwaysPass()
    )
    # Per-tick hard stops, in precedence order. The iteration cap is the loop's own bound.
    hard_stops = [BudgetCeiling(config.agent.max_cost_usd),
                  NoProgress(config.stops.no_progress_after)]

    durability.ensure_branch(repo, config.branch)
    log.info("run.start", goalLen=len(config.goal), branch=config.branch,
             adapter=config.agent.adapter, maxIter=config.stops.max_iter,
             budgetUsd=config.agent.max_cost_usd, dryRun=dry_run)

    cost = 0.0
    signatures = [durability.state_signature(repo)]
    feedback: str | None = None
    overfit = False

    for i in range(1, config.stops.max_iter + 1):
        tick = log.bind(tick=i)

        prompt = build_prompt(config, feedback)
        tick.info("agent.invoke", promptLen=len(prompt))
        result = _DryResult() if dry_run else agent.act(prompt, repo)
        cost += result.cost_usd
        tick.info("agent.done", ok=result.ok, costUsd=round(result.cost_usd, 4),
                  summary=result.summary)

        # Safety: the loop must never touch a protected path (Ch 9 + 16). Check before commit.
        violations = safety.protected_violations(config)
        if violations:
            tick.error("safety.protected_path_touched", count=len(violations), first=violations[0])
            durability.revert_uncommitted(repo)
            return RunResult(StopReason.SAFETY, i, cost,
                             detail=f"touched protected path {violations[0]}")

        committed = durability.commit_progress(repo, f"loopkit: tick {i} on {config.branch}")
        signature = durability.state_signature(repo)
        signatures.append(signature)
        tick.info("tick.commit", committed=committed, sig=signature)
        state = LoopState(iteration=i, cost_usd=cost, signature=signature, signatures=signatures)

        # DONE first: the iteration gate, then the held-out acceptance gate (Ch 9).
        gate = iteration_gate.check(repo)
        tick.info("gate.iteration", passed=gate.passed)
        if gate.passed:
            acc = acceptance_gate.check(repo)
            tick.info("gate.acceptance", passed=acc.passed)
            if acc.passed:
                tick.info("run.done", iterations=i, costUsd=round(cost, 4))
                return RunResult(StopReason.DONE, i, cost)
            # Overfit: passed what it saw, failed what it didn't. Feed that back (Ch 9).
            overfit = True
            tick.warn("gate.overfit", detail="iteration_pass_acceptance_fail")
            feedback = ("The visible checks pass but the held-out acceptance checks fail: you "
                        "have fit the visible tests, not solved the goal. Make the behaviour "
                        "correct.\n" + (acc.feedback or ""))
        else:
            feedback = gate.feedback

        # Hard stops, in precedence order (budget > no-progress). Cap is the loop bound below.
        reason = first_triggered(hard_stops, state)
        if reason is not None:
            tick.warn("loop.halt", reason=reason.value, iterations=i, costUsd=round(cost, 4))
            return RunResult(reason, i, cost, overfit=overfit)

    log.warn("loop.halt", reason=StopReason.ITERATION_CAP.value,
             iterations=config.stops.max_iter, costUsd=round(cost, 4))
    return RunResult(StopReason.ITERATION_CAP, config.stops.max_iter, cost, overfit=overfit)
