"""Deterministic repo introspection — `loopkit detect` proposes the mechanical, safety-critical config.

Two layers: the **heuristics** over synthetic trees (each marker → the right test command / protected
path / branch / adapter, with `which` injected so adapter detection is pinned, no real PATH), and the
**CLI contract** through `CliRunner` (print-by-default, `--write` refuses an existing config without
`--force`, `--out` writes the JSON audit record). The load-bearing property — the proposed TOML parses
and validates back into a `Config` — is asserted directly. No tokens, no network.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from loopkit.config import Config
from loopkit.extensions.detect import (HIGH, LOW, MEDIUM, NONE, Detection, RepoProfile, detect_repo)


def _fake_which(present: dict[str, str]):
    """A `shutil.which` stand-in: returns the mapped path for a binary, else None (nothing on PATH)."""
    return lambda binary: present.get(binary)


_NO_AGENT = _fake_which({})
_CLAUDE = _fake_which({"claude": "/usr/bin/claude"})


# --- test-runner detection ------------------------------------------------------------------
def test_pytest_ini_detects_pytest(tmp_path: Path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    profile = detect_repo(tmp_path, which=_NO_AGENT)
    assert profile.test_command == "python -m pytest -q"
    runner = next(d for d in profile.detections if d.key == "test-runner")
    assert runner.confidence == HIGH and "pytest" in runner.evidence


def test_pyproject_with_tests_dir_detects_pytest(tmp_path: Path):
    # a bare pyproject alone isn't proof of a suite; alongside a tests/ dir it is.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "tests").mkdir()
    assert detect_repo(tmp_path, which=_NO_AGENT).test_command == "python -m pytest -q"


def test_bare_pyproject_without_pytest_config_or_tests_is_not_a_runner(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")   # metadata only, no tests/
    profile = detect_repo(tmp_path, which=_NO_AGENT)
    assert profile.test_command is None
    assert next(d for d in profile.detections if d.key == "test-runner").confidence == NONE


def test_node_declared_test_script_uses_the_right_package_manager(tmp_path: Path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest --ci"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")            # pnpm lockfile → pnpm test
    assert detect_repo(tmp_path, which=_NO_AGENT).test_command == "pnpm test"


def test_npm_placeholder_script_is_not_a_test_command(tmp_path: Path):
    placeholder = 'echo "Error: no test specified" && exit 1'
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": placeholder}}))
    assert detect_repo(tmp_path, which=_NO_AGENT).test_command is None


def test_go_module_detects_go_test(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_repo(tmp_path, which=_NO_AGENT).test_command == "go test ./..."


def test_makefile_test_target_is_a_fallback_runner(tmp_path: Path):
    (tmp_path / "Makefile").write_text("build:\n\tgo build\ntest:\n\tgo test ./...\n")
    assert detect_repo(tmp_path, which=_NO_AGENT).test_command == "make test"


def test_makefile_without_a_test_target_is_not_a_runner(tmp_path: Path):
    (tmp_path / "Makefile").write_text("build:\n\tgo build\n")   # no `test:` target
    assert detect_repo(tmp_path, which=_NO_AGENT).test_command is None


def test_primary_runner_wins_and_others_become_alternatives(tmp_path: Path):
    # python markers take priority; a Makefile test target is recorded as an alternative, not dropped.
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    profile = detect_repo(tmp_path, which=_NO_AGENT)
    assert profile.test_command == "python -m pytest -q"
    alts = [d.value for d in profile.detections if d.key == "test-runner-alt"]
    assert "make test" in alts


# --- protected-path detection ---------------------------------------------------------------
def test_only_existing_candidates_are_proposed(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "poetry.lock").write_text("")
    profile = detect_repo(tmp_path, which=_NO_AGENT)
    assert profile.protected_paths == ["tests/", ".github/workflows/", "migrations/", "poetry.lock"]


def test_no_protected_candidates_yields_empty_list(tmp_path: Path):
    (tmp_path / "src").mkdir()                              # nothing in the curated set
    assert detect_repo(tmp_path, which=_NO_AGENT).protected_paths == []


def test_only_the_first_test_directory_is_protected(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "test").mkdir()                             # a repo usually has one — don't propose both
    protected = detect_repo(tmp_path, which=_NO_AGENT).protected_paths
    assert protected == ["tests/"] and "test/" not in protected


def test_a_spec_directory_is_recognised_as_the_test_dir(tmp_path: Path):
    (tmp_path / "spec").mkdir()
    assert detect_repo(tmp_path, which=_NO_AGENT).protected_paths == ["spec/"]


# --- adapter detection ----------------------------------------------------------------------
def test_adapter_is_claude_code_when_claude_on_path(tmp_path: Path):
    profile = detect_repo(tmp_path, which=_CLAUDE)
    assert profile.adapter == "claude-code"
    assert next(d for d in profile.detections if d.key == "adapter").confidence == HIGH


def test_adapter_is_codex_when_only_codex_on_path(tmp_path: Path):
    profile = detect_repo(tmp_path, which=_fake_which({"codex": "/usr/bin/codex"}))
    assert profile.adapter == "codex"


def test_adapter_defaults_to_claude_code_with_low_confidence_when_none_on_path(tmp_path: Path):
    profile = detect_repo(tmp_path, which=_NO_AGENT)
    adapter = next(d for d in profile.detections if d.key == "adapter")
    assert profile.adapter == "claude-code" and adapter.confidence == LOW


# --- default-branch detection ---------------------------------------------------------------
def test_default_branch_from_a_local_main(git_repo: Path):
    # git_repo is on `main` with no remote → the local-branch fallback (MEDIUM).
    profile = detect_repo(git_repo, which=_NO_AGENT)
    branch = next(d for d in profile.detections if d.key == "default-branch")
    assert profile.default_branch == "main" and branch.confidence == MEDIUM


def test_default_branch_none_outside_a_git_repo(tmp_path: Path):
    profile = detect_repo(tmp_path, which=_NO_AGENT)
    assert profile.default_branch is None
    assert next(d for d in profile.detections if d.key == "default-branch").confidence == NONE


# --- the proposed TOML: parses, validates, and is safe --------------------------------------
def test_proposed_toml_parses_and_validates_into_a_config(tmp_path: Path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "tests").mkdir()
    toml = detect_repo(tmp_path, which=_CLAUDE).to_toml()
    cfg = Config.model_validate(tomllib.loads(toml))       # round-trips through the real validator
    assert cfg.gate.iteration == "python -m pytest -q"
    assert cfg.safety.protected_paths == ["tests/"]
    assert cfg.branch == "loopkit/run"                     # never the default branch (Ch 16)


def test_toml_augments_forbid_branches_with_the_detected_default(git_repo: Path):
    import subprocess
    subprocess.run(["git", "-C", str(git_repo), "branch", "-m", "trunk"], check=True)
    cfg = Config.model_validate(tomllib.loads(detect_repo(git_repo, which=_NO_AGENT).to_toml()))
    assert "trunk" in cfg.safety.forbid_branches            # the loop must never push to the real default
    assert {"main", "master"} <= set(cfg.safety.forbid_branches)


def test_toml_leaves_acceptance_oracle_unset_for_the_molder(tmp_path: Path):
    # detect must NOT fake the held-out oracle — it's the copilot's + synth-gate's job (the Part IV line).
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    cfg = Config.model_validate(tomllib.loads(detect_repo(tmp_path, which=_CLAUDE).to_toml()))
    assert cfg.gate.acceptance is None
    assert "synth-gate" in detect_repo(tmp_path, which=_CLAUDE).to_toml()   # but it points the way


def test_toml_without_a_runner_stays_valid_with_a_placeholder(tmp_path: Path):
    cfg = Config.model_validate(tomllib.loads(detect_repo(tmp_path, which=_NO_AGENT).to_toml()))
    assert cfg.gate.iteration and cfg.safety.protected_paths == ["tests/"]  # a safe fallback, still valid


# --- provenance: JSON is self-describing ----------------------------------------------------
def test_profile_json_roundtrip_carries_the_audit_trail(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module x\n")
    profile = detect_repo(tmp_path, which=_CLAUDE)
    data = json.loads(profile.to_json())
    assert data["test_command"] == "go test ./..." and data["adapter"] == "claude-code"
    keys = {d["key"] for d in data["detections"]}
    assert {"test-runner", "adapter", "default-branch"} <= keys


def test_dataclasses_are_plain_and_stable():
    # a defensive shape check so a refactor that renames a field is caught here.
    d = Detection("k", "v", "why", HIGH)
    assert (d.key, d.value, d.evidence, d.confidence) == ("k", "v", "why", "high")
    p = RepoProfile(root="/x", test_command=None, protected_paths=[], default_branch=None,
                    adapter="claude-code")
    assert p.to_dict()["adapter"] == "claude-code"


# --- the CLI contract -----------------------------------------------------------------------
def _run_cli(args: list[str], monkeypatch, tmp_path: Path):
    from typer.testing import CliRunner

    from loopkit.cli import app
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir(exist_ok=True)
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))
    return CliRunner().invoke(app, ["detect", *args])


def _pytest_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n")
    (repo / "tests").mkdir()
    return repo


def test_cli_prints_the_proposal_without_writing(tmp_path: Path, monkeypatch):
    repo = _pytest_repo(tmp_path)
    result = _run_cli([str(repo)], monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "proposed loopkit.toml" in result.output
    assert not (repo / "loopkit.toml").exists()            # default is print-only, decide nothing


def test_cli_write_creates_the_config(tmp_path: Path, monkeypatch):
    repo = _pytest_repo(tmp_path)
    result = _run_cli([str(repo), "--write"], monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    written = (repo / "loopkit.toml").read_text()
    assert Config.model_validate(tomllib.loads(written)).gate.iteration == "python -m pytest -q"


def test_cli_write_refuses_an_existing_config_without_force(tmp_path: Path, monkeypatch):
    repo = _pytest_repo(tmp_path)
    (repo / "loopkit.toml").write_text("# hand-written, do not clobber\n")
    result = _run_cli([str(repo), "--write"], monkeypatch, tmp_path)
    assert result.exit_code == 1 and "already exists" in result.output
    assert "hand-written" in (repo / "loopkit.toml").read_text()   # left untouched


def test_cli_write_force_overwrites(tmp_path: Path, monkeypatch):
    repo = _pytest_repo(tmp_path)
    (repo / "loopkit.toml").write_text("# stale\n")
    result = _run_cli([str(repo), "--write", "--force"], monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert "stale" not in (repo / "loopkit.toml").read_text()


def test_cli_out_writes_the_json_profile(tmp_path: Path, monkeypatch):
    repo = _pytest_repo(tmp_path)
    out = tmp_path / "profile.json"
    result = _run_cli([str(repo), "--out", str(out)], monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["test_command"] == "python -m pytest -q"


def test_cli_missing_path_errors(tmp_path: Path, monkeypatch):
    result = _run_cli([str(tmp_path / "does-not-exist")], monkeypatch, tmp_path)
    assert result.exit_code == 1 and "no such path" in result.output
