"""Overlap tests: touch extraction, collision analysis, suggestions, and the two CLI surfaces.

The analysis is pure functions over specs (zero tokens), so most tests build `TaskSpec`s directly.
What matters contractually: the trust tiers (explicit `touches` beats goal text beats honest NONE),
covered-pair detection (an existing group or `after` path silences the suggestion), the advisory
posture (exit 0 always, `batch` warnings never block), and the mold → batch `touches` pass-through.
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from loopkit.extensions.batch import TaskSpec
from loopkit.extensions.mold import READY, MoldManifest, emit_batch_manifest
from loopkit.extensions.overlap import EXPLICIT, FROM_GOAL, NONE, analyze, touches_for


def _spec(id: str, **kw) -> TaskSpec:
    kw.setdefault("goal", f"solve {id}")
    return TaskSpec(id=id, **kw)


# --------------------------------------------------------------------------------------------
# Touch extraction — the trust tiers
# --------------------------------------------------------------------------------------------
def test_explicit_touches_beat_goal_text():
    t = touches_for(_spec("a", goal="see src/other/file.py", touches=["src/db/db.go"]))
    assert t.source == EXPLICIT and t.paths == {"src/db/db.go"}


def test_goal_paths_extracted_with_line_refs_stripped():
    t = touches_for(_spec("a", goal="fix `src/core/settings/base.py:421-441` and src/core/urls.py."))
    assert t.source == FROM_GOAL
    assert t.paths == {"src/core/settings/base.py", "src/core/urls.py"}


def test_goal_extraction_skips_urls_and_bare_slashes():
    t = touches_for(_spec("a", goal="see https://forge.example/repo/handlers/search.go and/or docs"))
    assert t.source == NONE and t.paths == frozenset()


def test_no_touch_data_is_honest_none():
    assert touches_for(_spec("a", goal="make it faster")).source == NONE
    assert touches_for(TaskSpec(id="b", issue=7)).source == NONE   # issue-sourced, unfetched


# --------------------------------------------------------------------------------------------
# Analysis — collisions, coverage, suggestions
# --------------------------------------------------------------------------------------------
def test_shared_path_is_an_uncovered_collision_with_suggestion():
    report = analyze([_spec("a", touches=["src/settings/base.py"]),
                      _spec("b", touches=["src/settings/base.py", "src/x.py"])])
    [c] = report.collisions
    assert (c.a, c.b, c.covered) == ("a", "b", False)
    assert c.paths == ("src/settings/base.py",)
    assert report.suggestions == {"a": "base", "b": "base"}   # named from the shared path's stem
    assert report.components == [["a", "b"]]


def test_shared_group_marks_collision_covered_and_silences_suggestion():
    report = analyze([_spec("a", touches=["f.py"], group="db"),
                      _spec("b", touches=["f.py"], group="db")])
    assert report.collisions[0].covered and not report.suggestions


def test_after_path_covers_transitively_in_either_direction():
    specs = [_spec("a", touches=["f.py"]),
             _spec("b", after=["a"]),
             _spec("c", touches=["f.py"], after=["b"])]     # c -> b -> a connects c to a
    report = analyze(specs)
    [c] = report.collisions
    assert c.covered and not report.suggestions


def test_disjoint_tasks_produce_no_collisions():
    report = analyze([_spec("a", touches=["x.py"]), _spec("b", touches=["y.py"])])
    assert not report.collisions and not report.suggestions and not report.components


def test_unanalyzed_tasks_are_reported_never_assumed_safe():
    report = analyze([_spec("a", goal="vague"), _spec("b", touches=["x.py"])])
    assert report.unanalyzed == ["a"]


def test_suggested_group_names_stay_unique_across_components():
    report = analyze([_spec("a", touches=["one/base.py"]), _spec("b", touches=["one/base.py"]),
                      _spec("c", touches=["two/base.py"]), _spec("d", touches=["two/base.py"])])
    names = {report.suggestions["a"], report.suggestions["c"]}
    assert names == {"base", "base-2"}


def test_existing_group_member_keeps_its_group_only_ungrouped_get_suggestions():
    report = analyze([_spec("a", touches=["f.py"], group="db"),
                      _spec("b", touches=["f.py"])])
    assert "a" not in report.suggestions and report.suggestions["b"] == "f"


# --------------------------------------------------------------------------------------------
# CLI — `loopkit overlap` + the `batch` advisory warning
# --------------------------------------------------------------------------------------------
def _invoke(monkeypatch, tmp_path: Path, command: str, *args: str):
    from loopkit.cli import app
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir(exist_ok=True)
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))
    return CliRunner().invoke(app, [command, *args])


def _manifest(tmp_path: Path, text: str, name: str = "batch.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


_OVERLAPPING = """
[[task]]
id = "a"
goal = "fix src/handlers/search.go truncation"

[[task]]
id = "b"
goal = "clamp the limit in src/handlers/search.go"

[[task]]
id = "c"
goal = "unrelated docs pass"
"""


def test_cli_overlap_reports_suggestions_and_exits_zero(monkeypatch, tmp_path):
    mf = _manifest(tmp_path, _OVERLAPPING)
    result = _invoke(monkeypatch, tmp_path, "overlap", "--tasks", str(mf))
    assert result.exit_code == 0, result.output
    assert "a ↔ b" in result.output
    assert '[[task]] id = "a"' in result.output           # literal TOML, not eaten as rich markup
    assert 'group = "search"' in result.output            # stem-named suggestion, copy-pasteable
    assert "unanalyzed" in result.output and "c" in result.output
    assert "advisory" in result.output


def test_cli_overlap_reads_a_mold_plan_and_clean_bill_is_explicit(monkeypatch, tmp_path):
    mf = _manifest(tmp_path, """
[defaults]
repo = "/tmp/somewhere"

[[task]]
id = "a"
goal = "fix a"
tier = "authz"
touches = ["src/a.py"]

[[task]]
id = "b"
goal = "fix b"
tier = "correctness"
touches = ["src/b.py"]
""", name="plan.toml")
    result = _invoke(monkeypatch, tmp_path, "overlap", "--tasks", str(mf))
    assert result.exit_code == 0, result.output
    assert "no predicted overlaps" in result.output


def test_batch_dry_run_warns_on_undeclared_overlap_only(monkeypatch, tmp_path):
    import subprocess
    src = tmp_path / "src"
    src.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@l"), ("config", "user.name", "t")):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)
    (src / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=src, check=True, capture_output=True)
    (tmp_path / "base.toml").write_text(
        f'goal = "placeholder"\nrepo = "{src}"\n\n[gate]\niteration = "true"\nacceptance = "true"\n')
    mf = _manifest(tmp_path, '[defaults]\nconfig = "base.toml"\n' + _OVERLAPPING)
    result = _invoke(monkeypatch, tmp_path, "batch", "--tasks", str(mf), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "predicted overlap" in result.output and "a ↔ b" in result.output

    covered = _manifest(tmp_path, '[defaults]\nconfig = "base.toml"\n'
                        + _OVERLAPPING.replace('goal = "fix src/handlers/search.go truncation"',
                                               'goal = "fix src/handlers/search.go truncation"\n'
                                               'group = "handlers"')
                                     .replace('goal = "clamp the limit in src/handlers/search.go"',
                                              'goal = "clamp the limit in src/handlers/search.go"\n'
                                              'group = "handlers"'),
                        name="covered.toml")
    result = _invoke(monkeypatch, tmp_path, "batch", "--tasks", str(covered), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "predicted overlap" not in result.output       # declared pair stays silent


# --------------------------------------------------------------------------------------------
# Mold pass-through — `touches` rides plan.toml → emitted batch.toml
# --------------------------------------------------------------------------------------------
def test_mold_emit_passes_touches_through(tmp_path):
    manifest = MoldManifest.model_validate({
        "defaults": {"repo": str(tmp_path)},
        "task": [{"id": "a", "goal": "fix a", "touches": ["src/a.py", "src/b.py"]}],
    })
    state = {"tasks": {"a": {"status": READY}}}
    path = emit_batch_manifest(manifest, tmp_path, state)
    assert 'touches = ["src/a.py", "src/b.py"]' in path.read_text()
