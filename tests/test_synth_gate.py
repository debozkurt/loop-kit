"""Oracle synthesis — fail-first (and fail→pass) verification of a proposed held-out gate.

Two layers of test: the **logic** over an injected fake gate-runner (a marker file stands in for
"is the tree fixed?", so every branch — blessed, already-green, unsatisfiable, fix-errored, isolated
— is exercised with no subprocess), and the **real machinery** end-to-end over the bundled demo-repo
with the actual `LocalToolExecutor` (pytest as the oracle, a python one-liner as the reference fix).
The CLI test drives the composed app through `CliRunner` so the exit-code contract (0 blessed, 3 not
real) is proven the way a user hits it. No tokens, no network.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from loopkit.extensions.synth_gate import (ENV_LIVE, FAIL_FIRST, PASS_ON_FIX, OracleVerdict,
                                           oracle_signature, verify_oracle)
from loopkit.gate import GateResult

TS = "2026-07-10T00:00:00+00:00"          # a fixed timestamp (the verdict takes the clock as input)


def _marker_runner(marker: str = "FIXED"):
    """A fake gate runner: the oracle 'passes' iff `marker` exists in the workspace it's run against.

    Stands in for a real oracle that fails on the buggy tree and passes once fixed — the fix commands
    in these tests create the marker. Records each (command, workspace) it saw for assertions.
    """
    seen: list[tuple[str, str]] = []

    def run(command: str, workspace: Path) -> GateResult:
        seen.append((command, str(workspace)))
        fixed = (Path(workspace) / marker).exists()
        return GateResult(fixed, None if fixed else "E   assert 20.0 == 18.0  (boundary not discounted)")

    run.seen = seen        # type: ignore[attr-defined]
    return run


# --- fail-first, in place (the common case, no fix) ----------------------------------------
def test_fail_first_blesses_an_oracle_that_fails_on_the_buggy_tree(tmp_path: Path):
    (tmp_path / "a.txt").write_text("buggy")               # no FIXED marker → the oracle fails
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, run_gate=_marker_runner())
    assert verdict.blessed is True and verdict.isolated is False
    assert [c.name for c in verdict.checks] == [FAIL_FIRST]
    assert verdict.checks[0].ok and verdict.checks[0].expected == "fail"
    # The failing output is kept as evidence — the molder must see *why* it failed.
    assert "boundary" in (verdict.checks[0].evidence or "")


def test_fail_first_refuses_an_already_green_oracle(tmp_path: Path):
    # A runner that always passes = an oracle that's green on the buggy tree. It certifies nothing.
    verdict = verify_oracle("noop", tmp_path, timestamp=TS,
                            run_gate=lambda c, w: GateResult(True, None))
    assert verdict.blessed is False
    assert verdict.checks[0].name == FAIL_FIRST and verdict.checks[0].ok is False
    assert "ALREADY PASSES" in verdict.checks[0].detail
    assert "no output captured" in (verdict.checks[0].evidence or "")   # exited 0 → synthesized note


def test_fail_first_runs_in_place_by_default(tmp_path: Path):
    runner = _marker_runner()
    verify_oracle("run oracle", tmp_path, timestamp=TS, run_gate=runner)
    # Without --fix / --isolate the oracle runs against the caller's tree itself, not a copy.
    assert runner.seen == [("run oracle", str(tmp_path.resolve()))]


# --- pass-on-fix (the gold fail→pass check) -------------------------------------------------
def test_pass_on_fix_blesses_an_oracle_that_flips(tmp_path: Path):
    (tmp_path / "a.txt").write_text("buggy")
    runner = _marker_runner()
    # The fix creates FIXED in the isolated copy → the oracle flips fail→pass.
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, fix="touch FIXED", run_gate=runner)
    assert verdict.blessed is True and verdict.isolated is True
    assert [c.name for c in verdict.checks] == [FAIL_FIRST, PASS_ON_FIX]
    assert all(c.ok for c in verdict.checks)
    # Both checks ran in the SAME throwaway copy (never the caller's tree).
    workspaces = {ws for _, ws in runner.seen}
    assert len(workspaces) == 1 and str(tmp_path) not in workspaces


def test_pass_on_fix_refuses_an_unsatisfiable_oracle(tmp_path: Path):
    (tmp_path / "a.txt").write_text("buggy")
    # A no-op fix leaves the tree buggy → the oracle still fails after it → it doesn't discriminate.
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, fix="true", run_gate=_marker_runner())
    assert verdict.blessed is False
    fix_check = next(c for c in verdict.checks if c.name == PASS_ON_FIX)
    assert fix_check.ok is False and "STILL FAILS" in fix_check.detail


def test_a_reference_fix_that_errors_is_a_failed_check_not_a_crash(tmp_path: Path):
    (tmp_path / "a.txt").write_text("buggy")
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, fix="exit 7", run_gate=_marker_runner())
    assert verdict.blessed is False
    fix_check = next(c for c in verdict.checks if c.name == PASS_ON_FIX)
    assert fix_check.ok is False and "did not apply cleanly" in fix_check.detail and "exit 7" in fix_check.detail


def test_isolate_runs_fail_first_in_a_copy_without_a_fix(tmp_path: Path):
    (tmp_path / "a.txt").write_text("buggy")
    runner = _marker_runner()
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, isolate=True, run_gate=runner)
    assert verdict.blessed is True and verdict.isolated is True
    assert [c.name for c in verdict.checks] == [FAIL_FIRST]           # no fix → still just fail-first
    assert str(tmp_path) not in {ws for _, ws in runner.seen}         # but run in a copy, not in place


def test_clone_mode_materializes_committed_state(git_repo: Path):
    # clone mode must reach a real git repo (the CLI's non-default materialization path).
    runner = _marker_runner()
    verdict = verify_oracle("run oracle", git_repo, timestamp=TS, fix="touch FIXED", mode="clone",
                            run_gate=runner)
    assert verdict.blessed is True and [c.name for c in verdict.checks] == [FAIL_FIRST, PASS_ON_FIX]


def test_unknown_mode_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        verify_oracle("o", tmp_path, timestamp=TS, fix="true", mode="bogus", run_gate=_marker_runner())


# --- provenance: signature + JSON self-description ------------------------------------------
def test_signature_is_stable_and_sensitive():
    assert oracle_signature("o", None, "copy") == oracle_signature("o", None, "copy")
    assert oracle_signature("o", None, "copy") != oracle_signature("o", "git apply x", "copy")
    assert oracle_signature("o", None, "copy") != oracle_signature("o2", None, "copy")
    # The probe key is added only when supplied: pre-probe signatures stay byte-stable, and a
    # different probe is a different certification.
    assert oracle_signature("o", None, "copy", probe=None) == oracle_signature("o", None, "copy")
    assert oracle_signature("o", None, "copy", probe="p") != oracle_signature("o", None, "copy")
    assert oracle_signature("o", None, "copy", probe="p") != oracle_signature("o", None, "copy",
                                                                             probe="p2")


# --- env-liveness probe (Q3): a positive proof the oracle's runner is even alive ------------------
def _probe_aware_runner(probe_cmd: str = "probe", probe_alive: bool = True):
    """A fake runner where the PROBE's fate is scripted and the oracle always 'fails' (buggy tree).

    Stands in for the Wave-A failure class: an oracle exiting non-zero for ENVIRONMENTAL reasons
    (auth down, SIGABRT'd venv) is byte-for-byte indistinguishable, at the exit-code level, from one
    that genuinely reproduces the bug — only the probe's verdict separates the two worlds.
    """
    seen: list[tuple[str, str]] = []

    def run(command: str, workspace: Path) -> GateResult:
        seen.append((command, str(workspace)))
        if command == probe_cmd:
            return GateResult(probe_alive, None if probe_alive
                              else "FATAL: password authentication failed for user \"spacer\"")
        return GateResult(False, "E   assert 20.0 == 18.0  (boundary not discounted)")

    run.seen = seen        # type: ignore[attr-defined]
    return run


def test_probe_pass_records_env_live_and_blesses(tmp_path: Path):
    runner = _probe_aware_runner(probe_alive=True)
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, probe="probe", run_gate=runner)
    assert verdict.blessed and verdict.env_live is True
    assert [c.name for c in verdict.checks] == [ENV_LIVE, FAIL_FIRST]
    assert [c for c, _ in runner.seen] == ["probe", "run oracle"]     # probe ran FIRST


def test_probe_fail_is_env_broken_and_skips_fail_first(tmp_path: Path):
    # The Wave-A regression: an env failure must yield env-broken, NOT a fail-first blessing —
    # and fail-first must not even run (its output would be the same environmental noise).
    runner = _probe_aware_runner(probe_alive=False)
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, probe="probe", run_gate=runner)
    assert not verdict.blessed and verdict.env_live is False
    assert [c.name for c in verdict.checks] == [ENV_LIVE]             # short-circuit: no fail-first
    assert verdict.checks[0].broken and "ENVIRONMENT is broken" in verdict.checks[0].detail
    assert [c for c, _ in runner.seen] == ["probe"]                   # the oracle never ran


def test_probe_runs_inside_the_isolated_copy(tmp_path: Path):
    # Materialization itself can break the env (the observed case: copytree duplicating a
    # non-relocatable venv) — so the probe must run in the COPY, never the pristine source.
    runner = _probe_aware_runner(probe_alive=True)
    verify_oracle("run oracle", tmp_path, timestamp=TS, probe="probe", isolate=True,
                  run_gate=runner)
    probe_ws = runner.seen[0][1]
    assert probe_ws != str(tmp_path.resolve())                        # the copy, not the source
    assert runner.seen[0][1] == runner.seen[1][1]                     # same tree as fail-first


def test_no_probe_is_unprobed_not_failed(tmp_path: Path):
    # Back-compat: probe-less verification behaves exactly as before, honestly marked unprobed.
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, run_gate=_marker_runner())
    assert verdict.blessed and verdict.env_live is None
    assert [c.name for c in verdict.checks] == [FAIL_FIRST]


def test_verdict_json_carries_env_live(tmp_path: Path):
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, probe="probe",
                            run_gate=_probe_aware_runner(probe_alive=False))
    data = json.loads(verdict.to_json())
    assert data["env_live"] is False
    assert [c["name"] for c in data["checks"]] == [ENV_LIVE]


def test_verdict_json_roundtrip_is_self_describing(tmp_path: Path):
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, fix="touch FIXED",
                            run_gate=_marker_runner())
    data = json.loads(verdict.to_json())
    assert data["blessed"] is True and data["timestamp"] == TS
    assert data["signature"] and data["loopkit_version"] and data["fix"] == "touch FIXED"
    assert [c["name"] for c in data["checks"]] == [FAIL_FIRST, PASS_ON_FIX]


# --- end to end over the real LocalToolExecutor + the bundled demo-repo (no tokens) ---------
def _demo_copy(tmp_path: Path) -> Path:
    """A working copy of the bundled demo-repo (with the seeded `> 10` bug)."""
    import shutil

    from loopkit.scenarios import demo_src
    repo = tmp_path / "demo"
    shutil.copytree(demo_src(), repo, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__"))
    return repo


def test_real_oracle_fails_first_and_flips_on_the_real_fix(tmp_path: Path):
    # The real seam the CLI uses: pytest as the held-out oracle, a python one-liner as the reference
    # fix (`> 10` → `>= 10`). Fail-first: the holdout boundary test fails on the buggy tree. Pass-on-
    # fix: after the fix it passes. Blessed — proven with the actual gate machinery, no fake runner.
    repo = _demo_copy(tmp_path)
    py = sys.executable
    oracle = f"{py} -m pytest tests/holdout -q"
    fix = (f"{py} -c \"import pathlib; p = pathlib.Path('pricing.py'); "
           f"p.write_text(p.read_text().replace('> 10', '>= 10'))\"")
    verdict = verify_oracle(oracle, repo, timestamp=TS, fix=fix)
    assert verdict.blessed is True
    assert [(c.name, c.ok) for c in verdict.checks] == [(FAIL_FIRST, True), (PASS_ON_FIX, True)]
    assert isinstance(verdict, OracleVerdict)


def test_real_seen_suite_is_refused_as_an_oracle(tmp_path: Path):
    # The Chapter-9 trap: the *seen* suite passes even on the buggy tree (it misses the boundary), so
    # it certifies nothing held-out — synth-gate must refuse it.
    repo = _demo_copy(tmp_path)
    verdict = verify_oracle(f"{sys.executable} -m pytest tests/seen -q", repo, timestamp=TS)
    assert verdict.blessed is False and verdict.checks[0].name == FAIL_FIRST


# --- the CLI exit-code contract -------------------------------------------------------------
def _run_cli(args: list[str], cwd: Path, monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from loopkit.cli import app
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir(exist_ok=True)
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))            # don't scrub the dev env
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(app, ["synth-gate", *args])


def test_cli_blesses_a_real_oracle_exit_0(tmp_path: Path, monkeypatch):
    repo = _demo_copy(tmp_path)
    out = tmp_path / "verdict.json"
    result = _run_cli([f"{sys.executable} -m pytest tests/holdout -q", "--out", str(out)],
                      repo, monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["blessed"] is True


def test_cli_refuses_an_already_green_oracle_exit_3(tmp_path: Path, monkeypatch):
    repo = _demo_copy(tmp_path)
    result = _run_cli(["true"], repo, monkeypatch, tmp_path)
    assert result.exit_code == 3, result.output
    assert "not blessed" in result.output


def test_cli_probe_pass_blesses_with_env_live(tmp_path: Path, monkeypatch):
    repo = _demo_copy(tmp_path)
    out = tmp_path / "verdict.json"
    result = _run_cli([f"{sys.executable} -m pytest tests/holdout -q",
                       "--probe", "true", "--out", str(out)], repo, monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["blessed"] is True and data["env_live"] is True


def test_cli_probe_fail_refuses_as_env_broken(tmp_path: Path, monkeypatch):
    repo = _demo_copy(tmp_path)
    out = tmp_path / "verdict.json"
    result = _run_cli([f"{sys.executable} -m pytest tests/holdout -q",
                       "--probe", "false", "--out", str(out)], repo, monkeypatch, tmp_path)
    assert result.exit_code == 3, result.output
    data = json.loads(out.read_text())
    assert data["blessed"] is False and data["env_live"] is False
    assert [c["name"] for c in data["checks"]] == ["env-live"]        # fail-first never ran


def test_cli_errors_when_no_oracle_and_no_config(tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run_cli([], empty, monkeypatch, tmp_path)                # no arg, no loopkit.toml
    assert result.exit_code == 1 and "no oracle" in result.output


# --- broken-oracle detection: a non-zero exit for the WRONG reason is not a genuine fail-first ------
def test_broken_oracle_command_not_found_is_not_blessed(tmp_path: Path):
    # A bare `python` where only python3/uv exist → 127. fail-first must NOT bless this.
    runner = lambda c, w: GateResult(False, "bash: line 1: python: command not found")
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, run_gate=runner)
    assert verdict.blessed is False
    assert verdict.checks[0].broken is True and verdict.checks[0].ok is False


def test_broken_oracle_shell_parse_error_output_is_not_blessed(tmp_path: Path):
    runner = lambda c, w: GateResult(False, "run.sh: line 16: unexpected EOF while looking for `''")
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, run_gate=runner)
    assert verdict.blessed is False and verdict.checks[0].broken is True


def test_genuine_assertion_failure_is_blessed_not_flagged_broken(tmp_path: Path):
    # A REAL failing test (the fail-first ideal) must not be mistaken for a broken oracle.
    runner = lambda c, w: GateResult(False, "E   assert 20.0 == 18.0  (boundary not discounted)")
    verdict = verify_oracle("run oracle", tmp_path, timestamp=TS, run_gate=runner)
    assert verdict.blessed is True and verdict.checks[0].broken is False


def test_parse_check_rejects_shell_syntax_error_before_any_gate_run(tmp_path: Path):
    # An apostrophe in ${VAR:?word} opens an unbalanced quote → `bash -n` catches it up front, so the
    # gate runner is never even consulted (short-circuit) and the oracle is flagged broken, not blessed.
    oracle_sh = tmp_path / "run.sh"
    oracle_sh.write_text('#!/usr/bin/env bash\n: "${ACCEPTANCE_DIR:?point at the oracle\'s dir}"\n'
                         'echo ok\n')
    consulted: list[int] = []
    def runner(c, w):
        consulted.append(1)
        return GateResult(False, "should not be reached")
    verdict = verify_oracle(f"bash {oracle_sh}", tmp_path, timestamp=TS, run_gate=runner)
    assert verdict.blessed is False and verdict.checks[0].broken is True
    assert not consulted            # short-circuited before running the gate
