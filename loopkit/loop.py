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

import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import durability, safety, secrets, trace
from .agent import Agent, AgentObserver
from .gate import AlwaysPass, Gate, ReviewUnavailable, ShellGate
from .log import get_logger
from .prompt import build_prompt
from .plan import PlanState, read_plan
from .stops import (BudgetCeiling, LoopState, NoProgress, PlanStall, ReviewStall, StopReason,
                    first_triggered)

if TYPE_CHECKING:
    # Typing-only imports: the core never depends on an extension at runtime — the hook and the
    # registry are duck-called. Both are opt-in (Ch 8, Ch 17), so passing None keeps v1 exact.
    from pathlib import Path
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
    plan_open: int | None = None   # plan-driven backlog: open / total checklist items at halt
    plan_total: int | None = None  # (both None when not in plan mode — exact prior RunResult)


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


def _make_run_id(repo) -> str:
    """A per-run correlation id: the starting state signature + a uuid suffix.

    The state-signature prefix groups a run by the base tree it started from (handy when scanning
    logs). The `uuid4` suffix makes the id UNIQUE per run — two concurrent runs off the *same* base
    commit would otherwise compute the same signature and share an id, intermixing their lines in any
    aggregated log/trace sink (cloud/fleet tier, one LangSmith project) and defeating the whole point
    of a correlation id. `state_signature` itself stays the no-progress oracle elsewhere; this only
    fixes the id used for correlation.
    """
    return f"{durability.state_signature(repo)[:8]}-{uuid.uuid4().hex[:4]}"


class _ActivityWriter:
    """The durable per-run activity artifact: the agent's raw event stream + loopkit tick markers,
    one JSON object per line, written as they happen. A None path is a silent no-op (a single `run`
    writes nothing extra; the batch runner points it next to the journal so it survives the rmtree'd
    worktree). The loop owns the handle and flushes each line, so a *hung* tick still leaves a partial,
    replayable trail — the exact 'why did this tick go sideways' case (docs/OPERATING.md)."""

    def __init__(self, path: "Path | None", run_id: str, branch: str) -> None:
        self.run_id = run_id
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")
            self._mark("run.start", branch=branch)

    def _mark(self, event: str, **fields) -> None:
        """Write a loopkit-authored marker line (namespaced under `loopkit` so it never collides
        with a verbatim agent event, which carries `type`)."""
        if self._fh is not None:
            self._fh.write(json.dumps({"loopkit": event, "run": self.run_id, **fields}) + "\n")
            self._fh.flush()

    def tick(self, i: int) -> None:
        self._mark("tick.start", tick=i)

    @property
    def sink(self):
        """The per-event writer handed to the adapter via `AgentObserver.activity` (None ⇒ no-op)."""
        if self._fh is None:
            return None

        def _write(raw_line: str) -> None:
            self._fh.write(raw_line.rstrip("\n") + "\n")   # verbatim agent event, faithful for replay
            self._fh.flush()

        return _write

    def close(self) -> None:
        if self._fh is not None:
            self._mark("run.done")
            self._fh.close()
            self._fh = None


def run_loop(config, agent: Agent, *, iteration_gate: Gate | None = None,
             acceptance_gate: Gate | None = None, regression_gate: Gate | None = None,
             review_hook: "ReviewHook | None" = None,
             skills: "SkillRegistry | None" = None, dry_run: bool = False,
             trace_metadata: dict | None = None,
             executor: "ToolExecutor | None" = None,
             activity_path: "Path | None" = None) -> RunResult:
    """Drive the agent toward `config.goal` until a terminal is reached. Returns the terminal.

    `trace_metadata` is merged onto the top-level trace span — the fleet worker passes the task id
    here so every run in a fleet is attributable in LangSmith (None = exact prior behavior).

    `executor` is the Phase-6 seam wired into the **default** gates the loop builds from config: a
    `RemoteToolExecutor` makes the held-out gate (agent-authored tests) run in the keyless executor
    sidecar. None ⇒ the in-process `LocalToolExecutor`. The protected-path guard and commit-every-tick
    stay here in loopkit-core (trusted) operating on the shared workspace — only the gate *command* is
    dispatched. An explicitly-passed gate keeps its own executor (the caller's choice).

    `activity_path` turns on the durable per-run activity artifact (the agent's raw event stream +
    tick markers, one JSON object per line). None (single runs) ⇒ off; the batch runner points it at
    `<manifest_dir>/<task>.activity.jsonl` — next to the journal, so it survives the rmtree'd worktree.
    """
    repo = config.repo_path()
    run_id = _make_run_id(repo)             # unique per run (see _make_run_id) — not just the state sig
    log = get_logger("loop", run_id)
    # Durable activity artifact, written as events happen (flushed per line, so a hung tick still
    # leaves a partial trail). None path ⇒ a silent no-op — exact prior behavior.
    activity = _ActivityWriter(activity_path, run_id, config.branch)

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
    # Review-loop bound (Ch 8): a reject loop bills agent + judge every tick and NoProgress can't
    # see it (the agent keeps editing → the signature keeps changing). Only when a hook is wired,
    # so off review the stop set is byte-identical to before.
    if review_hook is not None:
        hard_stops.append(ReviewStall(config.stops.review_stall_after))
    # Plan-driven backlog: NoProgress watches the git signature, but an agent stuck on one item still
    # edits files each tick (signature changes) so it never fires — the run would grind to the cap on
    # a wedged item, spending the whole budget. Add a stall stop that watches the done-count instead.
    # Plan-mode-only, so off it the stop set is byte-identical to the single-task loop's.
    if config.plan.file:
        hard_stops.append(PlanStall(config.stops.plan_stall_after))

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
        plan_file = config.plan.file            # plan-driven backlog mode; None = single-task (prior)
        last_plan: PlanState | None = None
        plan_dones: list[int] = []              # done-count history for the PlanStall stop (Ch 13)
        # Sticky review verdict, keyed by HEAD (Ch 8). A verdict outlives its tick: a REJECTed HEAD
        # stays rejected on idle ticks WITHOUT re-billing the judge, and an APPROVEd HEAD is never
        # re-reviewed. This closes the reject-then-idle hole (reject at tick N, commit nothing at
        # tick N+1 → the old per-tick reset let the gates run and DONE ship the rejected diff), and
        # it subsumes the old `advanced` condition: "only review new work" falls out of the key.
        review_head: str | None = None          # last HEAD a fresh verdict was rendered for
        review_last_ok = True                   # that verdict
        review_last_feedback: str | None = None  # its feedback, re-fed on sticky-rejected ticks
        review_rejects = 0                      # consecutive fresh rejections (ReviewStall input)

        def _res(reason, iterations, cost_usd, **kw):
            # Stamp the latest checklist progress onto every terminal (None when not in plan mode).
            po = last_plan.open if last_plan else None
            pt = last_plan.total if last_plan else None
            return RunResult(reason, iterations, cost_usd, plan_open=po, plan_total=pt, **kw)

        def _terminal(result):
            # Every terminal funnels here: finalize the durable artifact, then stamp the trace span.
            activity.close()
            return _finish(run_span, result)

        for i in range(1, config.stops.max_iter + 1):
            tick = log.bind(tick=i)
            activity.tick(i)                               # delimit this tick's events in the artifact
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
                        # Observer sinks: the tick logger for the live step stream, the activity writer
                        # for the durable artifact. A streaming adapter feeds both; others ignore them.
                        observer = AgentObserver(log=tick, activity=activity.sink)
                        result = (_DryResult() if dry_run
                                  else agent.act(prompt, repo, observer=observer))
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
                    return _terminal(_res(
                        StopReason.SAFETY, i, cost, detail=f"touched protected path {violations[0]}"))

                commit_msg = f"loopkit: tick {i} on {config.branch}"
                committed = durability.commit_progress(repo, commit_msg)
                # (Self-committing CLI agents make loopkit's commit a no-op while HEAD still moves;
                # the review below keys on HEAD itself — sticky verdict — so either commit path, or
                # a HEAD that moved ticks ago while the gate was red, is reviewed exactly once.)
                signature = durability.state_signature(repo)
                signatures.append(signature)
                tick.info("tick.commit", committed=committed, sig=signature)
                tick_span.metadata(committed=committed, cost_usd=round(cost, 6))
                # Plan-driven backlog (Ch 4-5): read the checklist fresh each tick — it is the agent's
                # durable working memory AND the loop's completion signal below. None-safe: no [plan].
                if plan_file:
                    last_plan = read_plan(repo, plan_file)
                    plan_dones.append(last_plan.done)   # feeds the PlanStall stop below
                    tick.info("plan.progress", done=last_plan.done, open=last_plan.open,
                              total=last_plan.total)
                    tick_span.metadata(plan_done=last_plan.done, plan_open=last_plan.open)
                # Iteration gate FIRST — free and deterministic. The (billed, nondeterministic)
                # review runs only behind a green gate, so no judge tokens are ever spent on a
                # draft that doesn't even pass its own checks (the review.sh doctrine: an LLM
                # verdict is a model call — gate it behind the mechanical check).
                with trace.span("iteration gate", run_type="tool",
                                inputs={"command": config.gate.iteration}) as gate_span:
                    with _heartbeat(tick, "iteration_gate"):
                        gate = iteration_gate.check(repo)
                    gate_span.outputs(passed=gate.passed, feedback=gate.feedback or None)
                tick.info("gate.iteration", passed=gate.passed)

                # Continuous review (Ch 8): a clean review of the CURRENT HEAD is a precondition
                # for everything downstream (acceptance, DONE); a failing one feeds back so the
                # agent fixes it while the producing context is fresh (roborev loop). Verdicts are
                # STICKY per HEAD: an unchanged HEAD re-uses its verdict for free — so a rejected
                # commit stays rejected on idle ticks (no re-bill, no reject-then-idle escape) and
                # an approved HEAD is never re-reviewed. Infra failure is not a verdict: a judge
                # that cannot decide (ReviewUnavailable) halts the run rather than feeding "the
                # judge is broken" to the agent as a phantom defect. None hook => v1 exactly.
                review_ok = True
                if review_hook is not None and gate.passed:
                    head_now = durability.current_head(repo)
                    if head_now == review_head:
                        review_ok = review_last_ok
                        if not review_ok:
                            tick.info("gate.review", passed=False, sticky=True)
                            feedback = review_last_feedback
                    else:
                        # The judge's identity rides the span (the built-in hook exposes .target;
                        # shell/callable hooks have none) — so a trace answers "which model judged
                        # this?" without grepping config.
                        target = getattr(review_hook, "target", None)
                        review_meta = ({"backend": target.backend, "model": target.model}
                                       if target is not None else None)
                        with trace.span("review", run_type="tool",
                                        metadata=review_meta) as review_span:
                            try:
                                with _heartbeat(tick, "review"):
                                    review = review_hook.review(repo, commit_msg)
                            except ReviewUnavailable as exc:
                                detail = secrets.redact(str(exc))
                                tick.error("gate.review.unavailable", detail=detail[:200])
                                review_span.outputs(unavailable=detail[:400])
                                tick_span.outputs(halt="review_unavailable")
                                return _terminal(_res(StopReason.REVIEW_UNAVAILABLE, i, cost,
                                                      detail=detail[:400]))
                            review_cost = float(getattr(review, "cost_usd", 0.0) or 0.0)
                            review_span.outputs(passed=review.passed,
                                                feedback=review.feedback or None)
                            review_span.metadata(cost_usd=round(review_cost, 6))
                        # Judge spend counts: fold it into the run cost NOW so the budget ceiling
                        # (checked below, on state built after this) sees it the same tick.
                        cost += review_cost
                        review_head = head_now
                        review_last_ok = review.passed
                        tick.info("gate.review", passed=review.passed,
                                  costUsd=round(review_cost, 4))
                        if review.passed:
                            review_rejects = 0
                            review_last_feedback = None
                        else:
                            review_ok = False
                            review_rejects += 1
                            reason_full = secrets.redact(review.feedback or "")
                            # Persist WHY review rejected, not just the pass/fail bit — otherwise
                            # the judge's verdict is lost the moment it feeds back, and "did review
                            # catch the real problem?" is unauditable after the run. Single-lined +
                            # tail-trimmed (the verdict sits at the end of the judge output); the
                            # full text still rides to the agent via `feedback` and to the span.
                            tick.info("gate.review.rejected",
                                      reason=" ".join(reason_full.split())[-400:]
                                             or "(no detail returned)")
                            feedback = ("A review of your last change found issues to fix before "
                                        "it can be accepted:\n" + reason_full)
                            review_last_feedback = feedback

                # DONE next: the held-out acceptance gate (Ch 9), behind BOTH the green gate and a
                # clean review — unreviewed-but-green work can never be declared done.
                # Plan-driven backlog: with open checklist items the run is not finished, so skip
                # the (expensive) whole-project acceptance gate and keep going — one item a tick.
                plan_blocks = last_plan is not None and last_plan.blocks_done
                if review_ok:
                    if gate.passed and not plan_blocks:
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
                                done = _res(StopReason.DONE, i, cost)
                                # Write edge of the flywheel (Ch 17): distil this success into a skill —
                                # but only through the registry's gate, so a thin win can't mint a lesson.
                                if skills is not None:
                                    minted = skills.write_back(done, repo, config.goal)
                                    tick.info("skill.write_back", minted=minted is not None,
                                              name=minted.name if minted else "-")
                                tick_span.outputs(result="done")
                                return _terminal(done)
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
                    elif gate.passed:
                        # Plan mode: the tick's item is verified, but the checklist is not finished.
                        tick.info("plan.incomplete", open=last_plan.open, total=last_plan.total)
                        feedback = (f"The iteration gate passes and nothing is broken, but "
                                    f"{last_plan.open} of {last_plan.total} checklist items in "
                                    f"{plan_file} are still open. Mark the item you just finished "
                                    f"`- [x]` if you haven't, then do the single most important "
                                    f"remaining one — the run is not done until every item is checked "
                                    f"and the acceptance gate passes.")
                    else:
                        feedback = secrets.redact(gate.feedback)

                # Snapshot AFTER review, so the budget ceiling sees this tick's judge spend and the
                # review-stall stop sees this tick's rejection — building it earlier (as before the
                # reorder) hid both until the following tick.
                state = LoopState(iteration=i, cost_usd=cost, signature=signature,
                                  signatures=signatures,
                                  plan_dones=plan_dones if plan_file else None,
                                  review_rejects=review_rejects)

                # Hard stops, in precedence order (budget > no-progress > review-stall > plan-stall).
                # Cap is the loop bound below.
                reason = first_triggered(hard_stops, state)
                if reason is not None:
                    detail = ""
                    if reason is StopReason.PLAN_STALL and last_plan is not None:
                        # Stuck, not progressing — say so, so a human (or a future NEEDS_HUMAN
                        # escalation) knows this halted on a wedged item, not a finished backlog.
                        detail = (f"no checklist item completed in "
                                  f"{config.stops.plan_stall_after} ticks — {last_plan.open} of "
                                  f"{last_plan.total} items still open")
                    tick.warn("loop.halt", reason=reason.value, iterations=i, costUsd=round(cost, 4))
                    tick_span.outputs(halt=reason.value)
                    return _terminal(_res(reason, i, cost, overfit=overfit, detail=detail))
                # Not a terminal: record why this tick continues (the feedback the next tick gets),
                # so a tick is never a blank row in the trace UI.
                tick_span.outputs(result="continue", feedback=feedback or None)

        log.warn("loop.halt", reason=StopReason.ITERATION_CAP.value,
                 iterations=config.stops.max_iter, costUsd=round(cost, 4))
        return _terminal(_res(StopReason.ITERATION_CAP, config.stops.max_iter, cost,
                              overfit=overfit))
