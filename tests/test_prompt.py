"""Prompt anchor assembly — including anchors that resolve OUTSIDE the repo.

An anchor may be a shared prompt file referenced by absolute path (outside the workspace). The
label line used `path.relative_to(repo)`, which raises ValueError for such a path and crashed the
whole prompt build. These tests pin that an external anchor is read (labelled by name) and an
in-repo anchor is read (labelled by its relative path).
"""
from __future__ import annotations

from pathlib import Path

from loopkit.prompt import read_anchors


def test_in_repo_anchor_is_read_with_relative_label(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PROMPT.md").write_text("in-repo guidance")
    out = read_anchors(repo, ["PROMPT.md"])
    assert "in-repo guidance" in out
    assert "# --- PROMPT.md ---" in out


def test_external_anchor_does_not_crash_and_is_labelled_by_name(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shared = tmp_path / "shared-guidance.md"          # outside the repo
    shared.write_text("shared guidance from outside the repo")
    # Absolute path to a file outside the repo — previously raised ValueError in relative_to.
    out = read_anchors(repo, [str(shared)])
    assert "shared guidance from outside the repo" in out
    assert "# --- shared-guidance.md ---" in out       # labelled by name, not a crash
