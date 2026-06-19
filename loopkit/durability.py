"""Durability: commit every tick so a crash loses one tick, not the run (Chapter 15).

State lives in git, not in memory. After every tick the loop commits the working tree, so the
run is resumable (re-run and it continues from HEAD) and idempotent at the commit boundary.
The state signature — a hash of HEAD plus the working-tree diff plus untracked files — is the
progress sensor the no-progress stop reads.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


def is_git_repo(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--git-dir").returncode == 0


def current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def is_clean(repo: Path) -> bool:
    return _git(repo, "status", "--porcelain").stdout.strip() == ""


def changed_paths(repo: Path) -> list[str]:
    """Paths differing from HEAD right now (staged, unstaged, or untracked).

    Uses --untracked-files=all so a brand-new nested file is listed individually; the default
    collapses a new directory to its top dir (`tests/`), which would let a granular protected
    path (`tests/holdout/`) slip the guard.
    """
    lines = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return [line[3:] for line in lines if line.strip()]


def state_signature(repo: Path) -> str:
    """A stable short hash of the current working state — HEAD plus the full diff."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    diff = _git(repo, "diff", "HEAD").stdout
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").stdout
    digest = hashlib.sha256()
    digest.update(head.encode())
    digest.update(diff.encode())
    digest.update(untracked.encode())
    return digest.hexdigest()[:16]


def commit_progress(repo: Path, message: str) -> bool:
    """Commit the working tree if it changed. Returns True iff a commit was made."""
    if is_clean(repo):
        return False
    _git(repo, "add", "-A")
    return _git(repo, "commit", "-m", message).returncode == 0


def revert_uncommitted(repo: Path) -> None:
    """Discard all uncommitted changes (tracked and untracked) — undo a bad tick."""
    _git(repo, "checkout", "--", ".")
    _git(repo, "clean", "-fdq")


def ensure_branch(repo: Path, branch: str) -> None:
    """Switch to `branch`, creating it from HEAD if it doesn't exist."""
    if current_branch(repo) == branch:
        return
    if _git(repo, "rev-parse", "--verify", branch).returncode == 0:
        _git(repo, "checkout", branch, check=True)
    else:
        _git(repo, "checkout", "-b", branch, check=True)
