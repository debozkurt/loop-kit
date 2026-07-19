"""The no-infra parallel batch — a manifest of independent tasks, one command, no Redis. [Part III]

The three shapes of pointing loopkit at work are one task (`run`), a sequential backlog (`--plan`),
and a parallel batch. Until now the parallel shape needed either the Redis fleet (separately started
workers) or the `run_fleet` Python API. This module is the promised middle: a **TOML tasks manifest**
drives the *same* queue/worker machinery entirely in-process (`InMemoryQueue` + `run_workers`), one
isolated clone + one branch + (optionally) one draft PR per task.

What it adds over blind fan-out is **conflict-aware scheduling** — batches of related fixes are
rarely fully independent, and the two ways they collide get one declarative field each:

- ``group = "name"`` — tasks sharing a group run **serially, in manifest order**. Use it when tasks
  are predicted to touch the same files (their PRs would conflict) or share an external resource
  (a test database) — mutual exclusion + ordering, *not* a success dependency: a failed group
  member does not stop the members after it.
- ``after = ["id", ...]`` — explicit dependency edges. Use them when one task's change must land
  before another's is even attempted (e.g. an expand-then-contract pair: the provider forwards a
  key before the consumer requires it). A task whose dependency does not reach DONE is **skipped**,
  never run against a base it assumed — and skips cascade down the chain.

Everything else is deliberately reused, not rewritten: tasks travel as the fleet's wire dicts,
outcomes are `WorkerOutcome`, the runner has the `make_repo_runner` shape (materialise an isolated
clone → `run_loop` → grade → push/PR), and the per-task hooks are the `run` CLI's own seams
(`--review` → `ShellReviewHook`, `--validate` → the pre-loop reproduce check).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import tomllib
from pydantic import BaseModel, Field, model_validator

from .. import secrets
from ..agent import Agent, build_agent
from ..config import Config, load_config
from ..log import get_logger
from ..stops import StopReason
from .fleet import InMemoryQueue, Queue, TaskRunner, WorkerOutcome, _grade, _prepare_repo, run_workers

# Batch-specific terminal strings, alongside the StopReason values and the fleet's "error".
# "skipped": a dependency did not reach DONE, so the task never ran (its base assumption is void).
# "validate_abort": the pre-loop validate command failed — the goal no longer reproduces / is stale,
# so the run spent nothing (mirrors `run --validate`'s exit-3 semantics).
SKIPPED = "skipped"
VALIDATE_ABORT = "validate_abort"

_log = get_logger("batch")


# --------------------------------------------------------------------------------------------
# Manifest — the declarative batch: which tasks, and how they may collide.
# --------------------------------------------------------------------------------------------
class BatchDefaults(BaseModel):
    """Batch-wide defaults a task inherits unless it sets its own value."""

    config: str | None = None             # base loopkit.toml for tasks without a per-task config
    provider: str = "auto"                # forge for issue-sourced goals: auto | github | gitlab
    review: str | None = None             # per-tick review command (ShellReviewHook), see `run --review`
    validate_cmd: str | None = Field(default=None, alias="validate")  # pre-loop check, see `run --validate`

    model_config = {"populate_by_name": True}


class TaskSpec(BaseModel):
    """One task row (`[[task]]`): what to do, and how it may collide with the others.

    The goal comes from `goal` verbatim or from forge issue `issue` (title + body, fetched by the
    CLI before dispatch — exactly `run --from-issue`). `config` points at a per-task loopkit.toml
    so each task can carry its own gates/budget/protected-path unlocks; absent, the batch default
    config applies. `group`/`after` are the scheduling fields (see the module docstring).
    """

    id: str
    goal: str | None = None
    issue: int | None = None
    title: str | None = None              # PR title override (issue-sourced tasks get the issue title)
    config: str | None = None             # per-task loopkit.toml (overrides [defaults] config)
    branch: str | None = None             # default: loopkit/issue-<n> when issue-sourced, else loopkit/<id>
    group: str | None = None              # serialize with other members, manifest order
    after: list[str] = Field(default_factory=list)  # dependency edges (skip if a dep fails)
    touches: list[str] = Field(default_factory=list)  # predicted-touch paths (advisory input to
                                          # `loopkit overlap`; never affects scheduling by itself)
    review: str | None = None             # per-task override of [defaults] review
    validate_cmd: str | None = Field(default=None, alias="validate")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _has_work(self) -> "TaskSpec":
        if self.goal is None and self.issue is None:
            raise ValueError(f"task '{self.id}': set either goal or issue — there is nothing to do")
        return self


class BatchManifest(BaseModel):
    """The whole batch as one object: `[defaults]` + a `[[task]]` list, validated up front.

    Validation catches the failure modes that would otherwise surface as a wedged scheduler twenty
    minutes in: duplicate ids, `after` edges pointing at nothing, and dependency cycles.
    """

    defaults: BatchDefaults = Field(default_factory=BatchDefaults)
    task: list[TaskSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> "BatchManifest":
        ids = [t.id for t in self.task]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate task ids: {', '.join(sorted(dupes))}")
        known = set(ids)
        for t in self.task:
            missing = [d for d in t.after if d not in known]
            if missing:
                raise ValueError(f"task '{t.id}': after references unknown task(s): {', '.join(missing)}")
            if t.id in t.after:
                raise ValueError(f"task '{t.id}': depends on itself")
        cycle = _find_cycle({t.id: t.after for t in self.task})
        if cycle:
            raise ValueError(f"dependency cycle: {' -> '.join(cycle)}")
        return self


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return one dependency cycle as a path (for the error message), or None if the DAG is clean."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in edges}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        path.append(node)
        for dep in edges.get(node, []):
            if color[dep] == GREY:                       # back edge — cycle closes here
                return path[path.index(dep):] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        color[node] = BLACK
        path.pop()
        return None

    for node in edges:
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def load_manifest(path: str | Path) -> BatchManifest:
    """Read and validate a batch manifest TOML into a `BatchManifest`."""
    p = Path(path).expanduser()
    with p.open("rb") as handle:
        data = tomllib.load(handle)
    return BatchManifest.model_validate(data)


# --------------------------------------------------------------------------------------------
# Scheduler — pure functions over (specs, finished outcomes), so every rule tests at zero tokens.
# --------------------------------------------------------------------------------------------
def ready_tasks(specs: list[TaskSpec], finished: dict[str, WorkerOutcome],
                pushed: set[str]) -> list[TaskSpec]:
    """The tasks that may start *now*: deps DONE, and no earlier same-group task still unfinished.

    Group members serialize in **manifest order** — a member is ready only once every earlier member
    has an outcome (success or not: a group is mutual exclusion, not a success dependency). `after`
    edges must have reached DONE; a failed dep makes the task *skippable*, not ready.
    """
    ready: list[TaskSpec] = []
    group_blocked: set[str] = set()                       # groups with an earlier unfinished member
    for spec in specs:                                    # manifest order is the serialization order
        blocked_group = spec.group in group_blocked if spec.group else False
        if spec.group and (spec.id not in finished):
            group_blocked.add(spec.group)                 # this member now blocks later members
        if spec.id in pushed or blocked_group:
            continue
        if all(d in finished and finished[d].done for d in spec.after):
            ready.append(spec)
    return ready


def skippable_tasks(specs: list[TaskSpec], finished: dict[str, WorkerOutcome],
                    pushed: set[str]) -> list[tuple[TaskSpec, str]]:
    """Tasks that must be skipped: some dependency finished without reaching DONE.

    Returns (spec, failed-dep-id) pairs. Skips cascade — a skipped dep is itself finished-not-done,
    so its dependents show up here on the next pass.
    """
    out: list[tuple[TaskSpec, str]] = []
    for spec in specs:
        if spec.id in pushed:
            continue
        failed = next((d for d in spec.after if d in finished and not finished[d].done), None)
        if failed is not None:
            out.append((spec, failed))
    return out


def plan_waves(specs: list[TaskSpec]) -> dict[str, int]:
    """Earliest-start wave per task, assuming instant completions — the dry-run schedule preview.

    Wave 1 tasks can all start immediately; a task's wave is one past the latest of its deps and its
    group predecessor. Display-only: the live scheduler is event-driven (a task starts the moment its
    predecessors finish), so waves are a lower bound on overlap, not an execution barrier.
    """
    wave: dict[str, int] = {}
    last_in_group: dict[str, str] = {}
    for spec in specs:
        w = 1
        for dep in spec.after:
            w = max(w, wave[dep] + 1)
        if spec.group and spec.group in last_in_group:
            w = max(w, wave[last_in_group[spec.group]] + 1)
        wave[spec.id] = w
        if spec.group:
            last_in_group[spec.group] = spec.id
    return wave


# --------------------------------------------------------------------------------------------
# Driver — the in-process fleet: push ready tasks as predecessors finish, collect every outcome.
# --------------------------------------------------------------------------------------------
@dataclass
class BatchRow:
    """One task's final line: its spec and its outcome (synthetic for skipped tasks)."""

    spec: TaskSpec
    outcome: WorkerOutcome

    @property
    def done(self) -> bool:
        return self.outcome.done


@dataclass
class BatchResult:
    """Every task accounted for — done, failed, or skipped; nothing silently dropped."""

    rows: list[BatchRow] = field(default_factory=list)

    @property
    def done(self) -> list[BatchRow]:
        return [r for r in self.rows if r.done]

    @property
    def failed(self) -> list[BatchRow]:
        return [r for r in self.rows if not r.done and r.outcome.reason != SKIPPED]

    @property
    def skipped(self) -> list[BatchRow]:
        return [r for r in self.rows if r.outcome.reason == SKIPPED]


def load_journal(path: str | Path) -> dict[str, WorkerOutcome]:
    """The last recorded outcome per task from a batch journal (one JSON line per finished task).

    The journal is the batch's durable checklist: `run_batch` appends each outcome the moment it
    lands, so a crash loses nothing already finished. Resume = load this, keep the DONE entries,
    and hand them to `run_batch(preloaded=...)` — successes skip, failures re-run (mold's own
    resume semantics). A torn last line from a crash is skipped, not fatal.
    """
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, WorkerOutcome] = {}
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            out[data["task_id"]] = WorkerOutcome(**data)
        except (ValueError, TypeError, KeyError):
            continue                                      # torn/foreign line — ignore honestly
    return out


def branch_for(spec: TaskSpec) -> str:
    """The task's run branch: explicit, else `loopkit/issue-<n>` (issue-sourced) or `loopkit/<id>`."""
    if spec.branch:
        return spec.branch
    return f"loopkit/issue-{spec.issue}" if spec.issue is not None else f"loopkit/{spec.id}"


def spec_to_task(spec: TaskSpec, defaults: BatchDefaults) -> dict:
    """Map a spec into the fleet's wire dict (the shape `Worker`/`TaskRunner` already consume).

    The goal must be resolved by now (the CLI fetches issue-sourced goals before dispatch, so a bad
    issue number fails fast instead of mid-batch). Per-task review/validate fall back to the batch
    defaults; `config` rides through as a path for the runner to load in its own thread.
    """
    return {"id": spec.id, "goal": spec.goal, "issue": spec.issue, "title": spec.title,
            "branch": branch_for(spec), "config": spec.config,
            "review": spec.review or defaults.review,
            "validate": spec.validate_cmd or defaults.validate_cmd}


def run_batch(specs: list[TaskSpec], runner: TaskRunner, *, jobs: int = 3,
              defaults: BatchDefaults | None = None, queue: Queue | None = None,
              poll: float = 0.05, timeout: float | None = None,
              run_id: str = "batch", journal: Path | None = None,
              preloaded: dict[str, WorkerOutcome] | None = None,
              on_finish: Callable[[WorkerOutcome, int, int], None] | None = None) -> BatchResult:
    """Drive the whole batch to completion: N worker threads, conflict-aware dispatch, every task
    accounted for.

    The workers are the fleet's own (`run_workers` over an `InMemoryQueue` — no Redis); this loop is
    the *scheduler* the blind `Coordinator.run_fleet` barrier doesn't have: it pushes a task the
    moment its `after` deps are DONE and its group predecessors are finished, marks dependents of a
    failed task `skipped` without running them, and returns when every task has an outcome.

    `timeout` (wall-clock seconds) is the stall guard: on expiry, still-unfinished tasks get an
    `error` outcome and the batch returns — a wedged agent can't hold the batch open forever.

    `journal` (append-only, one JSON line per outcome as it lands) is the durable checklist a
    crash can't erase; `preloaded` seeds already-finished outcomes (resume: pass the journal's
    DONE entries — they skip, count as satisfied deps, and appear in the result). `on_finish`
    fires once per newly-finished task with (outcome, finished_count, total) — progress reporting
    without coupling the driver to a console.
    """
    queue = queue or InMemoryQueue()
    log = get_logger("batch", run_id)
    workers, threads = run_workers(queue, runner, count=max(1, jobs), run_id=run_id)
    finished: dict[str, WorkerOutcome] = dict(preloaded or {})
    pushed: set[str] = set(finished)                      # preloaded = accounted for, never queued
    defaults = defaults or BatchDefaults()
    deadline = time.monotonic() + timeout if timeout else None

    def record(outcome: WorkerOutcome) -> None:
        if journal is not None:
            with journal.open("a") as handle:
                handle.write(json.dumps(asdict(outcome)) + "\n")
        if on_finish is not None:
            on_finish(outcome, len(finished), len(specs))

    log.info("batch.start", tasks=len(specs), jobs=max(1, jobs), resumed=len(pushed))
    try:
        while len(finished) < len(specs):
            moved = False                                 # any skip, push, or collected result
            for spec, failed_dep in skippable_tasks(specs, finished, pushed):
                outcome = WorkerOutcome(task_id=spec.id, branch=branch_for(spec), reason=SKIPPED,
                                        error=f"dependency '{failed_dep}' did not reach done")
                finished[spec.id] = outcome
                pushed.add(spec.id)                       # accounted for; never enqueued
                moved = True
                log.info("task.skip", task=spec.id, dep=failed_dep)
                record(outcome)
            for spec in ready_tasks(specs, finished, pushed):
                queue.push_task(spec_to_task(spec, defaults))
                pushed.add(spec.id)
                moved = True
                log.info("task.push", task=spec.id, group=spec.group or "-")
            for task_id in pushed - finished.keys():
                outcome = queue.get_result(task_id)
                if outcome is not None:
                    finished[task_id] = outcome
                    moved = True
                    log.info("task.finish", task=task_id, reason=outcome.reason,
                             done=outcome.done, iters=outcome.iterations)
                    record(outcome)
            if len(finished) == len(specs):
                break
            if deadline and time.monotonic() > deadline:
                for spec in specs:                        # account for everything still open
                    if spec.id not in finished:
                        finished[spec.id] = WorkerOutcome(
                            task_id=spec.id, branch=branch_for(spec),
                            reason="error", error=f"batch timeout after {timeout}s")
                        record(finished[spec.id])
                log.warn("batch.timeout", timeout=timeout)
                break
            in_flight = len(pushed) - len(finished)
            if not moved and in_flight == 0:
                # Validation makes this unreachable (acyclic, refs exist) — guard it anyway so a
                # future scheduler bug stalls loudly instead of spinning forever. `moved` matters:
                # a cascade of skips advances one edge per pass with nothing in flight, and each
                # pass that skips something counts as movement.
                raise RuntimeError("batch scheduler stalled: no task ready, none in flight — "
                                   "is the manifest acyclic?")
            if not moved:
                time.sleep(poll)
    finally:
        for worker in workers:
            worker.stop()
        for thread in threads:
            thread.join(timeout=5)
    result = BatchResult(rows=[BatchRow(spec=s, outcome=finished[s.id]) for s in specs])
    log.info("batch.done", done=len(result.done), failed=len(result.failed),
             skipped=len(result.skipped), total=len(specs))
    return result


# --------------------------------------------------------------------------------------------
# Runner — one task end to end, honoring its own config + hooks. The make_repo_runner shape.
# --------------------------------------------------------------------------------------------
def make_batch_runner(*, base_config: Config | None = None, open_pr: bool = False,
                      agent_factory: Callable[[dict], Agent] | None = None,
                      executor=None, artifacts_dir: Path | None = None,
                      no_review: bool = False) -> TaskRunner:
    """A `TaskRunner` for manifest tasks: load the task's config, materialise an isolated clone,
    run the pre-loop validate, run the loop (with its review hook), grade, and push + PR on DONE.

    Differs from `make_repo_runner` in one deliberate way: instead of one shared gate/adapter
    parameterisation for every task, **each task carries its own full `Config`** (via its `config`
    path, else `base_config`) — per-task gates, budget, stops, and protected-path unlocks, the
    quality policy as a reviewable artifact. The config's `repo` is the clone *source*; the runner
    repoints the loaded config at its own scratch clone, so concurrent same-repo tasks never share
    a working tree and the developer's checkout is never touched.

    `agent_factory` scripts the agent (tests use a `MockAgent`); default builds each task's
    configured adapter. `open_pr=True` flips `[remote]` on per task, exactly like `run --open-pr`.
    """
    def run_task(task: dict) -> WorkerOutcome:
        task_id = str(task["id"])
        branch = task.get("branch") or f"loopkit/{task_id}"
        # Durable activity artifact lands in the PERSISTENT artifacts dir (next to the journal), never
        # in `scratch` below — that worktree is rmtree'd in the finally, this outlives the run.
        activity_path = (artifacts_dir / f"{task_id}.activity.jsonl") if artifacts_dir else None
        cfg = load_config(task["config"]) if task.get("config") else base_config
        if cfg is None:
            raise RuntimeError(f"task '{task_id}' has no config — set [defaults] config in the "
                               "manifest or a per-task config")
        cfg = cfg.model_copy(deep=True)                   # never mutate a shared base across threads
        if task.get("goal"):
            cfg.goal = task["goal"]
        cfg.branch = branch
        if open_pr:
            cfg.remote.enabled = True
            cfg.remote.push = True
            cfg.remote.open_pr = True
        scratch = Path(tempfile.mkdtemp(prefix="loopkit-batch-"))
        tlog = _log.bind(task=task_id)
        try:
            repo = _prepare_repo(cfg.repo, scratch, mode="clone")
            cfg.repo = str(repo)
            # Pre-loop validate (the `run --validate` seam): non-zero exit ⇒ the goal is stale /
            # already fixed / doesn't reproduce — abort before the agent spends anything.
            if task.get("validate"):
                env = {**secrets.current().child_env(), "PYTHONDONTWRITEBYTECODE": "1"}
                vproc = subprocess.run(task["validate"], cwd=repo, shell=True, env=env,
                                       capture_output=True, text=True)
                if vproc.returncode != 0:
                    detail = ((vproc.stdout or "") + (vproc.stderr or "")).strip()[-500:]
                    tlog.info("task.validate_abort", exit=vproc.returncode)
                    return WorkerOutcome(task_id=task_id, branch=branch, reason=VALIDATE_ABORT,
                                         error=secrets.redact(detail) or "validate: non-zero exit")
            # Review is opt-out: a manifest `review =` wins, else the task's (or base) config
            # [review] command runs by default — unless --no-review. So a batch can't silently skip
            # the quality gate just because a task's manifest entry omitted `review =`.
            review_cmd = cfg.review.resolved(override=task.get("review"), disabled=no_review)
            if review_cmd is None and base_config is not None and not no_review:
                review_cmd = base_config.review.resolved()   # a base-config default covers every task
            review_hook = None
            if review_cmd:
                from .review import ShellReviewHook
                review_hook = ShellReviewHook(review_cmd)
            agent = agent_factory(task) if agent_factory else build_agent(cfg.agent, executor=executor)
            from ..loop import run_loop
            result = run_loop(cfg, agent, review_hook=review_hook, executor=executor,
                              trace_metadata={"task": task_id, "issue": task.get("issue")},
                              activity_path=activity_path)
            score, revalidated = _grade(result)
            pr_url: str | None = None
            if cfg.remote.enabled and result.reason is StopReason.DONE:
                from .remote import sync_done
                sync = sync_done(cfg, repo, title=f"loopkit: {task.get('title') or task_id}",
                                 issue=task.get("issue"))
                pr_url = sync["pr_url"]
                tlog.info("remote.sync", pushed=sync["pushed"], pr=pr_url or "-")
            return WorkerOutcome(task_id=task_id, branch=branch, reason=result.reason.value,
                                 iterations=result.iterations, cost_usd=result.cost_usd,
                                 overfit=result.overfit, score=score, revalidated=revalidated,
                                 pr_url=pr_url)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    return run_task
