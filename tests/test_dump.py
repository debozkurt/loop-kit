"""`--dump-dir`: the per-tick artifact tree, and what it must contain to be worth writing.

Driven by MockAgent + CallableGate, so the whole tick lifecycle runs with no agent binary and no
tokens. The load-bearing assertion is `prompt.diff`: the always-on logs carry `promptLen` and never
the text, so a gate failure arriving as the next tick's input is exactly the thing an operator
cannot see anywhere else. If that file stops showing the feedback block, the artifact tree has lost
its reason to exist.
"""
from __future__ import annotations

import json
from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, PromptConfig, ReviewConfig, StopsConfig
from loopkit.gate import CallableGate, GateResult
from loopkit.loop import run_loop
from loopkit.stops import StopReason


def _config(repo: Path, **overrides) -> Config:
    base = dict(goal="make it pass", repo=str(repo), branch="loopkit/test",
                gate=GateConfig(iteration="true"))
    base.update(overrides)
    return Config(**base)


def _writes(name: str, content: str = "ok"):
    def behavior(workspace: Path) -> str:
        (workspace / name).write_text(content)
        return f"wrote {name}"
    return behavior


def test_no_dump_path_writes_nothing(git_repo: Path, tmp_path: Path):
    # None ⇒ exact prior behavior: the loop must not create the tree, or even the directory.
    out = tmp_path / "artifacts"
    run_loop(_config(git_repo), MockAgent(behaviors=[_writes("a.txt")]),
             iteration_gate=CallableGate(lambda ws: True))
    assert not out.exists()


def test_dump_captures_config_and_anchors(git_repo: Path, tmp_path: Path):
    (git_repo / "PROMPT.md").write_text("# the task\nfix the thing\n")
    (git_repo / "loopkit.toml").write_text('goal = "make it pass"\n')
    out = tmp_path / "run-01"
    run_loop(_config(git_repo, prompt=PromptConfig(anchors=["PROMPT.md"])),
             MockAgent(behaviors=[_writes("a.txt")]),
             iteration_gate=CallableGate(lambda ws: True), dump_path=out)

    cfg = out / "00-config"
    assert json.loads((cfg / "config.json").read_text())["goal"] == "make it pass"
    # The anchors are the durable context every tick rebuilds from — a reader needs them verbatim.
    assert "fix the thing" in (cfg / "PROMPT.md").read_text()
    assert (cfg / "loopkit.toml").is_file()


def test_dump_captures_the_review_rubric(git_repo: Path, tmp_path: Path):
    # review.md says REJECT; the rubric is the standard it was measured against, and the one input
    # the agent never sees (protected path ⇒ absent from every diff.patch too). Without it the
    # dumped tree cannot explain its own verdict.
    (git_repo / "grading").mkdir()
    (git_repo / "grading" / "review-criteria.md").write_text("# rubric\nreject without a worked example\n")
    out = tmp_path / "run-01"
    run_loop(_config(git_repo, review=ReviewConfig(enabled=True, criteria=["grading/review-criteria.md"])),
             MockAgent(behaviors=[_writes("a.txt")]),
             iteration_gate=CallableGate(lambda ws: True), dump_path=out)

    rubric = out / "00-config" / "review-criteria.md"
    assert "reject without a worked example" in rubric.read_text()


def test_prompt_diff_shows_the_feedback_arriving(git_repo: Path, tmp_path: Path):
    # Tick 1 fails the gate with diagnostics; tick 2 must show those diagnostics entering the prompt.
    # This is the deck's whole claim — the closed loop — as one file.
    calls = {"n": 0}

    class _FailsOnce:
        def check(self, workspace: Path) -> GateResult:
            calls["n"] += 1
            if calls["n"] == 1:
                return GateResult(False, "E501 line too long (92 > 79 characters)")
            return GateResult(True, None)

    out = tmp_path / "run-01"
    result = run_loop(_config(git_repo), MockAgent(behaviors=[_writes("a.txt"), _writes("b.txt")]),
                      iteration_gate=_FailsOnce(), dump_path=out)
    assert result.reason is StopReason.DONE

    assert not (out / "tick-01" / "prompt.diff").exists()   # nothing to diff against on tick 1
    diff = (out / "tick-02" / "prompt.diff").read_text()
    assert "E501 line too long" in diff                     # the gate's own words, in the next prompt
    assert "+" in diff and "Feedback from the last attempt" in diff
    # and the feedback file for tick 1 says what got carried forward
    assert "E501" in (out / "tick-01" / "feedback.md").read_text()


def test_dump_records_every_gate_verdict_and_the_terminal(git_repo: Path, tmp_path: Path):
    out = tmp_path / "run-01"
    result = run_loop(_config(git_repo), MockAgent(behaviors=[_writes("a.txt")]),
                      iteration_gate=CallableGate(lambda ws: True),
                      acceptance_gate=CallableGate(lambda ws: True), dump_path=out)

    tick = out / "tick-01"
    assert (tick / "gate.iteration.txt").read_text().startswith("PASS")
    assert (tick / "gate.acceptance.txt").read_text().startswith("PASS")
    assert "wrote a.txt" in (tick / "agent.md").read_text()
    assert "a.txt" in (tick / "diff.patch").read_text()      # the tick's own commit
    run = json.loads((out / "run.json").read_text())
    assert run["reason"] == "done" and run["iterations"] == 1
    assert result.reason is StopReason.DONE


def test_dump_survives_a_tick_that_commits_nothing(git_repo: Path, tmp_path: Path):
    # The mock makes no edits, so nothing commits and the loop stalls on no-progress. The tree must
    # still be written — a stuck run is precisely when someone reads these files.
    out = tmp_path / "run-03"
    result = run_loop(_config(git_repo, stops=StopsConfig(max_iter=5, no_progress_after=2)),
                      MockAgent(), iteration_gate=CallableGate(lambda ws: False), dump_path=out)

    assert result.reason is StopReason.NO_PROGRESS
    assert "no commit this tick" in (out / "tick-01" / "diff.patch").read_text()
    assert json.loads((out / "run.json").read_text())["reason"] == "no_progress"
