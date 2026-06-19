"""Continuous-review tests: a clean review gates done; a failing one loops fix -> re-review.

Driven by MockAgent + CallableGate + CallableReviewHook, deterministic and token-free. The
claim under test (Ch 8): passing the gates is necessary but not sufficient — the review must
also be clean before the loop declares done, and a failing review's findings reach the next
tick so the agent can fix them.
"""
from __future__ import annotations

from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import AgentConfig, Config, GateConfig, StopsConfig
from loopkit.extensions.review import CallableReviewHook, ShellReviewHook
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


def test_review_blocks_done_then_clears_on_fix(git_repo: Path):
    # tick 1 writes code with a banned marker -> review fails (gate would have passed!).
    # tick 2 rewrites it clean -> review passes -> done. The fix->re-review loop in miniature.
    agent = MockAgent(behaviors=[_writes("solution.py", "ok # BUG"),
                                 _writes("solution.py", "ok")])
    always_green = CallableGate(lambda ws: (ws / "solution.py").exists())
    review = CallableReviewHook(lambda ws: "BUG" not in (ws / "solution.py").read_text(),
                                feedback="remove the BUG marker")
    result = run_loop(_config(git_repo), agent, iteration_gate=always_green,
                      acceptance_gate=always_green, review_hook=review)
    assert result.reason is StopReason.DONE
    assert result.iterations == 2          # blocked at tick 1 by review, done at tick 2


def test_clean_review_does_not_block_done(git_repo: Path):
    agent = MockAgent(behaviors=[_writes("solution.py")])
    gate = CallableGate(lambda ws: (ws / "solution.py").exists())
    result = run_loop(_config(git_repo), agent, iteration_gate=gate, acceptance_gate=gate,
                      review_hook=CallableReviewHook(lambda ws: True))
    assert result.reason is StopReason.DONE
    assert result.iterations == 1


def test_unfixable_review_blocks_done_to_the_cap(git_repo: Path):
    # Gates always pass, but the review never clears: done is never reached -> iteration cap.
    behaviors = [_writes(f"f{i}.txt", str(i)) for i in range(10)]   # keep changing -> no NO_PROGRESS
    cfg = _config(git_repo, stops=StopsConfig(max_iter=4, no_progress_after=99))
    result = run_loop(cfg, MockAgent(behaviors=behaviors),
                      iteration_gate=CallableGate(lambda ws: True),
                      acceptance_gate=CallableGate(lambda ws: True),
                      review_hook=CallableReviewHook(lambda ws: False, feedback="never clean"))
    assert result.reason is StopReason.ITERATION_CAP
    assert result.iterations == 4


def test_review_is_skipped_when_no_commit(git_repo: Path):
    # A no-op agent makes no commit, so there's no new diff to review -> the hook isn't called,
    # and the no-progress sensor fires as it would without any hook.
    seen: list[str] = []

    class CountingReview:
        def review(self, workspace: Path, commit_message: str) -> GateResult:
            seen.append(commit_message)
            return GateResult(True, None)

    cfg = _config(git_repo, stops=StopsConfig(max_iter=10, no_progress_after=2))
    result = run_loop(cfg, MockAgent(behaviors=[]),
                      iteration_gate=CallableGate(lambda ws: False), review_hook=CountingReview())
    assert result.reason is StopReason.NO_PROGRESS
    assert seen == []                      # never committed -> never reviewed


def test_no_review_hook_is_unchanged(git_repo: Path):
    # Without a hook, the loop is exactly v1: green gates -> done on tick 1.
    agent = MockAgent(behaviors=[_writes("solution.py")])
    gate = CallableGate(lambda ws: (ws / "solution.py").exists())
    result = run_loop(_config(git_repo), agent, iteration_gate=gate, acceptance_gate=gate)
    assert result.reason is StopReason.DONE
    assert result.iterations == 1


def test_shell_review_hook_passes_clean_and_fails_dirty(tmp_path: Path):
    # The production primitive: exit 0 is clean. `! grep` inverts — clean when no BUG is found.
    hook = ShellReviewHook("! grep -rn BUG .")
    (tmp_path / "a.py").write_text("all clean here")
    assert hook.review(tmp_path, "msg").passed is True

    (tmp_path / "a.py").write_text("has a BUG in it")
    verdict = hook.review(tmp_path, "msg")
    assert verdict.passed is False
    assert "BUG" in (verdict.feedback or "")     # the grep match is fed back as findings
