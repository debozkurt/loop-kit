"""The deployable fleet — the loop behind a queue, run by many containers (Chapter 12). [Part II]

Chapter 10's `Supervisor` fans `run_loop` out across **git worktrees** in one process: isolation
is *logical* (one repo, many working dirs). This module graduates that to a deployed fleet where
isolation is *physical*: each worker is its own container with its own filesystem, so it clones
the target repo and works on its own branch — no worktree machinery needed. The pieces no longer
share a process, so the in-memory `Future` is replaced by a **Redis queue**: the coordinator
`LPUSH`es tasks and polls a results hash; workers `BRPOP` a task, run the loop, and `HSET` the
outcome. The queue is also the Ch 12 *trigger* seam — a worker is indifferent to what woke it, so
anything that can push a task (a human, a cron, a webhook) drives the fleet.

What is deliberately **reused, not rewritten**:

- **The worker body is `run_loop`, unchanged** — container isolation replaces the worktree.
- **The result shapes** (`WorkerResult` / `FleetResult` / `Candidate` / `Generation` /
  `EvolutionResult`) are the in-process orchestrator's; the coordinator maps the same data through
  Redis instead of futures. `WorkerOutcome` is just their flat, JSON-able wire form.
- **The Ch 9 selection-inflation guard.** `evolve` keeps best-of-N, then confirms the
  highest-scoring survivor that *also* passed a held-out check it never competed on. Across
  containers the held-out check runs **in the worker** (only it has the candidate's filesystem)
  and rides back as `WorkerOutcome.revalidated`; the coordinator then applies the identical
  selection rule — walk survivors high-score-first, take the first that passed held-out. The
  *outcome* (which candidate reseeds) is identical to the in-process `Supervisor.evolve`; only
  *where* the gate runs moves. Only a re-validated winner reseeds, so a lucky overfit can't
  compound across generations.

The transport (queue), the worker loop, and the coordinator's selection logic are all unit-testable
with **no cluster and no tokens**: an `InMemoryQueue` (or `fakeredis`) + a `MockAgent`-backed runner.
The kind/Tilt/k8s layer is the thin outer shell exercised manually via `tilt up`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..agent import Agent, MockAgent, build_agent
from ..config import AgentConfig, Config, GateConfig, RemoteConfig, SafetyConfig, StopsConfig
from ..gate import ShellGate
from ..log import get_logger
from ..loop import RunResult, run_loop
from ..stops import StopReason
from .orchestrate import (
    Candidate,
    EvolutionResult,
    FleetResult,
    Generation,
    WorkerResult,
    _default_candidate_task,
)

# A worker turns one task (a JSON-able dict off the queue) into one outcome. Injected, because
# *how* a task becomes work is the one thing that can't cross the wire: tests inject a MockAgent
# runner, the container injects the demo-repo runner. The Worker loop around it is identical.
TaskRunner = Callable[[dict], "WorkerOutcome"]

_DEFAULT_REDIS_URL = "redis://localhost:6379"


# --------------------------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------------------------
@dataclass
class WorkerOutcome:
    """One worker's report for one task — the Redis wire payload (a flat, JSON-able row).

    A subset of `WorkerResult`/`Candidate` reduced to scalars, because a container can't hand the
    coordinator a live `RunResult` or a `Path` to a filesystem it no longer has. `score` and
    `revalidated` are populated only for evolutionary runs (the worker computes both while it still
    holds the candidate's working tree); fan-out leaves them None.
    """

    task_id: str
    branch: str
    reason: str                          # a StopReason value, or "error" when the worker raised
    iterations: int = 0
    cost_usd: float = 0.0
    overfit: bool = False
    score: float | None = None           # evolve: in-worker selection score (higher = fitter)
    revalidated: bool | None = None      # evolve: in-worker held-out verdict (the Ch 9 guard)
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.reason == StopReason.DONE.value

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> "WorkerOutcome":
        data = json.loads(payload)
        fields = {k: data.get(k) for k in (
            "task_id", "branch", "reason", "iterations", "cost_usd", "overfit",
            "score", "revalidated", "error")}
        return cls(**fields)

    def to_run_result(self) -> RunResult | None:
        """Rebuild a `RunResult` so the reused result shapes read identically on the coordinator.

        None for a crash (`reason == "error"`) — exactly as an in-process worker that raised before
        reaching a terminal has `WorkerResult.result is None`.
        """
        if self.reason == "error":
            return None
        return RunResult(StopReason(self.reason), self.iterations, self.cost_usd,
                         overfit=self.overfit)


# --------------------------------------------------------------------------------------------
# Queue — the transport. Two implementations, one contract.
# --------------------------------------------------------------------------------------------
class Queue(Protocol):
    """The coordinator/worker transport: a task list and a results hash."""

    def push_task(self, task: dict) -> None: ...
    def pop_task(self, timeout: float = 1.0) -> dict | None: ...
    def put_result(self, outcome: WorkerOutcome) -> None: ...
    def get_result(self, task_id: str) -> WorkerOutcome | None: ...
    def pending(self) -> int: ...


class RedisQueue:
    """Redis-backed queue: `LPUSH`/`BRPOP` a task list, `HSET`/`HGET` a results hash.

    `BRPOP` blocks the worker until a task arrives (no busy-poll), and because it pops, two workers
    never get the same task — Redis serves as the work-stealing dispatcher for free. The coordinator
    polls the results hash (one row per task id) until every task it pushed has reported.
    """

    def __init__(self, client, *, namespace: str = "loopkit") -> None:
        self._r = client
        self._tasks = f"{namespace}:tasks"
        self._results = f"{namespace}:results"

    @classmethod
    def from_url(cls, url: str = _DEFAULT_REDIS_URL, *, namespace: str = "loopkit") -> "RedisQueue":
        try:
            import redis                                  # lazy: the core never imports redis
        except ImportError as exc:                        # pragma: no cover - import guard
            raise RuntimeError(
                "the fleet needs the redis client — install it with: pip install 'loopkit[fleet]'"
            ) from exc
        return cls(redis.Redis.from_url(url, decode_responses=True), namespace=namespace)

    def push_task(self, task: dict) -> None:
        self._r.lpush(self._tasks, json.dumps(task))

    def pop_task(self, timeout: float = 1.0) -> dict | None:
        item = self._r.brpop([self._tasks], timeout=timeout)
        return None if item is None else json.loads(item[1])

    def put_result(self, outcome: WorkerOutcome) -> None:
        self._r.hset(self._results, outcome.task_id, outcome.to_json())

    def get_result(self, task_id: str) -> WorkerOutcome | None:
        raw = self._r.hget(self._results, task_id)
        return WorkerOutcome.from_json(raw) if raw is not None else None

    def pending(self) -> int:
        return int(self._r.llen(self._tasks))


class InMemoryQueue:
    """A thread-safe, dependency-free queue with the same contract — for scenarios and tests.

    `demo 12` runs in the container with no Redis, and the unit tests need no server, so the queue
    contract is honoured in-process: a `deque` plus a `Condition` reproduce `LPUSH`/`BRPOP` (append
    left, pop right = FIFO; block on empty). Outcomes are stored as objects (no JSON round-trip);
    the Redis wire path is proved separately against `fakeredis`.
    """

    def __init__(self) -> None:
        self._tasks: deque[dict] = deque()
        self._results: dict[str, WorkerOutcome] = {}
        self._cv = threading.Condition()

    def push_task(self, task: dict) -> None:
        with self._cv:
            self._tasks.appendleft(dict(task))
            self._cv.notify()

    def pop_task(self, timeout: float = 1.0) -> dict | None:
        with self._cv:
            if not self._tasks:
                self._cv.wait(timeout=timeout)
            return self._tasks.pop() if self._tasks else None

    def put_result(self, outcome: WorkerOutcome) -> None:
        with self._cv:
            self._results[outcome.task_id] = outcome

    def get_result(self, task_id: str) -> WorkerOutcome | None:
        with self._cv:
            return self._results.get(task_id)

    def pending(self) -> int:
        with self._cv:
            return len(self._tasks)


# --------------------------------------------------------------------------------------------
# Worker — the loop body, pulled from the queue.
# --------------------------------------------------------------------------------------------
class Worker:
    """Drains tasks off the queue and runs each through the injected runner (`run_loop` inside).

    The whole point is that the worker is identical whether it runs as a pod on the cluster or as a
    thread in a test: `pop -> run -> put`, with the task id as the correlation id on every line it
    logs, and any one task's crash contained to an `error` outcome so it can't sink the pod.
    """

    def __init__(self, queue: Queue, run_task: TaskRunner, *, name: str = "worker",
                 run_id: str = "-", poll_timeout: float = 1.0,
                 stop: threading.Event | None = None) -> None:
        self._q = queue
        self._run = run_task
        self._poll = poll_timeout
        self._stop = stop or threading.Event()
        self._log = get_logger("worker", run_id).bind(name=name)

    def stop(self) -> None:
        """Ask the worker to exit after its current task (the loop checks between pops)."""
        self._stop.set()

    def run_forever(self, max_tasks: int | None = None) -> int:
        """Pop-and-run until stopped (a long-lived pod) or `max_tasks` handled (tests). Count run."""
        handled = 0
        self._log.info("worker.up", poll=self._poll, maxTasks=max_tasks if max_tasks else "-")
        while not self._stop.is_set() and (max_tasks is None or handled < max_tasks):
            task = self._q.pop_task(timeout=self._poll)
            if task is None:                              # idle tick — re-check the stop flag
                continue
            self._handle(task)
            handled += 1
        self._log.info("worker.down", handled=handled)
        return handled

    def _handle(self, task: dict) -> None:
        task_id = str(task.get("id", "?"))
        branch = task.get("branch", "-")
        tlog = self._log.bind(task=task_id)               # correlation id rides every line below
        tlog.info("task.recv", goalLen=len(task.get("goal", "")), branch=branch)
        try:
            outcome = self._run(task)
        except Exception as exc:                          # noqa: BLE001 — one task must not sink the pod
            tlog.error("task.error", error=type(exc).__name__, detail=str(exc)[:200])
            outcome = WorkerOutcome(task_id=task_id, branch=branch, reason="error",
                                    error=str(exc)[:500])
        self._q.put_result(outcome)
        tlog.info("task.done", reason=outcome.reason, done=outcome.done,
                  iters=outcome.iterations, score=outcome.score)


def run_workers(queue: Queue, run_task: TaskRunner, *, count: int, run_id: str = "-",
                poll_timeout: float = 0.2) -> tuple[list[Worker], list[threading.Thread]]:
    """Start `count` worker threads draining `queue` — the in-process stand-in for N pods.

    Returns the workers (call `.stop()` on each) and their threads (join after collecting). Used by
    the scenario and tests to model a fleet of pods without a cluster; on the cluster the same
    `Worker.run_forever` is the container entrypoint instead.
    """
    workers: list[Worker] = []
    threads: list[threading.Thread] = []
    for i in range(count):
        worker = Worker(queue, run_task, name=f"worker-{i}", run_id=run_id, poll_timeout=poll_timeout)
        thread = threading.Thread(target=worker.run_forever, name=f"loopkit-worker-{i}", daemon=True)
        workers.append(worker)
        threads.append(thread)
        thread.start()
    return workers, threads


# --------------------------------------------------------------------------------------------
# Coordinator — enqueue, collect, and (for evolve) select. Transport-only; no agent, no gate.
# --------------------------------------------------------------------------------------------
class Coordinator:
    """Drives the fleet over the queue: enqueue tasks, poll results, assemble the reused shapes.

    The coordinator holds no agent and runs no gate — all of that happens in the workers. It only
    maps tasks out and outcomes back, and for `evolve` applies the selection rule (keep top-k, then
    confirm the first survivor — high score first — that the worker reports passed the held-out
    gate). That last step is the Ch 9 selection-inflation guard, intact at fleet scale.
    """

    def __init__(self, queue: Queue, *, run_id: str = "fleet",
                 collect_timeout: float = 600.0, poll_interval: float = 0.1) -> None:
        self._q = queue
        self._collect_timeout = collect_timeout
        self._poll = poll_interval
        self._log = get_logger("fleet", run_id)

    def run_fleet(self, tasks: list[dict]) -> FleetResult:
        """Blind fan-out over the queue: enqueue every task, collect every outcome (a barrier)."""
        if not tasks:
            self._log.info("fleet.enqueue", tasks=0)
            return FleetResult(workers=[])
        prepared = [self._with_id(task, i) for i, task in enumerate(tasks)]
        for task in prepared:
            self._q.push_task(task)
        self._log.info("fleet.enqueue", tasks=len(prepared))

        outcomes = self._collect([task["id"] for task in prepared])
        workers = [self._to_worker_result(task, outcomes.get(task["id"])) for task in prepared]
        result = FleetResult(workers=workers)
        self._log.info("fleet.collect", done=len(result.done), failed=len(result.failed),
                       total=len(prepared))
        return result

    def evolve(self, base_task: dict, *, generations: int, population: int, keep: int,
               candidate_task=None) -> EvolutionResult:
        """Generational search over the queue, preserving the Ch 9 selection-inflation guard.

        Each generation enqueues `population` attempts at one goal, collects their scored outcomes,
        keeps the top `keep`, and confirms the first survivor (high score first) the worker reports
        passed its held-out gate. Only that confirmed winner reseeds the next generation. v1 reseed
        is **prompt-level** (the winner's seed note is appended to the goal by the candidate-task
        builder); tree-level reseed (gen+1 branches off the winner's branch) needs the winner's tree
        on a shared volume the next generation can clone — that's v2.
        """
        builder = candidate_task or _default_candidate_task
        result = EvolutionResult()
        seed_branch: str | None = None
        self._log.info("evolve.start", generations=generations, population=population, keep=keep)

        for g in range(generations):
            tasks = [self._with_id(builder(base_task, g, i, seed_branch), i)
                     for i in range(population)]
            for task in tasks:
                self._q.push_task(task)
            glog = self._log.bind(gen=g, seed=seed_branch or "-")
            glog.info("generation.enqueue", population=population)

            outcomes = self._collect([task["id"] for task in tasks])
            candidates = [self._to_candidate(task, outcomes.get(task["id"])) for task in tasks]
            candidates.sort(key=lambda c: c.score, reverse=True)
            survivors = candidates[:max(1, keep)]
            confirmed = self._first_revalidated(survivors, outcomes, glog)

            generation = Generation(index=g, candidates=candidates, survivors=survivors,
                                    confirmed=confirmed)
            result.generations.append(generation)
            glog.info("generation.done",
                      best=survivors[0].branch if survivors else "-",
                      bestScore=round(survivors[0].score, 4) if survivors else None,
                      confirmed=confirmed.branch if confirmed else "-",
                      inflated=generation.inflated)
            if confirmed is not None:
                seed_branch = confirmed.branch            # only a validated winner reseeds

        winner = result.winner
        self._log.info("evolve.done", winner=winner.branch if winner else "-",
                       winnerScore=round(winner.score, 4) if winner else None,
                       inflationCaught=result.inflation_caught)
        return result

    # -- internals ---------------------------------------------------------------------------
    @staticmethod
    def _with_id(task: dict, index: int) -> dict:
        """Stamp a stable task id (the correlation key + the results-hash field) if absent."""
        task = dict(task)
        task.setdefault("id", str(task.get("slug") or f"t{index}"))
        return task

    def _collect(self, ids: list[str]) -> dict[str, WorkerOutcome]:
        """Poll the results hash until every id has reported or the collect deadline passes."""
        deadline = time.monotonic() + self._collect_timeout
        pending = set(ids)
        collected: dict[str, WorkerOutcome] = {}
        while pending and time.monotonic() < deadline:
            for task_id in list(pending):
                outcome = self._q.get_result(task_id)
                if outcome is not None:
                    collected[task_id] = outcome
                    pending.discard(task_id)
            if pending:
                time.sleep(self._poll)
        if pending:
            self._log.warn("collect.timeout", missing=len(pending),
                           ids=",".join(sorted(pending)))
        return collected

    def _to_worker_result(self, task: dict, outcome: WorkerOutcome | None) -> WorkerResult:
        if outcome is None:                               # never reported within the deadline
            return WorkerResult(task=task, branch=task.get("branch", "-"), worktree=Path("-"),
                                result=None, error="no result (collect timeout)")
        return WorkerResult(task=task, branch=outcome.branch, worktree=Path("-"),
                            result=outcome.to_run_result(), error=outcome.error)

    def _to_candidate(self, task: dict, outcome: WorkerOutcome | None) -> Candidate:
        if outcome is None or outcome.score is None:      # unreported/unscored sorts last
            return Candidate(task=task, branch=task.get("branch", "-"), worktree=Path("-"),
                             result=outcome.to_run_result() if outcome else None,
                             score=float("-inf"),
                             error=(outcome.error if outcome else "no result (collect timeout)"))
        return Candidate(task=task, branch=outcome.branch, worktree=Path("-"),
                         result=outcome.to_run_result(), score=outcome.score, error=outcome.error)

    def _first_revalidated(self, survivors: list[Candidate],
                           outcomes: dict[str, WorkerOutcome], glog) -> Candidate | None:
        """Walk survivors high-score-first; return the first whose worker reported held-out pass.

        Identical selection rule to `Supervisor._first_revalidated` — the only difference is that
        the held-out verdict was computed in the worker (it had the filesystem) and read back off
        the outcome, rather than run here. The guard is unchanged: a lucky top scorer that fails
        held-out is skipped, so it never becomes the seed.
        """
        for candidate in survivors:
            outcome = outcomes.get(candidate.task.get("id"))
            passed = bool(outcome and outcome.revalidated)
            glog.info("revalidate", branch=candidate.branch,
                      score=round(candidate.score, 4) if candidate.score != float("-inf") else None,
                      passed=passed)
            if passed:
                return candidate
        return None


# --------------------------------------------------------------------------------------------
# Repo runners — the container's task runner. make_repo_runner works on ANY repo; the demo
# runner is a thin wrapper that pins it to the bundled demo-repo.
# --------------------------------------------------------------------------------------------
PRICING_GOAL = ("Implement line_total in pricing.py so a 10% bulk discount applies at "
                "quantity >= 10, per PROMPT.md.")

_CORRECT_PRICING = '''\
"""Line-item pricing with a bulk discount."""


def line_total(unit_price, quantity):
    subtotal = unit_price * quantity
    if quantity >= 10:
        subtotal *= 0.9
    return round(subtotal, 2)
'''


def _demo_src() -> Path:
    """Locate the bundled demo-repo (env path in the container, else the source checkout)."""
    env = os.environ.get("LOOPKIT_DEMO_REPO")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "examples" / "demo-repo"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


def _source_origin(src: str) -> str | None:
    """The `origin` push URL of a local source repo, so a clone can push back to the real forge."""
    path = Path(src).expanduser()
    if not path.exists():
        return None                         # src is itself a URL — the clone already points at it
    out = _git(path, "remote", "get-url", "origin", check=False)
    return out.stdout.strip() or None if out.returncode == 0 else None


def _prepare_repo(src: str, scratch: Path, *, mode: str) -> Path:
    """Materialise the target repo in the worker's own filesystem (the physical isolation).

    `mode="clone"` (real repos / URLs): `git clone src`, then rewire `origin` to the *source's*
    origin so a later push reaches the real forge — a local clone otherwise points origin at the
    local path. `mode="copy"` (the bundled demo-repo, which isn't its own git repo): copy + git
    init + a seed commit.
    """
    repo = scratch / "work"
    if mode == "copy":
        shutil.copytree(src, repo)
        _git(repo, "init", "-q")
        _git(repo, "branch", "-m", "main")
        _git(repo, "config", "user.email", "fleet@loopkit")
        _git(repo, "config", "user.name", "loopkit-fleet")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
    else:
        _git(scratch, "clone", "--quiet", src, str(repo))
        origin = _source_origin(src)
        if origin:
            _git(repo, "remote", "set-url", "origin", origin)
        _git(repo, "config", "user.email", "loopkit@local")
        _git(repo, "config", "user.name", "loopkit")
    return repo


def _grade(result: RunResult) -> tuple[float, bool]:
    """Coarse selection score + held-out verdict from a terminal — enough for the Ch 9 guard.

    DONE = both gates passed (1.0, revalidated). overfit = fit the visible tests but failed
    held-out (1.0, NOT revalidated — the inflated candidate). anything else = 0. A finer score
    (e.g. fraction of held-out tests passing) is a drop-in here.
    """
    if result.reason is StopReason.DONE:
        return 1.0, True
    if result.overfit:
        return 1.0, False
    return 0.0, False


def make_repo_runner(repo_source: str, *, gate_iteration: str, gate_acceptance: str,
                     protected_paths=("tests/",), adapter: str = "claude-code", max_iter: int = 8,
                     mode: str = "clone", agent_factory: "Callable[[dict], Agent] | None" = None,
                     remote: RemoteConfig | None = None,
                     branch_prefix: str = "loopkit") -> TaskRunner:
    """A `TaskRunner` for an arbitrary repo: materialise it, run the loop on the task's branch,
    grade the terminal, and (if `remote.enabled`) push + open a PR.

    This is the generalisation of the demo runner: the gates, protected paths, adapter, and repo
    source are parameters instead of the pricing constants, and the goal comes from each task (an
    issue body, a CLI goal, …). The agent defaults to the configured adapter; pass `agent_factory`
    to script it (the demo does this for a token-free solve).
    """
    remote = remote or RemoteConfig()

    def run_task(task: dict) -> WorkerOutcome:
        task_id = str(task["id"])
        branch = task.get("branch") or f"{branch_prefix}/run-{task_id}"
        scratch = Path(tempfile.mkdtemp(prefix="loopkit-worker-"))
        try:
            repo = _prepare_repo(repo_source, scratch, mode=mode)
            cfg = Config(
                goal=task.get("goal") or "(no goal provided)", repo=str(repo), branch=branch,
                gate=GateConfig(iteration=gate_iteration, acceptance=gate_acceptance),
                agent=AgentConfig(adapter=adapter, max_cost_usd=5.0),
                stops=StopsConfig(max_iter=max_iter, no_progress_after=3),
                safety=SafetyConfig(protected_paths=list(protected_paths), require_clean_tree=False,
                                    allow_branches=[f"{branch_prefix}/*"]),
                remote=remote)
            agent = agent_factory(task) if agent_factory else build_agent(cfg.agent)
            result = run_loop(cfg, agent, iteration_gate=ShellGate(gate_iteration),
                              acceptance_gate=ShellGate(gate_acceptance))
            score, revalidated = _grade(result)
            # Outward edge: a solved branch is pushed + (optionally) a PR opened, only when the
            # repo's [remote] is enabled. The issue number rides through so the PR closes it.
            if cfg.remote.enabled and result.reason is StopReason.DONE:
                from .remote import sync_done
                sync = sync_done(cfg, repo, title=f"loopkit: {task.get('title') or task_id}",
                                 issue=task.get("issue"))
                get_logger("worker").bind(task=task_id).info(
                    "remote.sync", pushed=sync["pushed"], pr=sync["pr_url"] or "-")
            return WorkerOutcome(task_id=task_id, branch=branch, reason=result.reason.value,
                                 iterations=result.iterations, cost_usd=result.cost_usd,
                                 overfit=result.overfit, score=score, revalidated=revalidated)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    return run_task


def _demo_agent(adapter: str) -> Agent:
    """`mock` solves the pricing bug with no tokens (the cluster smoke test); else the real adapter."""
    if adapter == "mock":
        def write_solution(workspace: Path) -> str:
            (workspace / "pricing.py").write_text(_CORRECT_PRICING)
            return "wrote pricing.py"
        return MockAgent(behaviors=[write_solution])
    return build_agent(AgentConfig(adapter=adapter))


def make_demo_runner(*, adapter: str = "mock", max_iter: int = 6) -> TaskRunner:
    """The teaching runner: `make_repo_runner` pinned to the bundled demo-repo + its pytest gates.

    `copy` mode (demo-repo isn't its own git repo) and a scripted mock agent that solves the
    pricing bug token-free, so the fleet goes green on `tilt up` without credentials.
    """
    py = sys.executable
    return make_repo_runner(
        str(_demo_src()), mode="copy", adapter=adapter, max_iter=max_iter,
        gate_iteration=f"{py} -m pytest tests/seen -q",
        gate_acceptance=f"{py} -m pytest tests/holdout -q",
        protected_paths=("tests/",), branch_prefix="loopkit",
        agent_factory=lambda task: _demo_agent(adapter))
