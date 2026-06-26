"""The loop controller — the tick lifecycle that wires every other module (the spine).

One tick:  prime a fresh context -> invoke the agent -> guard protected paths -> commit ->
run the iteration gate -> on pass, run the held-out acceptance gate before declaring DONE ->
otherwise check the hard stops. The control flow below is the whole single-agent course; every
other module is a swappable part it calls.

Terminal precedence:  DONE  >  SAFETY  >  BUDGET_CEILING  >  NO_PROGRESS  >  ITERATION_CAP.
DONE short-circuits because finished work is the best outcome even on the tick that crosses a
limit; otherwise the bad stops apply in the order the course specifies.

Observability is two-layered: payload-free `[loopkit][loop]` logs on every event (always on), and
a full LangSmith trace tree when enabled (`trace.py`) — run -> tick -> agent -> llm/tool -> gates,
with cost/usage/model metadata on each span. The trace spans nest by lexical scope here; the API
adapter's `llm`/`tool` spans nest under the `agent` span automatically via LangSmith contextvars.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import durability, safety, secrets, trace
from .agent import Agent
from .gate import AlwaysPass, Gate, ShellGate
from .log import get_logger
from .prompt import build_prompt
from .stops import BudgetCeiling, LoopState, NoProgress, StopReason, first_triggered

if TYPE_CHECKING:
    # Typing-only imports: the core never depends on an extension at runtime — the hook and the
    # registry are duck-called. Both are opt-in (Ch 8, Ch 17), so passing None keeps v1 exact.
    from .executor import ToolExecutor
    from .extensions.review import ReviewHook
    from .extensions.skills import SkillRegistry


_HEARTBEAT_INTERVAL = 20.0          # seconds between liveness pings during a long, silent phase


@contextmanager
def _heartbeat(log, phase: str, interval: float = _HEARTBEAT_INTERVAL):
    """Emit a periodic liveness log while a blocking phase runs.

    The agent call and the gates are captured subprocesses that can run for *minutes* with no output —
    so a perfectly healthy run looks hung from the terminal. This pings `tick.progress phase=…
    elapsedSec=…` every `interval` seconds until the phase returns. It fires only *past* the interval,
    so fast ticks (mock/tests, sub-second) stay completely silent — no log noise. stdlib-only; the
    worker is a daemon thread joined on exit, so it never outlives the run."""
    stop = threading.Event()

    def beat() -> None:
        start = time.monotonic()
        while not stop.wait(interval):
            log.info("tick.progress", phase=phase, elapsedSec=round(time.monotonic() - start))

    worker = threading.Thread(target=beat, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1.0)


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


def _finish(run_span, result: RunResult) -> RunResult:
    """Record the run's terminal outputs on the top-level trace span and return the result."""
    run_span.outputs(terminal=result.reason.value, iterations=result.iterations,
                     cost_usd=round(result.cost_usd, 6), overfit=result.overfit,
                     detail=result.detail or None)
    return result


def run_loop(config, agent: Agent, *, iteration_gate: Gate | None = None,
             acceptance_gate: Gate | None = None, regression_gate: Gate | None = None,
             review_hook: "ReviewHook | None" = None,
             skills: "SkillRegistry | None" = None, dry_run: bool = False,
             trace_metadata: dict | None = None,
             executor: "ToolExecutor | None" = None) -> RunResult:
    """Drive the agent toward `config.goal` until a terminal is reached. Returns the terminal.

    `trace_metadata` is merged onto the top-level trace span — the fleet worker passes the task id
    here so every run in a fleet is attributable in LangSmith (None = exact prior behavior).

    `executor` is the Phase-6 seam wired into the **default** gates the loop builds from config: a
    `RemoteToolExecutor` makes the held-out gate (agent-authored tests) run in the keyless executor
    sidecar. None ⇒ the in-process `LocalToolExecutor`. The protected-path guard and commit-every-tick
    stay here in loopkit-core (trusted) operating on the shared workspace — only the gate *command* is
    dispatched. An explicitly-passed gate keeps its own executor (the caller's choice).
    """
    repo = config.repo_path()
    run_id = durability.state_signature(repo)[:8]
    log = get_logger("loop", run_id)

    iteration_gate = iteration_gate or ShellGate(config.gate.iteration, executor=executor)
    acceptance_gate = acceptance_gate or (
        ShellGate(config.gate.acceptance, executor=executor) if config.gate.acceptance else AlwaysPass()
    )
    # The second oracle (Ch 9, two-oracle): held-out PASS_TO_PASS. None / unconfigured ⇒ AlwaysPass,
    # so DONE is certified by acceptance alone — exact prior behavior.
    regression_gate = regression_gate or (
        ShellGate(config.gate.regression, executor=executor) if config.gate.regression else AlwaysPass()
    )
    # Per-tick hard stops, in precedence order. The iteration cap is the loop's own bound.
    hard_stops = [BudgetCeiling(config.agent.max_cost_usd),
                  NoProgress(config.stops.no_progress_after)]

    durability.ensure_branch(repo, config.branch)
    log.info("run.start", goalLen=len(config.goal), branch=config.branch,
             adapter=config.agent.adapter, maxIter=config.stops.max_iter,
             budgetUsd=config.agent.max_cost_usd, dryRun=dry_run)

    run_meta = {"run_id": run_id, "adapter": config.agent.adapter, "model": config.agent.model,
                "budget_usd": config.agent.max_cost_usd, "max_iter": config.stops.max_iter,
                "dry_run": dry_run}
    if trace_metadata:
        run_meta.update(trace_metadata)

    with trace.span("loopkit run", run_type="chain", tags=["loopkit"],
                    inputs={"goal": config.goal, "repo": str(repo), "branch": config.branch},
                    metadata=run_meta) as run_span:
        cost = 0.0
        signatures = [durability.state_signature(repo)]
        feedback: str | None = None
        overfit = False

        for i in range(1, config.stops.max_iter + 1):
            tick = log.bind(tick=i)
            with trace.span(f"tick {i}", run_type="chain", metadata={"tick": i}) as tick_span:
                # Read edge of the flywheel (Ch 17): render learned skills into this tick's prompt.
                # None registry -> no block -> v1 prompt exactly.
                skills_text = skills.render() if skills is not None else None
                prompt = build_prompt(config, feedback, skills_text)
                tick.info("agent.invoke", promptLen=len(prompt), skillsLen=len(skills_text or ""))

                # The agent span: the API adapter's llm/tool spans nest under it via contextvars,
                # so a Claude/OpenAI tick shows every model call and tool call here; a CLI adapter
                # shows just its parsed cost. Same span for every adapter — the contract is uniform.
                with trace.span("agent", run_type="chain", inputs={"prompt": prompt},
                                metadata={"adapter": config.agent.adapter,
                                          "model": config.agent.model}) as agent_span:
                    with _heartbeat(tick, "agent"):       # liveness pings during the silent agent call
                        result = _DryResult() if dry_run else agent.act(prompt, repo)
                    agent_span.outputs(ok=result.ok, summary=result.summary,
                                       tail=result.raw_tail or None)
                    agent_span.metadata(cost_usd=round(result.cost_usd, 6))
                cost += result.cost_usd
                tick.info("agent.done", ok=result.ok, costUsd=round(result.cost_usd, 4),
                          summary=result.summary)

                # Safety: the loop must never touch a protected path (Ch 9 + 16). Check before commit.
                violations = safety.protected_violations(config)
                if violations:
                    tick.error("safety.protected_path_touched", count=len(violations),
                               first=violations[0])
                    durability.revert_uncommitted(repo)
                    tick_span.outputs(halt="safety", protected_path=violations[0])
                    return _finish(run_span, RunResult(
                        StopReason.SAFETY, i, cost, detail=f"touched protected path {violations[0]}"))

                commit_msg = f"loopkit: tick {i} on {config.branch}"
                committed = durability.commit_progress(repo, commit_msg)
                signature = durability.state_signature(repo)
                signatures.append(signature)
                tick.info("tick.commit", committed=committed, sig=signature)
                tick_span.metadata(committed=committed, cost_usd=round(cost, 6))
                state = LoopState(iteration=i, cost_usd=cost, signature=signature,
                                  signatures=signatures)

                # Continuous review (Ch 8): review the fresh commit before it can count as done. A
                # clean review is a precondition for the done-check below; a failing one feeds back so
                # the agent fixes it next tick, while the producing context is fresh (roborev loop).
                # Only on a real commit — no new diff means nothing new to review. None => v1.
                review_ok = True
                if review_hook is not None and committed:
                    with trace.span("review", run_type="tool") as review_span:
                        review = review_hook.review(repo, commit_msg)
                        review_span.outputs(passed=review.passed, feedback=review.feedback or None)
                    tick.info("gate.review", passed=review.passed)
                    if not review.passed:
                        review_ok = False
                        feedback = ("A review of your last change found issues to fix before it can "
                                    "be accepted:\n" + secrets.redact(review.feedback or ""))

                # DONE next: the iteration gate, then the held-out acceptance gate (Ch 9). Skipped
                # when the review failed, so unreviewed-but-green work can never be declared done.
                if review_ok:
                    with trace.span("iteration gate", run_type="tool",
                                    inputs={"command": config.gate.iteration}) as gate_span:
                        with _heartbeat(tick, "iteration_gate"):
                            gate = iteration_gate.check(repo)
                        gate_span.outputs(passed=gate.passed, feedback=gate.feedback or None)
                    tick.info("gate.iteration", passed=gate.passed)
                    if gate.passed:
                        with trace.span("acceptance gate", run_type="tool",
                                        inputs={"command": config.gate.acceptance}) as acc_span:
                            with _heartbeat(tick, "acceptance_gate"):
                                acc = acceptance_gate.check(repo)
                            acc_span.outputs(passed=acc.passed, feedback=acc.feedback or None)
                        tick.info("gate.acceptance", passed=acc.passed)
                        if acc.passed:
                            # Second oracle (Ch 9): the fix works AND previously-passing behavior is
                            # preserved. AlwaysPass when no regression gate is configured (prior behavior).
                            with trace.span("regression gate", run_type="tool",
                                            inputs={"command": config.gate.regression}) as reg_span:
                                with _heartbeat(tick, "regression_gate"):
                                    reg = regression_gate.check(repo)
                                reg_span.outputs(passed=reg.passed, feedback=reg.feedback or None)
                            tick.info("gate.regression", passed=reg.passed)
                            if reg.passed:
                                tick.info("run.done", iterations=i, costUsd=round(cost, 4))
                                done = RunResult(StopReason.DONE, i, cost)
                                # Write edge of the flywheel (Ch 17): distil this success into a skill —
                                # but only through the registry's gate, so a thin win can't mint a lesson.
                                if skills is not None:
                                    minted = skills.write_back(done, repo, config.goal)
                                    tick.info("skill.write_back", minted=minted is not None,
                                              name=minted.name if minted else "-")
                                tick_span.outputs(result="done")
                                return _finish(run_span, done)
                            # Regression: the target is fixed but previously-passing behavior broke.
                            tick.warn("gate.regression_failed", detail="acceptance_pass_regression_fail")
                            feedback = ("The held-out acceptance check passes, but a regression check "
                                        "shows your change broke previously-passing behavior. Fix the "
                                        "goal WITHOUT regressing existing behavior.\n"
                                        + secrets.redact(reg.feedback or ""))
                        else:
                            # Overfit: passed what it saw, failed what it didn't. Feed that back (Ch 9).
                            overfit = True
                            tick.warn("gate.overfit", detail="iteration_pass_acceptance_fail")
                            feedback = ("The visible checks pass but the held-out acceptance checks "
                                        "fail: you have fit the visible tests, not solved the goal. Make "
                                        "the behaviour correct.\n" + secrets.redact(acc.feedback or ""))
                    else:
                        feedback = secrets.redact(gate.feedback)

                # Hard stops, in precedence order (budget > no-progress). Cap is the loop bound below.
                reason = first_triggered(hard_stops, state)
                if reason is not None:
                    tick.warn("loop.halt", reason=reason.value, iterations=i, costUsd=round(cost, 4))
                    tick_span.outputs(halt=reason.value)
                    return _finish(run_span, RunResult(reason, i, cost, overfit=overfit))

        log.warn("loop.halt", reason=StopReason.ITERATION_CAP.value,
                 iterations=config.stops.max_iter, costUsd=round(cost, 4))
        return _finish(run_span, RunResult(StopReason.ITERATION_CAP, config.stops.max_iter, cost,
                                           overfit=overfit))
