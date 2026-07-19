"""Mold-batch tests: the level ladder, the proposer seam, mandatory verification, resume — no tokens.

Layer 5's load-bearing claims: mechanical code never fakes an oracle (a skeleton with FILL markers
stops at needs-oracle, unverified); nothing unblessed reaches the emitted batch manifest (synth-gate
fail-first is mandatory and isolated); the two knobs compose (`--level` stops the ladder early,
`--limit` + the state file resume — successes skip, failures retry); and the emitted `batch.toml`
is actually consumable by `loopkit batch` (round-tripped through its own validator), with
not-ready tasks demoted visibly rather than dropped or left as dangling `after` edges. The oracle
runs use real shell commands over throwaway git repos — cheap and tokenless.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.extensions.batch import load_manifest as load_batch_manifest
from loopkit.extensions.detect import RepoProfile
from loopkit.extensions.mold import (
    DETECTED,
    NEEDS_ORACLE,
    ORACLE_REJECTED,
    READY,
    VERIFIED,
    MoldDefaults,
    MoldSpec,
    ShellProposer,
    _has_fill_markers,
    _ORACLE_SKELETON,
    _render_task_config,
    load_mold_manifest,
    mold_batch,
    oracle_command,
)

TS = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def _seed_repo(path: Path) -> Path:
    """A git repo with a pytest marker so `detect` finds a test runner deterministically."""
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\nname = \"demo\"\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_seed.py").write_text("def test_seed():\n    assert True\n")
    for args in (("init", "-q"), ("branch", "-m", "main"),
                 ("config", "user.email", "t@loopkit"), ("config", "user.name", "loopkit-test")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True)
    return path


def _mold_manifest(tmp_path: Path, body: str, repo: Path) -> Path:
    path = tmp_path / "mold.toml"
    path.write_text(f'[defaults]\nrepo = "{repo}"\n\n{body}')
    return path


def _write_failing_oracle(oracle_dir: Path, *, probe_alive: bool = True) -> None:
    """A complete (FILL-free) oracle that fails on the current tree — the fail-first ideal —
    plus its env-liveness probe (required since Q3; `probe_alive=False` simulates a broken env)."""
    oracle_dir.mkdir(parents=True, exist_ok=True)
    (oracle_dir / "run.sh").write_text("#!/usr/bin/env bash\necho 'not fixed yet'\nexit 1\n")
    (oracle_dir / "probe.sh").write_text(
        "#!/usr/bin/env bash\n" + ("true\n" if probe_alive else "echo 'auth down'\nexit 1\n"))


def _proposer_script(tmp_path: Path, *, exit_code: int = 1) -> str:
    """A stand-in for the headless-agent proposer: writes a real failing oracle into
    $MOLD_ORACLE_DIR AND fills the env-liveness probe via $MOLD_PROBE_FILE (the Q3 contract — a
    proposer that skips the probe holds its task at needs-oracle). `exit_code` controls what the
    *oracle* exits with, not the proposer."""
    script = tmp_path / "proposer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '#!/usr/bin/env bash\\necho proposed for %s\\nexit "
        f"{exit_code}" "\\n' \"$MOLD_TASK_ID\" > \"$MOLD_ORACLE_DIR/run.sh\"\n"
        "printf '#!/usr/bin/env bash\\ntrue\\n' > \"$MOLD_PROBE_FILE\"\n"
        "echo \"proposed using tier: $MOLD_TIER_ASSERTION\"\n")
    script.chmod(0o755)
    return f"bash {script}"


# --------------------------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------------------------
def test_mold_manifest_parses(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    mf = _mold_manifest(tmp_path, """
[[task]]
id = "a"
goal = "fix a"
tier = "authz"
group = "handlers"

[[task]]
id = "b"
issue = 7
after = ["a"]
""", repo)
    m = load_mold_manifest(mf)
    assert m.task[0].tier == "authz" and m.task[1].after == ["a"]
    assert m.task[1].tier == "correctness"                # the default tier


@pytest.mark.parametrize("body, fragment", [
    ('[[task]]\nid = "a"\ngoal = "x"\ntier = "made-up"\n', "unknown tier"),
    ('[[task]]\nid = "a"\n', "goal or issue"),
    ('[[task]]\nid = "a"\ngoal = "x"\n\n[[task]]\nid = "a"\ngoal = "y"\n', "duplicate"),
    ('[[task]]\nid = "a"\ngoal = "x"\nafter = ["ghost"]\n', "unknown task"),
])
def test_mold_manifest_rejects(tmp_path, body, fragment):
    repo = _seed_repo(tmp_path / "repo")
    with pytest.raises(ValueError, match=fragment):
        load_mold_manifest(_mold_manifest(tmp_path, body, repo))


def test_mold_manifest_requires_a_repo(tmp_path):
    path = tmp_path / "mold.toml"
    path.write_text('[[task]]\nid = "a"\ngoal = "x"\n')   # no [defaults] repo, no per-task repo
    with pytest.raises(ValueError, match="no repo"):
        load_mold_manifest(path)


# --------------------------------------------------------------------------------------------
# The level ladder
# --------------------------------------------------------------------------------------------
def test_level_detect_records_profile_and_stops(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    result = mold_batch(m, out, level="detect", timestamp=TS)
    assert result.rows[0].status == DETECTED
    profile = json.loads((out / "a" / "detect.json").read_text())
    assert "pytest" in profile["test_command"]            # the pyproject marker decided it
    assert not (out / "a" / "acceptance").exists()        # the ladder stopped before oracle


def test_level_oracle_without_proposer_stops_at_skeleton(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(
        tmp_path, '[[task]]\nid = "a"\ngoal = "x"\ntier = "serializer"\n', repo))
    out = tmp_path / "molded"
    result = mold_batch(m, out, level="oracle", timestamp=TS)
    row = result.rows[0]
    assert row.status == NEEDS_ORACLE and row.verdict is None   # never verified, never blessed
    skeleton = (out / "a" / "acceptance" / "run.sh").read_text()
    assert "FILL" in skeleton
    assert "confidential fields ABSENT" in skeleton       # the tier's typed assertion, inlined
    assert result.attention and result.attention[0].spec.id == "a"


def test_level_oracle_with_proposer_verifies_and_blesses(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    result = mold_batch(m, out, level="oracle", timestamp=TS,
                        proposer=ShellProposer(_proposer_script(tmp_path, exit_code=1)))
    row = result.rows[0]
    assert row.status == VERIFIED
    assert row.verdict is not None and row.verdict.blessed and row.verdict.isolated
    verdict = json.loads((out / "a" / "verdict.json").read_text())
    assert verdict["blessed"] is True
    notes = (out / "a" / "proposer-notes.md").read_text()
    assert "tier:" in notes                               # the tier assertion reached the proposer


def test_oracle_that_passes_on_buggy_tree_is_rejected(tmp_path):
    # exit 0 on the current tree = certifies nothing; fail-first must refuse to bless it.
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    result = mold_batch(m, out, level="oracle", timestamp=TS,
                        proposer=ShellProposer(_proposer_script(tmp_path, exit_code=0)))
    row = result.rows[0]
    assert row.status == ORACLE_REJECTED and not row.verdict.blessed
    assert "fail-first" in row.note


# --------------------------------------------------------------------------------------------
# FILL-marker detection: real markers vs. prose that merely says the word. Regression — a proposer
# that fills every marker but keeps the skeleton's explanatory comment ships a COMPLETE oracle; a
# bare `"FILL" in text` substring test wrongly re-read it as unfilled and stalled it at
# needs-oracle (observed on a real molding run). Only the marker grammar counts.
# --------------------------------------------------------------------------------------------
def test_has_fill_markers_flags_the_unfilled_skeleton(tmp_path):
    run_sh = tmp_path / "run.sh"
    run_sh.write_text(_ORACLE_SKELETON.format(task_id="a", tier="authz", assertion="x"))
    assert _has_fill_markers(run_sh)          # FILL 1/2/3 + FILL_/FILL_ token + FILL/ path present


def test_has_fill_markers_ignores_prose_mentions_of_the_word(tmp_path):
    run_sh = tmp_path / "run.sh"
    run_sh.write_text(
        "#!/usr/bin/env bash\n"
        "# any unfilled placeholder below blocks verification — a FILL marker would.\n"
        "# FILL markers are the judgment-still-owed signal, but this oracle has none.\n"
        "pytest tests/zz_holdout_test.py\n")
    assert not _has_fill_markers(run_sh)      # a complete oracle; the word only appears in prose


def test_has_fill_markers_ignores_kept_step_labels(tmp_path):
    """A proposer may fill the code line yet KEEP the `# FILL N —` step label above it. The label is
    not a fill target, so a filled oracle that keeps the labels must read as complete (the
    `FILL \\d`-style over-match this guards against was the second false positive found in the wild)."""
    run_sh = tmp_path / "run.sh"
    run_sh.write_text(
        "#!/usr/bin/env bash\n"
        "# FILL 1 — where the hidden test lands:\n"
        "HOLDOUT=\"tests/regression/zz_holdout_test.py\"\n"
        "# FILL 2 — copy the hidden test into the tree:\n"
        "cp \"$ACCEPTANCE_DIR/holdout.py\" \"$HOLDOUT\"\n"
        "# FILL 3 — run it through the repo's real runner:\n"
        "pytest \"$HOLDOUT\"\n")
    assert not _has_fill_markers(run_sh)      # labels kept, code filled — nothing actually owed


def test_has_fill_markers_flags_unfilled_code_placeholders(tmp_path):
    run_sh = tmp_path / "run.sh"
    run_sh.write_text(                        # the skeleton's code tokens, still unfilled
        "#!/usr/bin/env bash\n"
        "HOLDOUT=\"FILL/path/in/repo/test_holdout\"\n"
        "cp \"$ACCEPTANCE_DIR/FILL_test_holdout\" \"$HOLDOUT\"\n"
        "FILL_test_command \"$HOLDOUT\"\n")
    assert _has_fill_markers(run_sh)          # FILL/ + FILL_ code placeholders → still owed


def test_filled_oracle_that_keeps_labels_and_prose_still_verifies(tmp_path):
    """End-to-end guard for the false positive: the proposer fills every code placeholder but
    preserves BOTH the skeleton's prose comment and its `# FILL N —` step labels. The oracle is
    complete and fails-first, so it must be VERIFIED — not demoted to needs-oracle."""
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    script = tmp_path / "proposer_keeps_leftovers.sh"
    script.write_text(                        # marker-free CODE, but keeps the label + prose leftovers
        "#!/usr/bin/env bash\n"
        "cat > \"$MOLD_ORACLE_DIR/run.sh\" <<'ORACLE'\n"
        "#!/usr/bin/env bash\n"
        "# any unfilled placeholder below blocks verification — a FILL marker would.\n"
        "# FILL 1 — the held-out test path (filled below):\n"
        "echo 'not fixed yet'\n"
        "exit 1\n"
        "ORACLE\n"
        "printf '#!/usr/bin/env bash\\ntrue\\n' > \"$MOLD_PROBE_FILE\"\n"
        "echo proposed\n")
    script.chmod(0o755)
    result = mold_batch(m, out, level="oracle", timestamp=TS,
                        proposer=ShellProposer(f"bash {script}"))
    row = result.rows[0]
    assert row.status == VERIFIED             # was NEEDS_ORACLE under the substring / FILL \d bug
    assert row.verdict is not None and row.verdict.blessed
    kept = (out / "a" / "acceptance" / "run.sh").read_text()
    assert "a FILL marker would" in kept and "# FILL 1 —" in kept   # both leftovers survived, harmless


def test_filled_oracle_with_unfilled_probe_stays_needs_oracle(tmp_path):
    """The Q3 requirement: a molded oracle is unverifiable until its env-liveness probe is filled —
    an unprobed oracle is exactly what got false-blessed 6/6 when the env (not the bug) failed."""
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    script = tmp_path / "proposer_no_probe.sh"
    script.write_text(                        # fills run.sh, ignores $MOLD_PROBE_FILE
        "#!/usr/bin/env bash\n"
        "printf '#!/usr/bin/env bash\\nexit 1\\n' > \"$MOLD_ORACLE_DIR/run.sh\"\n")
    script.chmod(0o755)
    result = mold_batch(m, out, level="oracle", timestamp=TS,
                        proposer=ShellProposer(f"bash {script}"))
    row = result.rows[0]
    assert row.status == NEEDS_ORACLE and "probe.sh" in row.note


def test_env_broken_probe_rejects_instead_of_blessing(tmp_path):
    """The Wave-A regression, end-to-end through mold: an oracle whose runner cannot even pass a
    trivial probe (auth down / broken venv) is ORACLE_REJECTED on env-live — never VERIFIED."""
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    _write_failing_oracle(out / "a" / "acceptance", probe_alive=False)
    result = mold_batch(m, out, level="oracle", timestamp=TS)
    row = result.rows[0]
    assert row.status == ORACLE_REJECTED
    assert row.verdict is not None and row.verdict.env_live is False
    assert "env-live" in row.note
    # fail-first never ran — the only recorded check is the probe's.
    assert [c.name for c in row.verdict.checks] == ["env-live"]


def test_live_probe_verifies_and_records_env_live(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    _write_failing_oracle(out / "a" / "acceptance", probe_alive=True)
    result = mold_batch(m, out, level="oracle", timestamp=TS)
    row = result.rows[0]
    assert row.status == VERIFIED
    assert row.verdict is not None and row.verdict.env_live is True


def test_level_full_emits_config_and_batch_manifest(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, """
[[task]]
id = "a"
goal = "fix the \\"quoted\\" thing\\nacross two lines"
group = "handlers"
""", repo))
    out = tmp_path / "molded"
    result = mold_batch(m, out, level="full", timestamp=TS,
                        proposer=ShellProposer(_proposer_script(tmp_path)))
    assert result.rows[0].status == READY
    config = (out / "a" / "loopkit.toml").read_text()
    assert "pytest" in config                             # iteration gate from detect
    assert oracle_command(out / "a" / "acceptance") in config
    # The emitted batch manifest must be consumable by `loopkit batch` itself — round-trip it,
    # goal escaping included.
    emitted = load_batch_manifest(out / "batch.toml")
    task = emitted.task[0]
    assert task.id == "a" and task.group == "handlers"
    assert 'the "quoted" thing' in task.goal and "\n" in task.goal
    assert task.config == "a/loopkit.toml"                # relative — the dir travels as one unit


def _touches_proposer(tmp_path: Path, lines: str) -> str:
    """A proposer that also emits the observed-touches byproduct ($MOLD_TOUCHES_FILE)."""
    script = tmp_path / "touches-proposer.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "printf '#!/usr/bin/env bash\\nexit 1\\n' > \"$MOLD_ORACLE_DIR/run.sh\"\n"
        "printf '#!/usr/bin/env bash\\ntrue\\n' > \"$MOLD_PROBE_FILE\"\n"
        f"printf '{lines}' > \"$MOLD_TOUCHES_FILE\"\n")
    script.chmod(0o755)
    return f"bash {script}"


def test_proposer_touches_byproduct_rides_to_emitted_manifest(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "fix it"\n', repo))
    out = tmp_path / "molded"
    proposer = ShellProposer(_touches_proposer(
        tmp_path, "src/handlers/search.go\\n# comment skipped\\nsrc/db/db.go\\nsrc/handlers/search.go\\n"))
    result = mold_batch(m, out, level="full", timestamp=TS, proposer=proposer)
    assert result.rows[0].status == READY
    emitted = load_batch_manifest(out / "batch.toml")
    # comments skipped, duplicates dropped, order preserved — observed, not guessed
    assert emitted.task[0].touches == ["src/handlers/search.go", "src/db/db.go"]


def test_author_declared_touches_beat_observed(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(
        tmp_path, '[[task]]\nid = "a"\ngoal = "fix it"\ntouches = ["declared.py"]\n', repo))
    out = tmp_path / "molded"
    proposer = ShellProposer(_touches_proposer(tmp_path, "observed.py\\n"))
    mold_batch(m, out, level="full", timestamp=TS, proposer=proposer)
    emitted = load_batch_manifest(out / "batch.toml")
    assert emitted.task[0].touches == ["declared.py"]     # a human declaration is never diluted


def test_emit_reads_touches_artifact_on_resumed_run(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    mf = _mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "fix it"\n', repo)
    out = tmp_path / "molded"
    proposer = ShellProposer(_touches_proposer(tmp_path, "src/db/db.go\\n"))
    mold_batch(load_mold_manifest(mf), out, level="full", timestamp=TS, proposer=proposer)
    # Resume with a FRESH manifest object: the task skips (state says ready), the proposer never
    # re-runs, and the emitted manifest must still carry the observed touches — from the artifact.
    result = mold_batch(load_mold_manifest(mf), out, level="full", timestamp=TS, proposer=proposer)
    assert result.skipped == ["a"]
    emitted = load_batch_manifest(out / "batch.toml")
    assert emitted.task[0].touches == ["src/db/db.go"]


def test_emit_fills_review_placeholders_and_auto_wires_validate(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(
        tmp_path,
        'review = "bash judge.sh --goal {goal_file} --task {task_id}"\n\n'
        '[[task]]\nid = "a"\ngoal = "fix it"\n', repo))
    out = tmp_path / "molded"
    mold_batch(m, out, level="full", timestamp=TS,
               proposer=ShellProposer(_proposer_script(tmp_path)))
    emitted = load_batch_manifest(out / "batch.toml")
    task = emitted.task[0]
    # placeholders resolved to the molded artifacts (absolute — commands run in the scratch clone)
    assert task.review == f'bash judge.sh --goal {(out / "a" / "GOAL.md").resolve()} --task a'
    # no validate declared → auto-wired reproduce check from the blessed oracle
    assert task.validate_cmd.startswith("! ( ")
    assert str((out / "a" / "acceptance").resolve() / "run.sh") in task.validate_cmd


def test_explicit_validate_wins_and_empty_string_disables(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(
        tmp_path,
        '[[task]]\nid = "a"\ngoal = "fix a"\nvalidate = "bash repro.sh {task_id}"\n\n'
        '[[task]]\nid = "b"\ngoal = "fix b"\nvalidate = ""\n', repo))
    out = tmp_path / "molded"
    mold_batch(m, out, level="full", timestamp=TS,
               proposer=ShellProposer(_proposer_script(tmp_path)))
    emitted = load_batch_manifest(out / "batch.toml")
    by_id = {t.id: t for t in emitted.task}
    assert by_id["a"].validate_cmd == "bash repro.sh a"   # explicit command, placeholder filled
    assert by_id["b"].validate_cmd is None                # "" = opt out, auto-wire suppressed


def test_route_stage_uses_report_or_says_uncalibrated(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"trials": 5, "successes": 5, "goal": "g"}))
    m = load_mold_manifest(_mold_manifest(tmp_path, f"""
[[task]]
id = "calibrated"
goal = "x"
report = "{report}"

[[task]]
id = "uncalibrated"
goal = "y"
""", repo))
    out = tmp_path / "molded"
    result = mold_batch(m, out, level="route", timestamp=TS,
                        proposer=ShellProposer(_proposer_script(tmp_path)))
    by_id = {r.spec.id: r for r in result.rows}
    assert by_id["calibrated"].route is not None
    assert by_id["calibrated"].route.strategy == "single"     # 5/5 clears the threshold
    assert by_id["uncalibrated"].route is None
    route_note = json.loads((out / "uncalibrated" / "route.json").read_text())
    assert "uncalibrated" in route_note["reason"]             # said honestly, never guessed


# --------------------------------------------------------------------------------------------
# State: --limit chunks, successes skip, failures retry
# --------------------------------------------------------------------------------------------
def test_limit_and_state_resume(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    body = '[[task]]\nid = "a"\ngoal = "x"\n\n[[task]]\nid = "b"\ngoal = "y"\n'
    m = load_mold_manifest(_mold_manifest(tmp_path, body, repo))
    out = tmp_path / "molded"
    proposer = ShellProposer(_proposer_script(tmp_path))
    first = mold_batch(m, out, level="oracle", timestamp=TS, limit=1, proposer=proposer)
    assert [r.spec.id for r in first.rows] == ["a"] and not first.skipped
    second = mold_batch(m, out, level="oracle", timestamp=TS, limit=1, proposer=proposer)
    assert [r.spec.id for r in second.rows] == ["b"]      # resumed past the molded task
    assert second.skipped == ["a"]
    third = mold_batch(m, out, level="oracle", timestamp=TS, proposer=proposer)
    assert third.rows == [] and set(third.skipped) == {"a", "b"}


def test_failures_retry_and_human_edits_get_verified(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    out = tmp_path / "molded"
    first = mold_batch(m, out, level="oracle", timestamp=TS)          # no proposer → skeleton
    assert first.rows[0].status == NEEDS_ORACLE
    # The human loop: fill the skeleton by hand, re-run — the failure retries and now verifies.
    _write_failing_oracle(out / "a" / "acceptance")
    second = mold_batch(m, out, level="oracle", timestamp=TS)
    assert second.rows[0].status == VERIFIED and not second.skipped


def test_emitted_manifest_demotes_ready_task_with_unready_dependency(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    # 'dep' gets no proposer help (stays needs-oracle); 'top' is ready but depends on it.
    m = load_mold_manifest(_mold_manifest(tmp_path, """
[[task]]
id = "dep"
goal = "x"

[[task]]
id = "top"
goal = "y"
after = ["dep"]
""", repo))
    out = tmp_path / "molded"
    mold_batch(m, out, level="full", timestamp=TS)                    # both stop at needs-oracle
    _write_failing_oracle(out / "top" / "acceptance")                 # only 'top' gets a real oracle
    mold_batch(m, out, level="full", timestamp=TS)
    text = (out / "batch.toml").read_text()
    assert "[[task]]" not in text                         # 'top' demoted — no dangling after edge
    assert "waiting on 'dep'" in text                     # ...and demoted VISIBLY
    # The emitted file must still be valid enough to inspect; once 'dep' is fixed, both emit.
    _write_failing_oracle(out / "dep" / "acceptance")
    mold_batch(m, out, level="full", timestamp=TS)
    emitted = load_batch_manifest(out / "batch.toml")
    assert {t.id for t in emitted.task} == {"dep", "top"}


# --------------------------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------------------------
def _invoke(monkeypatch, tmp_path: Path, *args: str):
    from loopkit.cli import app
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir(exist_ok=True)
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))
    return CliRunner().invoke(app, ["mold-batch", *args])


def test_cli_dry_run_prints_plan(monkeypatch, tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    mf = _mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\ntier = "authz"\n', repo)
    result = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "molding plan" in result.output and "authz" in result.output
    assert not (tmp_path / "molded").exists()             # dry run writes nothing


def test_cli_full_run_exit_codes(monkeypatch, tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    mf = _mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo)
    out = tmp_path / "molded"
    # Without a proposer the task needs attention → exit 2, and the skeleton exists for the human.
    needs = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--out", str(out))
    assert needs.exit_code == 2, needs.output
    assert (out / "a" / "acceptance" / "run.sh").exists()
    # With a proposer everything molds → exit 0 and the next-step hint names the batch manifest.
    done = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--out", str(out),
                   "--proposer", _proposer_script(tmp_path))
    assert done.exit_code == 0, done.output
    assert "batch.toml" in done.output


def test_cli_rejects_bad_level_and_missing_repo(monkeypatch, tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    mf = _mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo)
    assert _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--level", "warp").exit_code == 1
    ghost = tmp_path / "ghost.toml"
    ghost.write_text('[defaults]\nrepo = "does/not/exist"\n\n[[task]]\nid = "a"\ngoal = "x"\n')
    assert _invoke(monkeypatch, tmp_path, "--tasks", str(ghost), "--dry-run").exit_code == 1


# --------------------------------------------------------------------------------------------
# Emitted-config parity: the knobs a hand-tuned config carries, that mold-batch must not drop.
# (Regression coverage for the six divergences the Batch-3 pilot surfaced.)
# --------------------------------------------------------------------------------------------
def _profile(**over) -> RepoProfile:
    base = dict(root="/x", test_command="uv run pytest -q", protected_paths=["tests/"],
                default_branch="develop", adapter="claude-code", detections=[])
    base.update(over)
    return RepoProfile(**base)


def _render(spec: MoldSpec, defaults: MoldDefaults | None = None, **profile_over) -> str:
    return _render_task_config(spec, defaults or MoldDefaults(), _profile(**profile_over),
                               repo=Path("/x"), iteration="uv run pytest -q", acceptance="bash o.sh")


def test_emit_defaults_headless_perms_flag_for_claude_code():
    # A headless `claude -p` denies every edit without the bypass → the agent lands zero changes.
    cfg = _render(MoldSpec(id="F-1", goal="g"))
    assert 'args         = ["--dangerously-skip-permissions"]' in cfg
    # Non-claude adapters have their own permission model → don't inject it.
    assert "dangerously-skip-permissions" not in _render(MoldSpec(id="F-1", goal="g"), adapter="codex")
    # Author override (incl. clearing it) wins.
    assert 'args         = ["--foo"]' in _render(MoldSpec(id="F-1", goal="g", agent_args=["--foo"]))
    assert "args         =" not in _render(MoldSpec(id="F-1", goal="g", agent_args=[]))


def test_emit_stops_default_unchanged_and_overridable():
    # Defaults keep the prior hardcoded pace (no behaviour change), but a heavyweight-gate repo can
    # slow it via [defaults] — stops are a knob now, not a constant.
    assert "max_iter          = 12\nno_progress_after = 4" in _render(MoldSpec(id="F-1", goal="g"))
    slow = _render(MoldSpec(id="F-1", goal="g"),
                   defaults=MoldDefaults(max_iter=30, no_progress_after=8))
    assert "max_iter          = 30\nno_progress_after = 8" in slow


def test_emit_pr_base_is_default_branch_never_silently_main():
    cfg = _render(MoldSpec(id="F-1", goal="g"))
    assert '[remote]\npr_base = "develop"' in cfg          # detect's default branch, not "main"
    assert 'pr_base = "release/x"' in _render(MoldSpec(id="F-1", goal="g", pr_base="release/x"))
    # No detectable default branch and no override → no [remote] section (batch falls back to its default).
    assert "[remote]" not in _render(MoldSpec(id="F-1", goal="g"), default_branch=None)


def test_emit_drops_bare_test_root_from_protected_paths():
    # The whole test tree must NOT be protected — the agent has to write tests, and the held-out oracle
    # is protected by invisibility, not by path.
    assert "protected_paths    = []" in _render(MoldSpec(id="F-1", goal="g"),
                                                protected_paths=["tests/"])
    # A specific subdir is kept (a real held-out location); infra is kept.
    kept = _render(MoldSpec(id="F-1", goal="g"),
                   protected_paths=["tests/regression", ".gitlab-ci.yml", "migrations"])
    assert '"tests/regression"' in kept and '".gitlab-ci.yml"' in kept and '"migrations"' in kept
    # Author override wins verbatim.
    assert 'protected_paths    = ["only/this"]' in _render(
        MoldSpec(id="F-1", goal="g", protected_paths=["only/this"]))


# --------------------------------------------------------------------------------------------
# Parallel molding (--jobs): proposers fan out, verify serialises per group, state is durable,
# and jobs=1 stays byte-identical to the serial path. See docs/mold-parallel-design.md.
# --------------------------------------------------------------------------------------------
import threading                                          # noqa: E402 — grouped with its users
import time                                               # noqa: E402

from loopkit.gate import GateResult                       # noqa: E402


def _many_task_body(ids, *, group=None):
    """A manifest body of independent single-goal tasks, optionally all sharing one group."""
    blocks = []
    for i in ids:
        g = f'\ngroup = "{group}"' if group else ""
        blocks.append(f'[[task]]\nid = "{i}"\ngoal = "fix {i}"{g}\n')
    return "\n".join(blocks)


class _ConcurrencyGate:
    """A fake gate runner that records peak in-flight concurrency. Probe passes, fail-first fails,
    so every task blesses; the sleep widens the window an overlapping call would occupy."""

    def __init__(self, *, delay=0.1):
        self._delay, self._lock = delay, threading.Lock()
        self.inflight = self.peak = 0

    def __call__(self, command, workspace):
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(self._delay)
        with self._lock:
            self.inflight -= 1
        return GateResult(passed="probe.sh" in command, feedback=None)


def test_jobs_1_matches_serial_and_parallel_agrees(tmp_path):
    # jobs=1 and jobs=4 must reach the same per-task verdicts (parity), differing only in wall-clock.
    def run(jobs, out):
        repo = _seed_repo(tmp_path / f"repo{jobs}")
        m = load_mold_manifest(_mold_manifest(tmp_path, _many_task_body(["a", "b", "c", "d"]), repo))
        return mold_batch(m, out, level="oracle", timestamp=TS, jobs=jobs,
                          proposer=ShellProposer(_proposer_script(tmp_path)))
    serial = run(1, tmp_path / "s")
    parallel = run(4, tmp_path / "p")
    assert [r.spec.id for r in serial.rows] == ["a", "b", "c", "d"]        # manifest order, not race order
    assert [r.spec.id for r in parallel.rows] == ["a", "b", "c", "d"]
    assert ([r.status for r in serial.rows] == [r.status for r in parallel.rows]
            == [VERIFIED] * 4)


def test_grouped_verify_never_overlaps(tmp_path):
    # Same-group tasks name one contended gate resource — their VERIFY steps must be mutually exclusive.
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path,
                                          _many_task_body(["a", "b", "c"], group="db"), repo))
    gate = _ConcurrencyGate()
    result = mold_batch(m, tmp_path / "out", level="oracle", timestamp=TS, jobs=3,
                        proposer=ShellProposer(_proposer_script(tmp_path)), run_gate=gate)
    assert [r.status for r in result.rows] == [VERIFIED] * 3
    assert gate.peak == 1                                 # the group lock held: never two verifies at once


def test_ungrouped_verify_runs_concurrently(tmp_path):
    # No group ⇒ no verify lock ⇒ verifies overlap (the whole point of --jobs for hermetic gates).
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, _many_task_body(["a", "b", "c"]), repo))
    gate = _ConcurrencyGate()
    result = mold_batch(m, tmp_path / "out", level="oracle", timestamp=TS, jobs=3,
                        proposer=ShellProposer(_proposer_script(tmp_path)), run_gate=gate)
    assert [r.status for r in result.rows] == [VERIFIED] * 3
    assert gate.peak >= 2                                 # at least two verifies were in flight together


def test_limit_selects_first_n_in_manifest_order_under_jobs(tmp_path):
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path,
                                          _many_task_body(["a", "b", "c", "d"]), repo))
    result = mold_batch(m, tmp_path / "out", level="oracle", timestamp=TS, jobs=4, limit=2,
                        proposer=ShellProposer(_proposer_script(tmp_path)))
    assert [r.spec.id for r in result.rows] == ["a", "b"]  # first two by manifest order, deterministic
    state = json.loads((tmp_path / "out" / "state.json").read_text())
    assert set(state["tasks"]) == {"a", "b"}              # only the selected two persisted


def test_state_written_incrementally_per_task(tmp_path):
    # Each finished task is persisted AS IT LANDS (durable checklist), not only at the end: at jobs=1,
    # by the time task 'b' reaches the gate, 'a' is already recorded in state.json.
    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, _many_task_body(["a", "b"]), repo))
    out = tmp_path / "out"
    seen: list[int] = []
    gate = _ConcurrencyGate(delay=0.02)

    def watcher(command, workspace):
        state_path = out / "state.json"
        seen.append(len(json.loads(state_path.read_text())["tasks"]) if state_path.exists() else 0)
        return gate(command, workspace)

    mold_batch(m, out, level="oracle", timestamp=TS, jobs=1,
               proposer=ShellProposer(_proposer_script(tmp_path)), run_gate=watcher)
    assert 1 in seen                                      # 'a' was persisted before 'b' ran (incremental)
    assert set(json.loads((out / "state.json").read_text())["tasks"]) == {"a", "b"}


def test_reference_proposer_template_molds_a_task(tmp_path, monkeypatch):
    # The shipped examples/molding/templates/proposer.sh is what a new repo copies — prove it actually
    # drives a task to VERIFIED (fills run.sh + probe.sh, passes its own validation + the synth-gate),
    # not just that it parses. A fake `claude` on PATH stands in for the headless agent (no tokens).
    template = Path(__file__).resolve().parents[1] / "examples/molding/templates/proposer.sh"
    assert template.is_file(), "reference proposer template missing"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"                                # consume the prompt on stdin
        "printf '#!/usr/bin/env bash\\nexit 1\\n' > \"$MOLD_ORACLE_DIR/run.sh\"\n"   # fails first
        "printf '#!/usr/bin/env bash\\ntrue\\n' > \"$MOLD_PROBE_FILE\"\n"            # env alive
        "echo 'PROPOSED: reference'\n")
    (bindir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")

    repo = _seed_repo(tmp_path / "repo")
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    result = mold_batch(m, tmp_path / "out", level="oracle", timestamp=TS,
                        proposer=ShellProposer(f"bash {template}"))
    assert result.rows[0].status == VERIFIED             # the canonical template molds end-to-end


def test_proposer_timeout_default_and_override():
    assert MoldDefaults().proposer_timeout == 1800.0
    assert MoldDefaults(proposer_timeout=42.0).proposer_timeout == 42.0


def test_proposer_honours_its_timeout(tmp_path):
    # A proposer that overruns its wall-clock cap is a failed proposal (not a hang) → needs-oracle.
    repo = _seed_repo(tmp_path / "repo")
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/usr/bin/env bash\nsleep 5\n")
    slow.chmod(0o755)
    m = load_mold_manifest(_mold_manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\n', repo))
    result = mold_batch(m, tmp_path / "out", level="oracle", timestamp=TS,
                        proposer=ShellProposer(f"bash {slow}", timeout=0.3))
    assert result.rows[0].status == NEEDS_ORACLE          # timed out → no proposal → skeleton stands
    assert "timed out" in (tmp_path / "out" / "a" / "proposer-notes.md").read_text()
