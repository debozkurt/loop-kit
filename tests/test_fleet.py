"""Fleet tests: the queue-driven coordinator + worker, with no cluster and no tokens.

The deployable fleet (Ch 12) replaces in-process futures with a Redis queue and worktree isolation
with container (own-filesystem) isolation. Both substitutions are exercised here without either:
the transport is proved against `fakeredis`, and the coordinator/worker logic against an
`InMemoryQueue` + `MockAgent`-backed runners that genuinely drive `run_loop` in throwaway repos.
The load-bearing claim under test is the same one the in-process `evolve` carries — best-of-N is
re-validated on a held-out gate it never competed on, so a lucky overfit never reseeds.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import fakeredis
import pytest

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, SafetyConfig, StopsConfig
from loopkit.extensions.fleet import (
    Coordinator,
    InMemoryQueue,
    RedisQueue,
    Worker,
    WorkerOutcome,
    make_demo_runner,
    run_workers,
)
from loopkit.gate import CallableGate
from loopkit.loop import run_loop
from loopkit.stops import StopReason

# Four attempts at one goal, mirroring the Ch 11 scenario: the 'memorizer' tops the selection
# score yet fails the held-out check — exactly the candidate best-of-N is most likely to crown.
SELECTION = {"memorizer": 1.0, "solver": 0.9, "partial": 0.6, "broken": 0.2}
HELD_OUT = {"memorizer": False, "solver": True, "partial": True, "broken": False}


# --------------------------------------------------------------------------------------------
# Helpers: a real run_loop runner over throwaway repos = the worker's container isolation.
# --------------------------------------------------------------------------------------------
def _seed_repo(path: Path) -> Path:
    """A fresh git repo on `main` with one commit — a worker pod's own filesystem, in miniature."""
    path.mkdir(parents=True)
    for args in (("init", "-q"), ("branch", "-m", "main"),
                 ("config", "user.email", "t@loopkit"), ("config", "user.name", "loopkit-test")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True)
    return path


def _loop_runner(base: Path, repos: dict[str, Path]):
    """A TaskRunner that drives run_loop in a fresh repo per task (its own isolated tree).

    A MockAgent writes the task's file on tick 1; a CallableGate passes once it exists. Each task
    gets its own repo under `base`, recorded in `repos` so the test can prove the work is physically
    isolated — task A's file lands only in A's repo, never in B's.
    """
    def run_task(task: dict) -> WorkerOutcome:
        repo = _seed_repo(base / task["id"])
        repos[task["id"]] = repo
        cfg = Config(goal=task["goal"], repo=str(repo), branch=task["branch"],
                     gate=GateConfig(iteration="true"),
                     stops=StopsConfig(max_iter=task.get("max_iter", 4), no_progress_after=2),
                     safety=SafetyConfig(require_clean_tree=False))
        behaviors = [] if task.get("starve") else [_write(task["file"])]
        gate = CallableGate(lambda ws: (ws / task["file"]).exists())
        result = run_loop(cfg, MockAgent(behaviors=behaviors),
                          iteration_gate=gate, acceptance_gate=gate)
        return WorkerOutcome(task_id=task["id"], branch=task["branch"], reason=result.reason.value,
                             iterations=result.iterations, cost_usd=result.cost_usd,
                             overfit=result.overfit)
    return run_task


def _write(name: str):
    def behavior(workspace: Path) -> str:
        (workspace / name).write_text("ok")
        return f"wrote {name}"
    return behavior


def _evolve_runner():
    """A TaskRunner that reports a candidate's selection score and in-worker held-out verdict."""
    def run_task(task: dict) -> WorkerOutcome:
        kind = task["kind"]
        return WorkerOutcome(task_id=task["id"], branch=task["branch"], reason="done",
                             iterations=1, cost_usd=0.5,
                             score=SELECTION[kind], revalidated=HELD_OUT[kind])
    return run_task


def _evolve_candidate(base_task: dict, generation: int, candidate: int, seed_branch):
    kinds = ["memorizer", "solver", "partial", "broken"]
    task = dict(base_task)
    task["slug"] = f"g{generation}-c{candidate}"
    task["kind"] = kinds[candidate]
    task["branch"] = f"loopkit/run-g{generation}-c{candidate}"
    if seed_branch:
        task["seed_branch"] = seed_branch
    return task


def _drain(coordinator_call, workers_threads):
    """Stop the worker threads after the coordinator has collected, then join them."""
    workers, threads = workers_threads
    for worker in workers:
        worker.stop()
    for thread in threads:
        thread.join(timeout=5)


# --------------------------------------------------------------------------------------------
# Wire format + transport
# --------------------------------------------------------------------------------------------
def test_worker_outcome_json_round_trip():
    # Fan-out outcome: score/revalidated are absent.
    fan = WorkerOutcome(task_id="t1", branch="loopkit/run-t1", reason="done", iterations=2,
                        cost_usd=1.5, overfit=False)
    assert WorkerOutcome.from_json(fan.to_json()) == fan
    # Evolve outcome: the held-out verdict survives the round-trip (it's the guard's signal).
    evo = WorkerOutcome(task_id="g0-c0", branch="b", reason="done", score=0.9, revalidated=True)
    back = WorkerOutcome.from_json(evo.to_json())
    assert back == evo and back.revalidated is True


def test_worker_outcome_to_run_result_maps_terminal_and_crash():
    done = WorkerOutcome(task_id="x", branch="b", reason="done", iterations=3, cost_usd=2.0)
    rr = done.to_run_result()
    assert rr is not None and rr.reason is StopReason.DONE and rr.iterations == 3
    # A crashed worker has no terminal — mirrors WorkerResult.result is None in-process.
    assert WorkerOutcome(task_id="x", branch="b", reason="error", error="boom").to_run_result() is None


def test_redis_queue_round_trips_tasks_and_results():
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    queue = RedisQueue(client)
    queue.push_task({"id": "a", "goal": "g1"})
    queue.push_task({"id": "b", "goal": "g2"})
    assert queue.pending() == 2
    # FIFO: first pushed is first popped (LPUSH + BRPOP).
    assert queue.pop_task(timeout=1)["id"] == "a"
    assert queue.pop_task(timeout=1)["id"] == "b"
    assert queue.pop_task(timeout=1) is None            # empty -> timeout -> None

    queue.put_result(WorkerOutcome(task_id="a", branch="loopkit/run-a", reason="done"))
    got = queue.get_result("a")
    assert got is not None and got.done and got.branch == "loopkit/run-a"
    assert queue.get_result("missing") is None


# --------------------------------------------------------------------------------------------
# Fan-out (Coordinator.run_fleet)
# --------------------------------------------------------------------------------------------
def test_fan_out_over_in_memory_queue_reaches_done_isolated(tmp_path: Path):
    repos: dict[str, Path] = {}
    queue = InMemoryQueue()
    runner = _loop_runner(tmp_path, repos)
    wt = run_workers(queue, runner, count=3, poll_timeout=0.05)
    try:
        tasks = [{"slug": s, "branch": f"loopkit/run-{s}", "goal": f"feature {s}",
                  "file": f"feature_{s}.py"} for s in ("a", "b", "c")]
        fleet = Coordinator(queue, collect_timeout=10).run_fleet(tasks)
    finally:
        _drain(None, wt)

    assert len(fleet.done) == 3 and not fleet.failed
    assert {w.branch for w in fleet.done} == {"loopkit/run-a", "loopkit/run-b", "loopkit/run-c"}
    # Physical isolation: each task's file is in its OWN repo and nowhere else.
    for slug in ("a", "b", "c"):
        assert (repos[slug] / f"feature_{slug}.py").exists()
        for other in ("a", "b", "c"):
            if other != slug:
                assert not (repos[other] / f"feature_{slug}.py").exists()


def test_fan_out_over_fakeredis_end_to_end(tmp_path: Path):
    # Prove the real wire path: separate Redis connections (the worker-steal dispatcher) + the
    # coordinator polling the results hash, all over fakeredis sharing one server.
    server = fakeredis.FakeServer()
    repos: dict[str, Path] = {}
    runner = _loop_runner(tmp_path, repos)

    workers, threads = [], []
    for i in range(3):
        q = RedisQueue(fakeredis.FakeStrictRedis(server=server, decode_responses=True))
        worker = Worker(q, runner, name=f"w{i}", poll_timeout=0.1)
        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        workers.append(worker)
        threads.append(thread)

    coord_q = RedisQueue(fakeredis.FakeStrictRedis(server=server, decode_responses=True))
    try:
        tasks = [{"slug": s, "branch": f"loopkit/run-{s}", "goal": f"feature {s}",
                  "file": f"feature_{s}.py"} for s in ("a", "b", "c")]
        fleet = Coordinator(coord_q, collect_timeout=10).run_fleet(tasks)
    finally:
        _drain(None, (workers, threads))

    assert len(fleet.done) == 3
    assert {w.branch for w in fleet.done} == {"loopkit/run-a", "loopkit/run-b", "loopkit/run-c"}


def test_one_crashing_worker_is_contained(tmp_path: Path):
    repos: dict[str, Path] = {}
    base_runner = _loop_runner(tmp_path, repos)

    def runner(task: dict) -> WorkerOutcome:
        if task["slug"] == "b":
            raise ValueError("synthetic worker crash")     # the pod's task raises mid-run
        return base_runner(task)

    queue = InMemoryQueue()
    wt = run_workers(queue, runner, count=3, poll_timeout=0.05)
    try:
        tasks = [{"slug": s, "branch": f"loopkit/run-{s}", "goal": f"feature {s}",
                  "file": f"feature_{s}.py"} for s in ("a", "b", "c")]
        fleet = Coordinator(queue, collect_timeout=10).run_fleet(tasks)
    finally:
        _drain(None, wt)

    # The crash is contained to its own outcome; the other two still reach done.
    assert {w.branch for w in fleet.done} == {"loopkit/run-a", "loopkit/run-c"}
    crashed = next(w for w in fleet.failed if w.task["slug"] == "b")
    assert crashed.result is None and "synthetic worker crash" in (crashed.error or "")


def test_no_progress_worker_fails_without_sinking_fleet(tmp_path: Path):
    repos: dict[str, Path] = {}
    queue = InMemoryQueue()
    wt = run_workers(queue, _loop_runner(tmp_path, repos), count=3, poll_timeout=0.05)
    try:
        # Task b's agent never writes its file -> its gate never passes -> NO_PROGRESS.
        tasks = [{"slug": s, "branch": f"loopkit/run-{s}", "goal": f"feature {s}",
                  "file": f"feature_{s}.py", "max_iter": 6, "starve": s == "b"}
                 for s in ("a", "b", "c")]
        fleet = Coordinator(queue, collect_timeout=10).run_fleet(tasks)
    finally:
        _drain(None, wt)

    assert {w.branch for w in fleet.done} == {"loopkit/run-a", "loopkit/run-c"}
    stuck = next(w for w in fleet.failed if w.task["slug"] == "b")
    assert stuck.result is not None and stuck.result.reason is StopReason.NO_PROGRESS


def test_empty_fleet_returns_empty_result():
    fleet = Coordinator(InMemoryQueue()).run_fleet([])
    assert fleet.workers == [] and fleet.done == [] and fleet.failed == []


def test_collect_timeout_marks_unreported_tasks_failed():
    # No workers running, so nothing reports -> the coordinator gives up at the deadline.
    queue = InMemoryQueue()
    fleet = Coordinator(queue, collect_timeout=0.3, poll_interval=0.05).run_fleet(
        [{"slug": "a", "branch": "loopkit/run-a", "goal": "g", "file": "a.py"}])
    assert len(fleet.failed) == 1
    assert fleet.workers[0].result is None and "timeout" in (fleet.workers[0].error or "")


# --------------------------------------------------------------------------------------------
# Evolutionary search — the Ch 9 selection-inflation guard at fleet scale
# --------------------------------------------------------------------------------------------
def test_evolve_catches_selection_inflation_over_the_queue():
    queue = InMemoryQueue()
    wt = run_workers(queue, _evolve_runner(), count=4, poll_timeout=0.05)
    try:
        result = Coordinator(queue, collect_timeout=10).evolve(
            {"goal": "Implement solution"}, generations=1, population=4, keep=2,
            candidate_task=_evolve_candidate)
    finally:
        _drain(None, wt)

    gen = result.generations[0]
    # The top selection score is the memorizer (1.0) — and it's a survivor...
    assert gen.survivors[0].task["kind"] == "memorizer"
    # ...but re-validation on the held-out gate confirms the solver (0.9) instead.
    assert gen.confirmed is not None and gen.confirmed.task["kind"] == "solver"
    assert gen.inflated is True
    assert result.winner.task["kind"] == "solver"
    assert result.inflation_caught is True


# --------------------------------------------------------------------------------------------
# The container's real task runner — the demo-repo job, solved with no tokens
# --------------------------------------------------------------------------------------------
def test_demo_runner_solves_pricing_end_to_end():
    """The actual worker-pod runner (clone demo-repo, run_loop, real pytest gates), mock adapter.

    Proves the path `tilt up` exercises: the mock agent writes the correct pricing.py, both real
    gates pass, the worker reports DONE with score 1.0 / revalidated True — all without a token.
    """
    queue = InMemoryQueue()
    wt = run_workers(queue, make_demo_runner(adapter="mock", max_iter=4), count=2, poll_timeout=0.05)
    try:
        tasks = [{"slug": f"t{i}", "branch": f"loopkit/run-t{i}", "goal": "solve pricing"}
                 for i in range(2)]
        fleet = Coordinator(queue, collect_timeout=60).run_fleet(tasks)
    finally:
        _drain(None, wt)
    assert len(fleet.done) == 2 and not fleet.failed
    assert all(w.result.reason is StopReason.DONE for w in fleet.workers)


def test_evolve_reseeds_the_validated_winner_across_generations():
    queue = InMemoryQueue()
    wt = run_workers(queue, _evolve_runner(), count=4, poll_timeout=0.05)
    try:
        result = Coordinator(queue, collect_timeout=10).evolve(
            {"goal": "Implement solution"}, generations=2, population=4, keep=2,
            candidate_task=_evolve_candidate)
    finally:
        _drain(None, wt)

    assert len(result.generations) == 2
    # Every generation confirms the solver; the lucky memorizer never reseeds.
    for gen in result.generations:
        assert gen.confirmed is not None and gen.confirmed.task["kind"] == "solver"
        assert gen.inflated is True
    assert result.winner.task["kind"] == "solver"
