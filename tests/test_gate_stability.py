"""Gate-determinism preflight (Ch 9): prove the gate is stable before trusting it as a stop oracle.

A gate that returns a different pass/fail on an *identical* tree corrupts every stop decision the loop
makes — it will "fix" code that is already correct, or halt on code that is broken. A flaky gate is
worse than no gate. `safety.gate_stability` runs the gate N times on the unchanged tree and reports
whether the verdict is unanimous; `loopkit run --check-gate N` (or `safety.gate_stability_runs`)
refuses to start otherwise. Default off ⇒ exact prior behavior. All token-free.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.cli import app
from loopkit.gate import CallableGate, ShellGate
from loopkit.safety import gate_stability

runner = CliRunner()


def _norm(output: str) -> str:
    """Collapse rich's line-wrapping so a phrase assertion isn't broken by a wrap-inserted newline."""
    return " ".join(output.split())


# --- the function: stable pass, stable fail, flaky (callable + real shell) -------------------
def test_stable_pass_gate_is_deterministic(tmp_path: Path):
    stab = gate_stability(CallableGate(lambda ws: True), tmp_path, runs=5)
    assert stab.deterministic and stab.passes == 5 and stab.runs == 5


def test_stable_fail_gate_is_deterministic(tmp_path: Path):
    # A loop legitimately starts red — a stable *fail* is fine; only instability is the problem.
    stab = gate_stability(CallableGate(lambda ws: False), tmp_path, runs=5)
    assert stab.deterministic and stab.passes == 0


def test_flaky_callable_gate_is_caught(tmp_path: Path):
    seq = iter([True, False, True, False])
    stab = gate_stability(CallableGate(lambda ws: next(seq)), tmp_path, runs=4)
    assert not stab.deterministic and stab.passes == 2


def test_flaky_shell_gate_is_caught(tmp_path: Path):
    # The real seam: a ShellGate whose exit code flips on a counter file → mixed verdict over 4 runs.
    flaky = (f"{sys.executable} -c \"import pathlib,sys; p=pathlib.Path('.c'); "
             f"n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); sys.exit(n%2)\"")
    stab = gate_stability(ShellGate(flaky), tmp_path, runs=4)
    assert not stab.deterministic


# --- the CLI: run --check-gate refuses a flaky gate, proceeds past a stable one --------------
_FLAKY_GATE = '''\
import pathlib, sys
p = pathlib.Path(".lk_flaky")
n = int(p.read_text()) if p.exists() else 0
p.write_text(str(n + 1))
sys.exit(n % 2)
'''


@pytest.fixture
def clean_creds(monkeypatch, tmp_path):
    """An empty creds dir so `loopkit run`'s first-thing secrets.install is a clean no-op (it never
    scrubs the dev's real os.environ), plus silenced tracing so no stray key reaches the network."""
    d = tmp_path / "creds"
    d.mkdir()
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(d))
    for var in ("LANGSMITH_API_KEY", "LANGSMITH_TRACING", "LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(var, raising=False)
    return d


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body)
    subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", f"add {name}"], cwd=repo, check=True, capture_output=True)


def _write_config(repo: Path, iteration: str) -> Path:
    # Commit everything so the tree is clean (passes require_clean_tree). The iteration command embeds
    # the interpreter path so the test doesn't depend on `python` being on PATH.
    toml = (f'goal = "fix it"\nrepo = "."\nbranch = "loopkit/run"\n\n'
            f'[agent]\nadapter = "mock"\n\n[gate]\niteration = "{iteration}"\n\n'
            f'[safety]\nprotected_paths = []\n')
    _commit(repo, "loopkit.toml", toml)
    return repo / "loopkit.toml"


def test_run_check_gate_refuses_a_flaky_gate(git_repo: Path, clean_creds):
    _commit(git_repo, "flaky_gate.py", _FLAKY_GATE)
    toml = _write_config(git_repo, f"{sys.executable} flaky_gate.py")
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--check-gate", "4", "--dry-run"])
    assert result.exit_code == 1
    assert "non-deterministic" in _norm(result.output)


def test_run_check_gate_passes_a_stable_gate(git_repo: Path, clean_creds):
    toml = _write_config(git_repo, "true")                 # always exits 0 → unanimous pass
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--check-gate", "3", "--dry-run"])
    assert result.exit_code != 1                           # not refused
    assert "deterministic over 3 runs" in _norm(result.output)
