"""ACI + two-oracle-gate lessons applied (docs/part-iii-prior-art.md).

Three field-validated improvements, each token-free:
- Edit-time validation: `write_file` refuses a syntactically-broken edit at the tool boundary
  (SWE-agent's ACI guardrail), so the bad state never lands.
- Shaped gate feedback: a failing gate's output is surfaced as high-signal, budget-bounded feedback
  instead of a blind tail (Anthropic, *Writing tools for agents*).
- The two-oracle gate: DONE requires the held-out acceptance gate (the fix works) AND an optional
  held-out regression gate (previously-passing behavior preserved) — SWE-bench's FAIL_TO_PASS +
  PASS_TO_PASS. None-safe: no regression gate ⇒ acceptance alone certifies (exact prior behavior).
"""
from __future__ import annotations

import sys
from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, StopsConfig
from loopkit.executor import (
    LocalToolExecutor,
    _WorkspaceTools,
    shape_failure_output,
    validate_syntax,
)
from loopkit.gate import CallableGate
from loopkit.loop import run_loop
from loopkit.stops import StopReason


# --- edit-time validation (SWE-agent ACI guardrail) -----------------------------------------
def test_write_file_rejects_broken_python(tmp_path: Path):
    tools = _WorkspaceTools(tmp_path)
    out, is_error = tools.dispatch("write_file", {"path": "bad.py", "content": "def f(:\n    pass\n"})
    assert is_error and "REJECTED" in out
    assert not (tmp_path / "bad.py").exists()              # the broken edit never landed


def test_write_file_accepts_valid_python(tmp_path: Path):
    tools = _WorkspaceTools(tmp_path)
    out, is_error = tools.dispatch("write_file", {"path": "ok.py", "content": "def f():\n    return 1\n"})
    assert not is_error and (tmp_path / "ok.py").read_text().startswith("def f")


def test_write_file_rejects_broken_json(tmp_path: Path):
    tools = _WorkspaceTools(tmp_path)
    out, is_error = tools.dispatch("write_file", {"path": "cfg.json", "content": "{not: valid,}"})
    assert is_error and not (tmp_path / "cfg.json").exists()


def test_write_file_unguarded_language_writes_as_is(tmp_path: Path):
    # We only guard what we can parse cheaply; a .txt with python-looking junk still writes.
    tools = _WorkspaceTools(tmp_path)
    out, is_error = tools.dispatch("write_file", {"path": "notes.txt", "content": "def f(:"})
    assert not is_error and (tmp_path / "notes.txt").exists()


def test_validate_syntax_allows_empty_content():
    assert validate_syntax("x.py", "") is None             # empty module is valid
    assert validate_syntax("x.json", "   ") is None        # empty file is a legitimate intermediate


# --- shaped gate feedback (Writing tools for agents) ----------------------------------------
def test_shape_failure_short_output_is_unchanged():
    # The prior contract: short output passes through verbatim (a marker the loop may key on survives).
    assert shape_failure_output("boom-diagnostics", budget=2000) == "boom-diagnostics"


def test_shape_failure_surfaces_early_failures_in_a_long_log():
    early = "AssertionError: widget is broken at the boundary\n"
    text = early + ("benign filler line\n" * 5000)         # the failure is far above a blind tail
    shaped = shape_failure_output(text, budget=400)
    assert "widget is broken" in shaped                    # surfaced despite truncation
    assert "key failures" in shaped
    assert len(shaped) <= 400 * 2 + 200                    # bounded to ~2×budget


def test_run_gate_shapes_a_long_failing_output(tmp_path: Path):
    cmd = (f"{sys.executable} -c \"print('AssertionError early'); print('x'*6000); "
           f"import sys; sys.exit(1)\"")
    result = LocalToolExecutor().run_gate(cmd, tmp_path, tail=500)
    assert result.passed is False
    assert "AssertionError early" in (result.feedback or "")   # the early failure isn't lost


# --- the two-oracle gate (FAIL_TO_PASS + PASS_TO_PASS) ---------------------------------------
def _cfg(repo: Path, **gate) -> Config:
    return Config(goal="do it", repo=str(repo), branch="loopkit/test",
                  gate=GateConfig(iteration="true", **gate),
                  stops=StopsConfig(max_iter=3, no_progress_after=99))


def _solver() -> MockAgent:
    return MockAgent(behaviors=[lambda ws: (ws / "solution.txt").write_text("x")])


def test_regression_gate_blocks_done_when_it_fails(git_repo: Path):
    # Acceptance passes (the target is fixed) but the regression oracle fails ⇒ never DONE.
    result = run_loop(_cfg(git_repo), _solver(),
                      iteration_gate=CallableGate(lambda ws: True),
                      acceptance_gate=CallableGate(lambda ws: True),
                      regression_gate=CallableGate(lambda ws: False))
    assert result.reason is StopReason.ITERATION_CAP


def test_regression_gate_allows_done_when_it_passes(git_repo: Path):
    result = run_loop(_cfg(git_repo), _solver(),
                      iteration_gate=CallableGate(lambda ws: True),
                      acceptance_gate=CallableGate(lambda ws: True),
                      regression_gate=CallableGate(lambda ws: True))
    assert result.reason is StopReason.DONE


def test_no_regression_gate_is_exact_prior_behavior(git_repo: Path):
    # None regression gate + no config.gate.regression ⇒ acceptance alone certifies DONE.
    result = run_loop(_cfg(git_repo), _solver(),
                      iteration_gate=CallableGate(lambda ws: True),
                      acceptance_gate=CallableGate(lambda ws: True))
    assert result.reason is StopReason.DONE


def test_regression_gate_from_config_shell_command(git_repo: Path):
    # Configured the loopkit.toml way: [gate] regression = "false" (a failing held-out check).
    cfg = _cfg(git_repo, acceptance=None, regression="false")
    result = run_loop(cfg, _solver(), iteration_gate=CallableGate(lambda ws: True))
    assert result.reason is StopReason.ITERATION_CAP        # acceptance AlwaysPass, regression fails
