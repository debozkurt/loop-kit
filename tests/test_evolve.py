"""Evolutionary-strategy tests: the selection-inflation guard is the thing under test.

Best-of-N is itself a way to overfit — pick the top of N noisy candidates and the winner's
score is inflated by luck, not just skill (the winner's curse). The defence is the Ch 9 move
applied at the fleet scale: re-validate the kept winner on a held-out gate it never competed
on. These tests prove a candidate that games the *selection* score gets caught by that gate,
and that only a validated winner reseeds the next generation. MockAgent + CallableGate keep it
deterministic and token-free.
"""
from __future__ import annotations

from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig
from loopkit.extensions.orchestrate import Supervisor, remove_worktree
from loopkit.gate import CallableGate


def _base_config(repo: Path) -> Config:
    return Config(goal="placeholder — evolve sets the real goal", repo=str(repo),
                  branch="loopkit/run", gate=GateConfig(iteration="true"))


def _writes(name: str, content: str = "ok"):
    def behavior(workspace: Path) -> str:
        (workspace / name).write_text(content)
        return f"wrote {name}"
    return behavior


def _evolve(repo: Path, *, kinds, selection, holdout, generations=1, keep=2,
            keep_worktrees=False):
    """Run evolve with one synthetic candidate per `kind` and the given score/holdout maps."""
    def candidate_task(base_task, generation, candidate, seed_branch):
        task = dict(base_task)
        task["slug"] = f"g{generation}-c{candidate}"
        task["kind"] = kinds[candidate]
        task["generation"] = generation
        return task

    def make_agent(task):
        return MockAgent(behaviors=[_writes("solution.py", task["kind"])])

    def make_gates(task, workspace):
        gate = CallableGate(lambda ws: (ws / "solution.py").exists())
        return gate, gate

    def score(task, workspace):
        return selection[task["kind"]]

    def revalidate(task, workspace):
        passes = holdout[task["kind"]]
        return CallableGate(lambda ws: passes)

    supervisor = Supervisor(_base_config(repo), make_agent=make_agent, make_gates=make_gates,
                            max_workers=4, keep_worktrees=keep_worktrees)
    result = supervisor.evolve({"goal": "implement solution.py"}, generations=generations,
                               population=len(kinds), keep=keep, score=score,
                               revalidate=revalidate, candidate_task=candidate_task)
    return result


def test_selection_inflation_is_caught_by_revalidation(git_repo: Path):
    # 'overfit' games the selection score (top of the heap) but fails the held-out gate;
    # 'genuine' scores lower yet generalizes. The guard must overrule the top scorer.
    result = _evolve(
        git_repo,
        kinds=["overfit", "genuine", "weak", "broken"],
        selection={"overfit": 1.0, "genuine": 0.9, "weak": 0.5, "broken": 0.1},
        holdout={"overfit": False, "genuine": True, "weak": True, "broken": False},
        keep=2,
    )
    gen = result.generations[0]
    assert gen.survivors[0].task["kind"] == "overfit"     # highest selection score...
    assert gen.inflated is True                            # ...but it failed re-validation
    assert result.winner is not None
    assert result.winner.task["kind"] == "genuine"        # the validated runner-up wins
    assert result.inflation_caught is True


def test_top_scorer_that_validates_is_the_winner(git_repo: Path):
    # No inflation: the top selection score also passes the held-out gate, so it just wins.
    result = _evolve(
        git_repo,
        kinds=["strong", "ok"],
        selection={"strong": 0.9, "ok": 0.6},
        holdout={"strong": True, "ok": True},
        keep=2,
    )
    gen = result.generations[0]
    assert gen.survivors[0].task["kind"] == "strong"
    assert gen.inflated is False
    assert result.winner.task["kind"] == "strong"
    assert result.inflation_caught is False


def test_no_winner_when_every_survivor_overfits(git_repo: Path):
    # Every kept survivor fails re-validation -> nothing is confirmed, nothing reseeds.
    result = _evolve(
        git_repo,
        kinds=["lucky", "luckier"],
        selection={"lucky": 0.8, "luckier": 0.9},
        holdout={"lucky": False, "luckier": False},
        keep=2,
    )
    assert result.winner is None
    assert result.generations[0].inflated is True
    assert result.inflation_caught is True


def test_only_validated_winner_reseeds_next_generation(git_repo: Path):
    # Gen 0's confirmed winner writes a marker; gen 1 branches off that winner, so every gen-1
    # worktree starts with the marker already present — the code carried forward (tree-level
    # reseed). Verified with kept worktrees, then cleaned up.
    def candidate_task(base_task, generation, candidate, seed_branch):
        task = dict(base_task)
        task["slug"] = f"g{generation}-c{candidate}"
        task["generation"] = generation
        task["candidate"] = candidate
        return task

    def gen0_behavior(workspace: Path) -> str:
        # Both files in one tick: the loop reaches DONE the moment the gate (solution.py) passes,
        # so a marker written on a *later* tick would never land. The marker must ride along now.
        (workspace / "solution.py").write_text("g0")
        (workspace / "carried.py").write_text("from-g0")
        return "wrote g0 solution + marker"

    def make_agent(task):
        if task["generation"] == 0:
            # The winner (candidate 0, top score below) leaves a marker that should carry forward.
            return MockAgent(behaviors=[gen0_behavior])
        return MockAgent(behaviors=[_writes("solution.py", "g1")])

    def make_gates(task, workspace):
        gate = CallableGate(lambda ws: (ws / "solution.py").exists())
        return gate, gate

    def score(task, workspace):
        return 1.0 - task["candidate"] * 0.1          # candidate 0 always ranks first

    def revalidate(task, workspace):
        return CallableGate(lambda ws: True)          # everyone validates; reseed always advances

    supervisor = Supervisor(_base_config(git_repo), make_agent=make_agent, make_gates=make_gates,
                            max_workers=3, keep_worktrees=True)
    result = supervisor.evolve({"goal": "build the feature"}, generations=2, population=2,
                               keep=1, score=score, revalidate=revalidate,
                               candidate_task=candidate_task)
    root = result.generations[0].candidates[0].worktree.parent
    try:
        assert result.generations[0].confirmed.task["candidate"] == 0
        gen1 = result.generations[1]
        # gen 1 branched off gen 0's winner: the carried marker is present before gen-1 even ran.
        for candidate in gen1.candidates:
            assert (candidate.worktree / "carried.py").exists()
    finally:
        for generation in result.generations:
            for candidate in generation.candidates:
                if candidate.worktree.exists():
                    remove_worktree(git_repo, candidate.worktree)
        import shutil
        import subprocess
        shutil.rmtree(root, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=git_repo, capture_output=True)
