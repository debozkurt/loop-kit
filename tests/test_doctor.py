"""Gate-aware `loopkit doctor` — beyond the static checks, it runs the iteration gate once on the
current tree and translates the verdict into a readiness signal a beginner can act on:

  - a gate that already PASSES   → flagged (the loop may instantly, falsely declare DONE)
  - a gate that FAILS            → reported as the *healthy* start (the loop has work to do)
  - a gate command that's BROKEN → flagged as a misconfig, not mistaken for a test failure
  - acceptance == iteration      → flagged (a held-out gate the loop optimizes against can't catch overfit)

Token-free: the gates are `true`/`false`/a bogus command and the agent is `mock`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from loopkit.cli import app

runner = CliRunner()


def _norm(output: str) -> str:
    """Collapse rich's line-wrapping so a phrase assertion isn't broken by a wrap-inserted newline."""
    return " ".join(output.split())


def _doctor_config(repo: Path, iteration: str, acceptance: str | None = None) -> Path:
    """A committed loopkit.toml whose `repo` is the absolute test repo, so the gate runs *there*."""
    acc = f'acceptance = "{acceptance}"\n' if acceptance else ""
    toml = (f'goal = "fix it"\nrepo = "{repo}"\nbranch = "loopkit/run"\n\n'
            f'[agent]\nadapter = "mock"\n\n'
            f'[gate]\niteration = "{iteration}"\n{acc}\n'
            f'[safety]\nprotected_paths = ["tests/"]\n')
    p = repo / "loopkit.toml"
    p.write_text(toml)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "config"], cwd=repo, check=True, capture_output=True)
    return p


def test_doctor_flags_a_gate_that_already_passes(git_repo: Path):
    p = _doctor_config(git_repo, "true")                       # exits 0 on the unchanged tree
    out = _norm(runner.invoke(app, ["doctor", "-c", str(p)]).output)
    assert "already passes" in out and "false DONE" in out


def test_doctor_reports_a_failing_gate_as_the_healthy_start(git_repo: Path):
    p = _doctor_config(git_repo, "false")                      # exits 1 → the loop has work
    out = _norm(runner.invoke(app, ["doctor", "-c", str(p)]).output)
    assert "has work" in out and "looks broken" not in out


def test_doctor_flags_a_broken_gate_command(git_repo: Path):
    p = _doctor_config(git_repo, "loopkit-no-such-cmd-xyz")    # shell: command not found
    out = _norm(runner.invoke(app, ["doctor", "-c", str(p)]).output)
    assert "gate looks broken" in out


def test_doctor_warns_when_acceptance_equals_iteration(git_repo: Path):
    p = _doctor_config(git_repo, "true", acceptance="true")    # held-out gate the loop optimizes against
    out = _norm(runner.invoke(app, ["doctor", "-c", str(p)]).output)
    assert "not held-out" in out


def test_doctor_no_gate_skips_running_the_gate(git_repo: Path):
    # --no-gate must not run the gate: a broken command produces no gate-verdict row at all.
    p = _doctor_config(git_repo, "loopkit-no-such-cmd-xyz")
    out = _norm(runner.invoke(app, ["doctor", "-c", str(p), "--no-gate"]).output)
    assert "gate verdict" not in out and "looks broken" not in out
