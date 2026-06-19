"""Shared fixtures. The git_repo fixture gives each test an isolated, committed repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh repo on `main` with one seed commit and a clean working tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "branch", "-m", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "loopkit-test")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo
