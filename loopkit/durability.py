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

# Loopkit-core runs git inside a workspace the (untrusted) executor can also write to — the shared
# emptyDir in the Phase-6 split, and (on the no-sidecar CI/local tiers) a tree a same-uid `run_bash`
# can edit. Left alone, git would execute workspace-controlled hooks (`.git/hooks/*`) and honor a
# workspace-written `.git/config` (`core.fsmonitor` runs a command) — i.e. run attacker-chosen code AS
# loopkit-core, the credential holder, defeating the agent-isolation boundary (Finding A,
# docs/part-iii-security-review.md). Pinning these on the command line is highest-precedence, so it
# overrides anything an injected `.git/config` sets: hooks are looked up under `/dev/null` (none exist)
# and fsmonitor is forced off. Applied to EVERY loopkit-core git call (here + `remote.run_git`), so the
# vector is closed on all three tiers. `credential.helper` is additionally reset on authenticated ops
# (see `remote.run_git`) so an injected helper can't capture the token.
HARDENED_GIT_FLAGS: tuple[str, ...] = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *HARDENED_GIT_FLAGS, *args], cwd=repo,
                          capture_output=True, text=True, check=check)


def is_git_repo(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--git-dir").returncode == 0


def current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def current_head(repo: Path) -> str:
    """The current HEAD commit sha (empty before the first commit). Used to detect that the tick
    advanced HEAD — whether loopkit's own commit fired OR the agent self-committed."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


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
