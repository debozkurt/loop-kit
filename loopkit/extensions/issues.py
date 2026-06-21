"""Issue-sourced tasks — let GitHub/GitLab issues be the fleet's work queue. [Part II]

The fleet's coordinator enqueues task dicts (Ch 12); where those dicts come from is a *source*.
The simplest hand-written source is a list of goals. This module is a richer one: read open issues
off a forge and map each into a task, so a labelled backlog becomes the fleet's queue and the loop
is driven by *events you didn't trigger* — the Ch 12 trigger idea, made concrete. Solve the issue
on its own branch, and (with `[remote]` enabled) the PR that lands closes it.

Same discipline as `remote.py`: **shell out to `gh` / `glab`** with JSON output, no SDK dependency.
A missing CLI or a forge error returns an empty list with a clear log line, never a crash — an
empty queue is a valid (if boring) fleet.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import secrets
from ..log import get_logger
from .remote import detect_provider, git_env

_log = get_logger("issues")


def fetch_github_issues(repo: Path, *, label: str | None = None, limit: int = 20,
                        state: str = "open") -> list[dict]:
    """Open issues via `gh issue list --json` (returns [] if gh is missing/unauthed/errors)."""
    cmd = ["gh", "issue", "list", "--state", state, "--limit", str(limit),
           "--json", "number,title,body,url,labels"]
    if label:
        cmd += ["--label", label]
    raw = _run_json(repo, cmd, cli="gh")
    return [{"number": i["number"], "title": i.get("title", ""), "body": i.get("body") or "",
             "url": i.get("url", "")} for i in raw]


def fetch_gitlab_issues(repo: Path, *, label: str | None = None, limit: int = 20,
                        state: str = "opened") -> list[dict]:
    """Open issues via `glab issue list --output json` (returns [] if glab is missing/errors)."""
    cmd = ["glab", "issue", "list", "--output", "json", "--per-page", str(limit)]
    if state == "open":
        state = "opened"                      # glab's vocabulary
    if label:
        cmd += ["--label", label]
    raw = _run_json(repo, cmd, cli="glab")
    return [{"number": i.get("iid") or i.get("id"), "title": i.get("title", ""),
             "body": i.get("description") or "", "url": i.get("web_url", "")} for i in raw]


def fetch_issues(repo: Path, *, provider: str = "auto", label: str | None = None,
                 limit: int = 20, remote: str = "origin") -> list[dict]:
    """Dispatch to the right forge. `provider='auto'` detects it from the remote URL."""
    resolved = provider if provider != "auto" else detect_provider(repo, remote)
    log = _log.bind(provider=resolved, label=label or "-")
    if resolved == "github":
        issues = fetch_github_issues(repo, label=label, limit=limit)
    elif resolved == "gitlab":
        issues = fetch_gitlab_issues(repo, label=label, limit=limit)
    else:
        log.error("fetch.unsupported_provider", provider=resolved,
                  hint="set [remote] provider or a github/gitlab remote")
        return []
    log.info("fetch.done", count=len(issues))
    return issues


def issue_to_task(issue: dict, *, base_branch: str = "loopkit") -> dict:
    """Map one issue into a fleet task dict (the wire shape the coordinator/worker consume).

    The goal is the issue's title + body verbatim — that *is* the spec. The slug/branch/id are
    derived from the issue number so the work is traceable end to end: branch `loopkit/issue-42`,
    task id `issue-42`, and `issue=42` so a PR can close it.
    """
    number = issue["number"]
    title = (issue.get("title") or "").strip()
    body = (issue.get("body") or "").strip()
    goal = f"{title}\n\n{body}".strip() if body else title
    return {"id": f"issue-{number}", "slug": f"issue-{number}",
            "branch": f"{base_branch}/issue-{number}", "issue": number,
            "title": title, "goal": goal}


def issues_to_tasks(issues: list[dict], *, base_branch: str = "loopkit") -> list[dict]:
    """Map a batch of issues into fleet tasks (skips any with an empty title — nothing to solve)."""
    tasks = [issue_to_task(i, base_branch=base_branch) for i in issues if (i.get("title") or "").strip()]
    _log.info("issues.mapped", issues=len(issues), tasks=len(tasks))
    return tasks


def _run_json(repo: Path, cmd: list[str], *, cli: str) -> list[dict]:
    """Run a forge CLI expected to print a JSON array; return [] on any failure (logged)."""
    try:
        # gh/glab read GH_TOKEN/GITHUB_TOKEN from env — give them the scrubbed env with only the git
        # token re-injected. Redact the error detail so a token can't leak into the log line.
        proc = subprocess.run(cmd, cwd=repo, env=git_env(), capture_output=True, text=True)
    except FileNotFoundError:
        _log.error("cli.missing", cli=cli, hint=f"install {cli} and authenticate it")
        return []
    if proc.returncode != 0:
        _log.error("cli.failed", cli=cli,
                   detail=secrets.redact((proc.stderr or proc.stdout).strip()[-200:]))
        return []
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        _log.error("cli.bad_json", cli=cli)
        return []
    return data if isinstance(data, list) else []
