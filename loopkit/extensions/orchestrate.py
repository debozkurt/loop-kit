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

# Evolutionary search adds two more callables. A Scorer ranks a finished candidate's worktree
# (higher = fitter) — this is the *selection* signal. A RevalidateFactory yields a *held-out*
# gate the candidate never optimized or was selected against — the Ch 9 guard that catches a
# winner whose high selection score was luck (selection inflation) rather than skill.
Scorer = Callable[[dict, Path], float]
RevalidateFactory = Callable[[dict, Path], Gate]
# Build one candidate's task from (base_task, generation, candidate_index, seed_branch). The
# default carries the prior winner forward both ways: tree-level (the worktree branches off
# seed_branch) and prompt-level (a seed note appended to the goal, for live agents).
CandidateTaskFactory = Callable[[dict, int, int, "str | None"], dict]


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


@dataclass
class Candidate:
    """One attempt in an evolutionary generation: a worker plus its selection score."""

    task: dict
    branch: str
    worktree: Path
    result: RunResult | None
    score: float                        # the Scorer's fitness (-inf if it never finished)
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.result is not None and self.result.reason is StopReason.DONE


@dataclass
class Generation:
    """One round: every candidate (ranked by score), the survivors kept, and the confirmed best.

    `confirmed` is the highest-scoring survivor that *also* passed re-validation — the held-out
    check it never competed on. It is what seeds the next generation, so a lucky overfit can't
    propagate. `inflated` is the tell: the top scorer was not the one that survived re-validation.
    """

    index: int
    candidates: list[Candidate]         # sorted by score, descending
    survivors: list[Candidate]          # the top-k kept
    confirmed: Candidate | None         # best survivor that passed re-validation (the seed)

    @property
    def inflated(self) -> bool:
        """True when the top-scoring survivor failed re-validation — selection inflation caught."""
        if not self.survivors:
            return False
        return self.confirmed is None or self.confirmed.branch != self.survivors[0].branch


@dataclass
class EvolutionResult:
    """The whole run: every generation, with the final validated winner surfaced."""

    generations: list[Generation] = field(default_factory=list)

    @property
    def winner(self) -> Candidate | None:
        """The most recent generation's confirmed (re-validated) best, if any."""
        for generation in reversed(self.generations):
            if generation.confirmed is not None:
                return generation.confirmed
        return None

    @property
    def inflation_caught(self) -> bool:
        """True if re-validation ever overruled a generation's top-scoring candidate."""
        return any(generation.inflated for generation in self.generations)


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
        workers = self._dispatch(tasks, root, self.base)
        result = FleetResult(workers=workers)

        if not self.keep_worktrees:
            self._teardown(workers, root)
        self._log.info("fleet.done", done=len(result.done), failed=len(result.failed),
                       total=len(tasks))
        return result

    def _dispatch(self, tasks: list[dict], root: Path, base: str) -> list[WorkerResult]:
        """Run `tasks` concurrently (bounded pool), each in its own worktree off `base`.

        The shared engine under both strategies: blind fan-out calls it once; evolution calls it
        once per generation, varying `base` to branch each generation off the prior winner.
        """
        slots: list[WorkerResult | None] = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._run_worker, task, i, root, base): i
                       for i, task in enumerate(tasks)}
            for fut in as_completed(futures):
                slots[futures[fut]] = fut.result()
        return [w for w in slots if w is not None]

    def _run_worker(self, task: dict, index: int, root: Path, base: str) -> WorkerResult:
        """Run one task to a terminal in its own worktree. Never raises — failures are returned."""
        branch = task.get("branch") or f"{self.base_config.branch}-{task.get('slug', index)}"
        worktree = root / branch.replace("/", "-")
        wlog = self._log.bind(task=index, branch=branch)
        try:
            make_worktree(self._repo, branch, worktree, base=base)
            wlog.info("worker.start", worktree=str(worktree), base=base,
                      goalLen=len(task["goal"]))
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

    def _teardown(self, units: list, root: Path) -> None:
        """Remove each unit's worktree checkout (branches persist), then the temp root."""
        for unit in units:
            if unit.worktree.exists():
                remove_worktree(self._repo, unit.worktree)
        shutil.rmtree(root, ignore_errors=True)

    def evolve(self, base_task: dict, *, generations: int, population: int, keep: int,
               score: Scorer, revalidate: RevalidateFactory,
               candidate_task: CandidateTaskFactory | None = None) -> EvolutionResult:
        """Evolutionary search: N attempts per generation, keep the validated best, reseed it.

        Each generation fans `population` attempts at the *same* goal out to isolated workers,
        scores them, keeps the top `keep`, and re-validates the survivors on a held-out gate to
        find the one real winner. The next generation branches off that winner — its code carried
        forward — so improvement compounds. But only *validated* improvement: re-validation gates
        the seed, so a candidate whose high selection score was luck never becomes the seed and
        its noise can't compound across generations. That guard is Ch 9 at the fleet scale —
        best-of-N is itself a way to overfit, and a held-out check is the only honest defence.
        """
        builder = candidate_task or _default_candidate_task
        root = Path(tempfile.mkdtemp(prefix="loopkit-evolve-"))
        log = self._log.bind(mode="evolve")
        log.info("evolve.start", generations=generations, population=population, keep=keep)
        result = EvolutionResult()
        seed_branch: str | None = None
        try:
            for g in range(generations):
                base = seed_branch or self.base
                tasks = [builder(base_task, g, i, seed_branch) for i in range(population)]
                glog = log.bind(gen=g, base=base)
                glog.info("generation.start", population=population)

                workers = self._dispatch(tasks, root, base)
                candidates = self._score_candidates(workers, score)
                candidates.sort(key=lambda c: c.score, reverse=True)
                survivors = candidates[:max(1, keep)]
                confirmed = self._first_revalidated(survivors, revalidate, glog)

                generation = Generation(index=g, candidates=candidates, survivors=survivors,
                                        confirmed=confirmed)
                result.generations.append(generation)
                glog.info("generation.done",
                          best=survivors[0].branch if survivors else "-",
                          bestScore=round(survivors[0].score, 4) if survivors else None,
                          confirmed=confirmed.branch if confirmed else "-",
                          inflated=generation.inflated)

                if confirmed is not None:
                    seed_branch = confirmed.branch      # only a validated winner reseeds
                if not self.keep_worktrees:             # branches persist; checkouts go
                    for candidate in candidates:
                        if candidate.worktree.exists():
                            remove_worktree(self._repo, candidate.worktree)
        finally:
            if not self.keep_worktrees:
                shutil.rmtree(root, ignore_errors=True)

        winner = result.winner
        log.info("evolve.done", winner=winner.branch if winner else "-",
                 winnerScore=round(winner.score, 4) if winner else None,
                 inflationCaught=result.inflation_caught)
        return result

    def _score_candidates(self, workers: list[WorkerResult], score: Scorer) -> list[Candidate]:
        """Score each finished worker in its worktree; unfinished or unscored -> -inf (last)."""
        candidates: list[Candidate] = []
        for worker in workers:
            value = float("-inf")
            if worker.result is not None and worker.worktree.exists():
                try:
                    value = float(score(worker.task, worker.worktree))
                except Exception as exc:   # noqa: BLE001 — a bad scorer must not sink the run
                    self._log.warn("score.error", branch=worker.branch, error=type(exc).__name__)
            candidates.append(Candidate(task=worker.task, branch=worker.branch,
                                        worktree=worker.worktree, result=worker.result,
                                        score=value, error=worker.error))
        return candidates

    def _first_revalidated(self, survivors: list[Candidate], revalidate: RevalidateFactory,
                           glog) -> Candidate | None:
        """Walk survivors high-score-first; return the first that passes the held-out gate."""
        for candidate in survivors:
            if not candidate.worktree.exists():
                continue
            verdict = revalidate(candidate.task, candidate.worktree).check(candidate.worktree)
            glog.info("revalidate", branch=candidate.branch, score=round(candidate.score, 4),
                      passed=verdict.passed)
            if verdict.passed:
                return candidate
        return None


def _default_candidate_task(base_task: dict, generation: int, candidate: int,
                            seed_branch: str | None) -> dict:
    """Derive a candidate's task: a unique slug per (generation, candidate), winner reseeded.

    Tree-level reseeding is done in `_dispatch` (the worktree branches off `seed_branch`, so the
    prior winner's code is the starting tree). Here we add the prompt-level half: a seed note
    appended to the goal so a *live* agent is told to refine the prior best instead of starting
    over. MockAgents ignore the prompt, so tests exercise the tree-level path; live runs get both.
    """
    task = dict(base_task)
    task["slug"] = f"g{generation}-c{candidate}"
    task["generation"] = generation
    task["candidate"] = candidate
    if seed_branch:
        task["seed_branch"] = seed_branch
        task["goal"] = (f"{base_task['goal']}\n\nYou are improving on a prior best attempt whose "
                        "code is already in the working tree — refine it, do not start over.")
    return task


def run_fleet(base_config: Config, tasks: list[dict], *, make_agent: AgentFactory,
              make_gates: GateFactory | None = None, max_workers: int = 4,
              base: str = "HEAD", keep_worktrees: bool = False) -> FleetResult:
    """Convenience: build a Supervisor and run a single blind fan-out over `tasks`."""
    return Supervisor(base_config, make_agent=make_agent, make_gates=make_gates,
                      max_workers=max_workers, base=base,
                      keep_worktrees=keep_worktrees).run_fleet(tasks)
