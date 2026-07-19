"""Characterization net for the CLI surface — the safety net for the cli.py -> cli/ package refactor.

It snapshots the *composed* command tree (every command's full path + its parameter names) by
introspecting the top-level `app` exactly as a user / `CliRunner` sees it. A structural refactor that
drops a command, renames a flag, or fails to register a sub-app changes this snapshot and fails here,
even if the behavioral tests happen not to exercise that command. Behavior is covered elsewhere; this
guards the *shape*.

If you intentionally add/remove a command or option, update EXPECTED in the same change.
"""
from __future__ import annotations

import typer

from loopkit.cli import app

# The full CLI surface: command path -> sorted parameter names. Captured 2026-06-29.
EXPECTED: dict[str, list[str]] = {
    "cloud bootstrap": ["context", "kubeconfig", "yes"],
    "cloud context": ["context", "kubeconfig"],
    "cloud creds ls": ["kubeconfig"],
    "cloud creds rm": ["as_submitter", "context", "env_name", "kubeconfig", "yes"],
    "cloud creds set": ["adapter", "as_submitter", "context", "env_name", "kubeconfig", "yes"],
    "cloud doctor": ["context", "kubeconfig"],
    "cloud kill": ["context", "kubeconfig", "run", "yes"],
    "cloud logs": ["kubeconfig", "role", "run", "tail"],
    "cloud ls": ["kubeconfig"],
    "cloud run": [
        "adapter", "allow_fleet_fallback", "as_submitter", "context", "env_name", "evolve",
        "from_env", "from_issues", "generations", "goal", "image", "in_cluster", "keep",
        "kubeconfig", "label", "name", "node_pool", "population", "provider", "skills_branch",
        "skills_repo", "target", "workers", "yes",
    ],
    "cloud schedule": [
        "adapter", "allow_fleet_fallback", "as_submitter", "context", "cron", "env_name",
        "from_issues", "goal", "image", "in_cluster", "kubeconfig", "label", "name", "provider",
        "target", "workers", "yes",
    ],
    "cloud schedules": ["in_cluster", "kubeconfig"],
    "cloud status": ["kubeconfig", "run"],
    "cloud unschedule": ["context", "in_cluster", "kubeconfig", "name", "yes"],
    "cloud webhook": [
        "adapter", "allow_fleet_fallback", "as_submitter", "context", "env_name", "host", "image",
        "label", "port", "provider", "redis_url", "secret", "workers",
    ],
    "batch": ["dry_run", "jobs", "no_review", "only", "open_pr", "out", "provider", "resume",
              "tasks_file", "timeout"],
    "demo": ["chapter", "live"],
    "detect": ["force", "out", "repo", "write"],
    "doctor": ["config", "gate"],
    "executor": ["socket_path"],
    "fleet evolve": ["drain_workers", "generations", "keep", "population", "redis_namespace", "redis_url"],
    "fleet run": [
        "drain_workers", "from_issues", "goal", "label", "provider", "redis_namespace",
        "redis_url", "target", "tasks",
    ],
    "fleet worker": [
        "adapter", "executor_socket", "gate_acceptance", "gate_iteration", "max_iter", "name",
        "redis_namespace", "redis_url", "skills_branch", "skills_repo", "target",
    ],
    "init": ["ci", "path", "plan"],
    "learn": ["chapter", "live"],
    "mold-batch": ["dry_run", "force", "level", "limit", "out_dir", "proposer", "provider",
                   "tasks_file"],
    "measure": ["adapter", "config", "from_issue", "k", "max_iter", "mode", "out", "provider", "repo", "trials"],
    "overlap": ["tasks_file"],
    "route": [
        "adapter", "config", "from_issue", "from_report", "k", "max_iter", "mode", "out",
        "provider", "repo", "threshold", "trials",
    ],
    "review": ["backend", "base", "config", "criteria", "goal", "model", "repo"],
    "run": [
        "adapter", "api_key", "branch", "check_gate", "config", "dry_run", "force", "from_event",
        "from_issue", "max_iter", "no_review", "open_pr", "provider", "repo", "review", "sandbox",
        "skills", "skills_distiller", "validate",
    ],
    "synth-gate": ["config", "fix", "isolate", "mode", "oracle", "out", "repo"],
}


def _surface(command, prefix: str = "") -> dict[str, list[str]]:
    """Walk the composed Click command tree -> {full command path: sorted param names}."""
    out: dict[str, list[str]] = {}
    subcommands = getattr(command, "commands", None)
    if subcommands:                                   # a group (the root app or a sub-app)
        for name, sub in subcommands.items():
            out.update(_surface(sub, f"{prefix}{name} "))
    else:                                             # a leaf command
        out[prefix.strip()] = sorted(p.name for p in command.params)
    return out


def test_cli_surface_matches_snapshot():
    surface = _surface(typer.main.get_command(app))
    assert surface == EXPECTED


def test_every_command_group_is_mounted():
    """The four sub-app namespaces must all be reachable from the composed app."""
    paths = _surface(typer.main.get_command(app))
    namespaces = {p.split(" ")[0] for p in paths if " " in p}
    assert {"cloud", "fleet"} <= namespaces
    assert any(p.startswith("cloud creds ") for p in paths)   # the nested creds sub-app
