"""Orchestration tests: fan-out runs many isolated worker loops without stepping on each other.

Driven by MockAgent + CallableGate, so the whole fleet runs deterministically with no
coding-agent binary and no tokens — the same discipline the single-agent course uses (Ch 9),
extended to the supervisor. The isolation claim (Ch 10-12) is the thing under test: each worker
edits in its own worktree/branch, so its files never leak into the main checkout or a sibling.
"""
from __future__ import annotations

from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, StopsConfig
from loopkit.extensions.orchestrate import (
    Supervisor,
    make_worktree,
    remove_worktree,
    run_fleet,
)
from loopkit.gate import CallableGate
from loopkit.stops import StopReason


def _base_config(repo: Path) -> Config:
    return Config(goal="placeholder — each task overrides this", repo=str(repo),
                  branch="loopkit/run", gate=GateConfig(iteration="true"))


def _writes(name: str, content: str = "ok"):
    """A MockAgent behavior that writes `name` in the worker's workspace."""
    def behavior(workspace: Path) -> str:
        (workspace / name).write_text(content)
        return f"wrote {name}"
    return behavior


def _file_agent(task: dict) -> MockAgent:
    """Per-task agent that writes the task's file on its first tick (then is a no-op)."""
    return MockAgent(behaviors=[_writes(task["file"])])


def _file_gates(task: dict, workspace: Path):
    """Iteration + acceptance both pass once the task's file exists in this worktree."""
    gate = CallableGate(lambda ws: (ws / task["file"]).exists())
    return gate, gate


def _branches(repo: Path) -> set[str]:
    import subprocess
    out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                         cwd=repo, capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def test_fan_out_reaches_done_on_isolated_branches(git_repo: Path):
    tasks = [{"goal": f"create feature {s}", "slug": s, "file": f"feature_{s}.py"}
             for s in ("a", "b", "c")]
    fleet = run_fleet(_base_config(git_repo), tasks, make_agent=_file_agent,
                      make_gates=_file_gates, max_workers=3)

    assert len(fleet.done) == 3
    assert not fleet.failed
    # Each worker landed on its own branch off the base branch name.
    assert {w.branch for w in fleet.done} == {"loopkit/run-a", "loopkit/run-b", "loopkit/run-c"}
    assert _branches(git_repo).issuperset({"loopkit/run-a", "loopkit/run-b", "loopkit/run-c"})


def test_workers_are_isolated_from_main_and_each_other(git_repo: Path):
    tasks = [{"goal": f"create feature {s}", "slug": s, "file": f"feature_{s}.py"}
             for s in ("a", "b", "c")]
    run_fleet(_base_config(git_repo), tasks, make_agent=_file_agent, make_gates=_file_gates)

    # The main checkout stays on its original commit and never sees any worker's files: the
    # edits happened in separate worktrees, so nothing leaked across the isolation boundary.
    for name in ("feature_a.py", "feature_b.py", "feature_c.py"):
        assert not (git_repo / name).exists()


def test_worktrees_are_cleaned_up_by_default(git_repo: Path):
    tasks = [{"goal": "feature a", "slug": "a", "file": "feature_a.py"}]
    fleet = run_fleet(_base_config(git_repo), tasks, make_agent=_file_agent,
                      make_gates=_file_gates)
    # Default keep_worktrees=False tears down the checkout; the branch survives.
    assert not fleet.workers[0].worktree.exists()
    assert "loopkit/run-a" in _branches(git_repo)


def test_keep_worktrees_leaves_checkouts(git_repo: Path):
    tasks = [{"goal": "feature a", "slug": "a", "file": "feature_a.py"}]
    fleet = run_fleet(_base_config(git_repo), tasks, make_agent=_file_agent,
                      make_gates=_file_gates, keep_worktrees=True)
    worktree = fleet.workers[0].worktree
    assert worktree.exists()
    assert (worktree / "feature_a.py").exists()    # the work is right there on disk
    remove_worktree(git_repo, worktree)            # clean up after the assertion


def test_one_failing_worker_does_not_sink_the_fleet(git_repo: Path):
    # The middle task's agent never writes its file, so its gate never passes -> NO_PROGRESS.
    def make_agent(task: dict) -> MockAgent:
        return MockAgent(behaviors=[] if task["slug"] == "b" else [_writes(task["file"])])

    tasks = [{"goal": f"feature {s}", "slug": s, "file": f"feature_{s}.py"}
             for s in ("a", "b", "c")]
    cfg = _base_config(git_repo)
    cfg.stops = StopsConfig(max_iter=6, no_progress_after=2)
    fleet = run_fleet(cfg, tasks, make_agent=make_agent, make_gates=_file_gates)

    assert len(fleet.workers) == 3
    assert {w.branch for w in fleet.done} == {"loopkit/run-a", "loopkit/run-c"}
    stuck = next(w for w in fleet.failed if w.task["slug"] == "b")
    assert stuck.result is not None and stuck.result.reason is StopReason.NO_PROGRESS


def test_empty_fleet_returns_empty_result(git_repo: Path):
    fleet = run_fleet(_base_config(git_repo), [], make_agent=_file_agent)
    assert fleet.workers == []
    assert fleet.done == [] and fleet.failed == []


def test_make_worktree_is_isolated_and_branch_survives_removal(git_repo: Path, tmp_path: Path):
    import subprocess
    path = tmp_path / "wt-x"
    make_worktree(git_repo, "loopkit/run-x", path, base="HEAD")
    try:
        (path / "scratch.txt").write_text("isolated edit")
        # The edit is invisible to the main checkout — separate working directory, same repo.
        assert not (git_repo / "scratch.txt").exists()
        assert "loopkit/run-x" in _branches(git_repo)
    finally:
        remove_worktree(git_repo, path)
    # The checkout is gone but the branch remains in the repo.
    assert not path.exists()
    assert "loopkit/run-x" in _branches(git_repo)
    subprocess.run(["git", "worktree", "prune"], cwd=git_repo, capture_output=True)
