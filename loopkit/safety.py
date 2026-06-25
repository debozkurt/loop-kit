"""Safety: bound the blast radius before the loop runs, and after every tick (Chapter 16).

Loop safety is mostly blast-radius containment. The pre-flight checks refuse to run in a
dangerous configuration (on main, on a dirty tree, on a forbidden branch). The post-tick
check enforces the one invariant the held-out gate depends on: the loop must never touch a
protected path — for example, the acceptance tests it isn't allowed to see (Ch 9).
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from . import durability


@dataclass
class Preflight:
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class GateStability:
    """The result of running one gate N times on an unchanged tree (see `gate_stability`)."""

    deterministic: bool
    runs: int
    verdicts: list[bool] = field(default_factory=list)   # one pass/fail per run

    @property
    def passes(self) -> int:
        return sum(self.verdicts)


def gate_stability(gate, workspace: Path, runs: int) -> GateStability:
    """Run `gate` `runs` times on the unchanged `workspace` and report whether the verdict is stable.

    A non-deterministic gate (a different pass/fail on identical state) corrupts every stop decision
    the loop makes: it will "fix" code that is already correct, or halt on code that is broken. A
    flaky gate is worse than no gate (Ch 9). Run this once, on the initial tree, before trusting the
    gate as the loop's stop oracle. The verdict need not be *pass* — a loop legitimately starts red —
    only *stable*: all `runs` agree.

    `gate` is duck-typed (anything with `.check(workspace).passed`), so this stays decoupled from the
    concrete `Gate` implementations. It is read-only with respect to the loop's state; the caller runs
    it before any tick, on a frozen tree.
    """
    verdicts = [bool(gate.check(workspace).passed) for _ in range(runs)]
    return GateStability(deterministic=len(set(verdicts)) <= 1, runs=runs, verdicts=verdicts)


def preflight(config) -> Preflight:
    """Return the list of reasons it is unsafe to start this run (empty == safe)."""
    repo = config.repo_path()
    problems: list[str] = []

    if not durability.is_git_repo(repo):
        return Preflight(False, [f"{repo} is not a git repository"])

    branch = config.branch
    for pattern in config.safety.forbid_branches:
        if fnmatch.fnmatch(branch, pattern):
            problems.append(f"configured branch {branch!r} is forbidden (matches {pattern!r})")
    if config.safety.allow_branches and not any(
        fnmatch.fnmatch(branch, p) for p in config.safety.allow_branches
    ):
        problems.append(f"branch {branch!r} matches none of allow_branches "
                        f"{config.safety.allow_branches}")
    if config.safety.require_clean_tree and not durability.is_clean(repo):
        problems.append("working tree is dirty; commit or stash first "
                        "(or set safety.require_clean_tree = false)")
    if config.gate.acceptance and not config.safety.protected_paths:
        problems.append("an acceptance gate is set but no protected_paths guard it; "
                        "the loop could optimize against the held-out checks")
    return Preflight(not problems, problems)


def protected_violations(config) -> list[str]:
    """Paths the working tree currently changes that fall under a protected path (Ch 9 + 16)."""
    bad: list[str] = []
    for path in durability.changed_paths(config.repo_path()):
        for guard in config.safety.protected_paths:
            g = guard.rstrip("/")
            if path == g or path.startswith(g + "/") or fnmatch.fnmatch(path, guard):
                bad.append(path)
                break
    return bad
