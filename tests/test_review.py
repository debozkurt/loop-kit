"""Continuous-review tests: a clean review gates done; a failing one loops fix -> re-review.

Driven by MockAgent + CallableGate + CallableReviewHook, deterministic and token-free. The
claim under test (Ch 8): passing the gates is necessary but not sufficient — the review must
also be clean before the loop declares done, and a failing review's findings reach the next
tick so the agent can fix them.
"""
from __future__ import annotations

from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import AgentConfig, Config, GateConfig, ReviewConfig, StopsConfig
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


def test_review_runs_when_agent_self_commits(git_repo: Path):
    # A CLI agent (claude-code/codex) often commits its OWN work, so loopkit's commit_progress is a
    # no-op (committed=False) even though HEAD advanced. The review must still fire on that diff —
    # otherwise a self-committing agent silently skips the review gate (the exact bug this guards).
    import subprocess

    def write_and_commit(workspace: Path) -> str:
        (workspace / "solution.py").write_text("ok")
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "agent self-commit"], cwd=workspace, check=True)
        return "wrote + committed solution.py"

    seen: list[str] = []

    class CountingReview:
        def review(self, workspace: Path, commit_message: str) -> GateResult:
            seen.append(commit_message)
            return GateResult(True, None)

    gate = CallableGate(lambda ws: (ws / "solution.py").exists())
    result = run_loop(_config(git_repo), MockAgent(behaviors=[write_and_commit]),
                      iteration_gate=gate, acceptance_gate=gate, review_hook=CountingReview())
    assert result.reason is StopReason.DONE
    assert len(seen) == 1          # review fired despite loopkit's own commit being a no-op


def test_review_config_resolution():
    # No command: `resolved()` (the command-only view) is None — the built-in judge has no shell
    # command; on-ness now lives on decide().kind, never on the command (see the decision test).
    off = ReviewConfig()
    assert off.resolved() is None
    assert off.resolved(override="cmd.sh") == "cmd.sh"
    # Command set: runs BY DEFAULT (opt-out). Override wins; --no-review disables.
    on = ReviewConfig(command="judge.sh")
    assert on.resolved() == "judge.sh"                         # default-on once a command is set
    assert on.resolved(override="other.sh") == "other.sh"     # explicit override wins
    assert on.resolved(disabled=True) is None                 # --no-review beats the default
    assert on.resolved(override="other.sh", disabled=True) is None   # ...and beats an override too
    # enabled=false suppresses the configured command, but an explicit override is strong enough to
    # still run (precedence: --no-review > override > enabled gate > command).
    disabled = ReviewConfig(command="judge.sh", enabled=False)
    assert disabled.resolved() is None
    assert disabled.resolved(override="explicit.sh") == "explicit.sh"


def test_review_decision_carries_reason_and_kind():
    # decide() names WHAT runs (kind) AND why (reason), so callers can LOG the decision (it was
    # previously invisible → silently-off). Review is truly on-by-default: a bare config resolves
    # to the BUILT-IN judge — on with no command — which is exactly why `on` derives from kind,
    # never from `command is not None` (that would render the default judge "off" while it runs).
    default = ReviewConfig().decide()
    assert default.kind == "default" and default.on is True and default.command is None
    assert "built-in judge" in default.reason
    on = ReviewConfig(command="judge.sh").decide()
    assert on.kind == "command" and on.command == "judge.sh" and on.on is True
    assert "[review] command" in on.reason
    ovr = ReviewConfig(command="judge.sh").decide(override="cli.sh")
    assert ovr.kind == "command" and ovr.command == "cli.sh" and ovr.on and "override" in ovr.reason
    no = ReviewConfig(command="judge.sh").decide(disabled=True)
    assert no.kind == "off" and no.command is None and no.on is False and "--no-review" in no.reason
    off_switch = ReviewConfig(command="judge.sh", enabled=False).decide()
    assert off_switch.kind == "off" and off_switch.on is False and "enabled" in off_switch.reason


def test_shell_review_hook_passes_clean_and_fails_dirty(tmp_path: Path):
    # The production primitive: exit 0 is clean. `! grep` inverts — clean when no BUG is found.
    hook = ShellReviewHook("! grep -rn BUG .")
    (tmp_path / "a.py").write_text("all clean here")
    assert hook.review(tmp_path, "msg").passed is True

    (tmp_path / "a.py").write_text("has a BUG in it")
    verdict = hook.review(tmp_path, "msg")
    assert verdict.passed is False
    assert "BUG" in (verdict.feedback or "")     # the grep match is fed back as findings
