"""Orchestration — a supervisor over many worker loops (Chapters 10-12). [Part II]

The single-agent core drives one loop. Orchestration runs *many* loops over independent
tasks and keeps them from stepping on each other. The isolation primitive is the **git
worktree**: a second working directory backed by the one object store, so each worker edits
and commits in physical isolation (no file collisions between parallel workers) while every
commit still lands in the same repo — a winner's branch is recoverable from the main checkout
afterwards. `run_loop` becomes the worker body *unchanged*; this module only wraps it.

Two strategies sit on this base. **Blind fan-out** (here) dispatches N independent tasks to N
isolated workers and collects the terminals — no worker sees another's work. **Evolutionary**
(the next layer) scores the fleet, keeps the top-k, and reseeds the winners' diffs into the
next generation's prompts; it must carry the Ch 9 selection-inflation guard — re-validate the
kept winner on a held-out gate it never competed on, because best-of-N is itself a way of
overfitting to noise. Fan-out is the foundation both share, so it lands first.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import durability
from ..agent import Agent
from ..config import Config
from ..gate import Gate
from ..log import get_logger
from ..loop import RunResult, run_loop
from ..stops import StopReason

# The supervisor calls these once per task. A fresh Agent per worker because adapters are
# stateful (MockAgent walks a behavior list, so a shared instance would interleave ticks);
# gates are optional — return (None, None) to let run_loop build the ShellGates from config,
# which is the production path. Tests and demos inject deterministic gates instead.
AgentFactory = Callable[[dict], Agent]
GateFactory = Callable[[dict, Path], tuple[Gate | None, Gate | None]]


@dataclass
class WorkerResult:
    """One worker's outcome: which task it ran, on which branch/worktree, and its terminal."""

    task: dict
    branch: str
    worktree: Path
    result: RunResult | None = None     # None iff the worker raised before/within run_loop
    error: str | None = None            # the exception text when result is None

    @property
    def done(self) -> bool:
        """True only when the worker's loop reached DONE (the acceptance gate passed)."""
        return self.result is not None and self.result.reason is StopReason.DONE


@dataclass
class FleetResult:
    """The fleet's outcome: every worker, with the convenience split into done / not-done."""

    workers: list[WorkerResult] = field(default_factory=list)

    @property
    def done(self) -> list[WorkerResult]:
        return [w for w in self.workers if w.done]

    @property
    def failed(self) -> list[WorkerResult]:
        """Workers that halted on any non-DONE terminal, or crashed before reaching one."""
        return [w for w in self.workers if not w.done]


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


# `git worktree add` mutates the shared .git/worktrees registry, so concurrent adds can race
# its lock. Serialize *creation* only; the loops themselves then run fully in parallel (each
# commits into its own per-worktree index/HEAD, which don't contend).
_WORKTREE_LOCK = threading.Lock()


def make_worktree(repo: Path, branch: str, path: Path, *, base: str = "HEAD") -> Path:
    """Check out an isolated worktree at `path` on a fresh `branch` off `base`. Returns `path`.

    The fresh branch is what makes a worker's commits its own: every tick commits onto
    `branch`, so the work is durable and resumable per worker (Ch 15) and recoverable from the
    main repo after the worktree is torn down.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WORKTREE_LOCK:
        _git(repo, "worktree", "add", "-b", branch, str(path), base, check=True)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    """Tear down a worktree checkout. The branch and its commits survive in the repo."""
    _git(repo, "worktree", "remove", "--force", str(path))


class Supervisor:
    """Runs many worker loops over independent tasks, each in its own worktree/branch.

    Blind fan-out: dispatch every task to its own isolated worker and collect the terminals.
    The pool is bounded so a fleet of 50 tasks doesn't open 50 agents at once — `max_workers`
    is the only knob between "run them all now" and "trickle them through". One worker crashing
    is contained to its own `WorkerResult.error`; it never sinks the fleet.

    The evolutionary strategy (select top-k, reseed winners) layers onto this same machinery in
    the next step, carrying the Ch 9 selection-inflation guard.
    """

    def __init__(self, base_config: Config, *, make_agent: AgentFactory,
                 make_gates: GateFactory | None = None, max_workers: int = 4,
                 base: str = "HEAD", keep_worktrees: bool = False) -> None:
        self.base_config = base_config
        self.make_agent = make_agent
        self.make_gates = make_gates
        self.max_workers = max(1, max_workers)
        self.base = base
        self.keep_worktrees = keep_worktrees
        self._repo = base_config.repo_path()
        self._run_id = durability.state_signature(self._repo)[:8]
        self._log = get_logger("fleet", self._run_id)

    def run_fleet(self, tasks: list[dict]) -> FleetResult:
        """Blind fan-out over `tasks` — each runs concurrently (bounded) and isolated.

        This is a barrier: it returns only once every worker has reached a terminal, so the
        caller has the whole fleet's outcomes in hand to select from.
        """
        if not tasks:
            self._log.info("fleet.start", tasks=0)
            return FleetResult(workers=[])

        root = Path(tempfile.mkdtemp(prefix="loopkit-fleet-"))
        self._log.info("fleet.start", tasks=len(tasks), maxWorkers=self.max_workers,
                       base=self.base, root=str(root))
        slots: list[WorkerResult | None] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_worker, task, i, root): i
                       for i, task in enumerate(tasks)}
            for fut in as_completed(futures):
                slots[futures[fut]] = fut.result()
        result = FleetResult(workers=[w for w in slots if w is not None])

        if not self.keep_worktrees:
            self._teardown(result, root)
        self._log.info("fleet.done", done=len(result.done), failed=len(result.failed),
                       total=len(tasks))
        return result

    def _run_worker(self, task: dict, index: int, root: Path) -> WorkerResult:
        """Run one task to a terminal in its own worktree. Never raises — failures are returned."""
        branch = task.get("branch") or f"{self.base_config.branch}-{task.get('slug', index)}"
        worktree = root / branch.replace("/", "-")
        wlog = self._log.bind(task=index, branch=branch)
        try:
            make_worktree(self._repo, branch, worktree, base=self.base)
            wlog.info("worker.start", worktree=str(worktree), goalLen=len(task["goal"]))
            # Derive a per-worker config: same envelope, this task's goal, pointed at the
            # isolated worktree and branch. Nested config models are shared read-only.
            cfg = self.base_config.model_copy(update={
                "goal": task["goal"], "repo": str(worktree), "branch": branch})
            iteration_gate, acceptance_gate = (
                self.make_gates(task, worktree) if self.make_gates else (None, None))
            result = run_loop(cfg, self.make_agent(task), iteration_gate=iteration_gate,
                              acceptance_gate=acceptance_gate)
            wlog.info("worker.done", reason=result.reason.value, iterations=result.iterations,
                      costUsd=round(result.cost_usd, 4), overfit=result.overfit)
            return WorkerResult(task=task, branch=branch, worktree=worktree, result=result)
        except Exception as exc:   # noqa: BLE001 — one worker crashing must not sink the fleet
            wlog.error("worker.error", error=type(exc).__name__, detail=str(exc)[:200])
            return WorkerResult(task=task, branch=branch, worktree=worktree, error=str(exc))

    def _teardown(self, result: FleetResult, root: Path) -> None:
        for worker in result.workers:
            if worker.worktree.exists():
                remove_worktree(self._repo, worker.worktree)
        shutil.rmtree(root, ignore_errors=True)


def run_fleet(base_config: Config, tasks: list[dict], *, make_agent: AgentFactory,
              make_gates: GateFactory | None = None, max_workers: int = 4,
              base: str = "HEAD", keep_worktrees: bool = False) -> FleetResult:
    """Convenience: build a Supervisor and run a single blind fan-out over `tasks`."""
    return Supervisor(base_config, make_agent=make_agent, make_gates=make_gates,
                      max_workers=max_workers, base=base,
                      keep_worktrees=keep_worktrees).run_fleet(tasks)
