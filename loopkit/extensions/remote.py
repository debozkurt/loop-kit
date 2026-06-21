"""Remote sync — push the loop's branch and (optionally) open a PR/MR. [Part II]

The core loop is durable *locally*: it commits every tick to its own branch (Ch 15), and never
touches `main`. This module is the **outward** edge — taking that finished branch to GitHub or
GitLab so a human (or CI) can review and merge it. It is deliberately the last, opt-in step:
nothing leaves your machine unless `[remote] enabled = true`, and even then the default is a
*draft* PR so a person merges, not the loop.

Design choices that keep it thin and safe:

- **Shell out to `gh` / `glab`**, never a new Python SDK dependency — same discipline as the agent
  adapters (the model is a subprocess; so is the forge). Missing CLI → a clear message, not a crash.
- **The safety envelope still holds.** `push_branch` refuses any branch in `config.safety.
  forbid_branches` (so a misconfig can't push to `main`) and never force-pushes.
- **Provider is auto-detected** from the remote URL (github.com → `gh`, gitlab → `glab`), so one
  config works for either forge; override with `[remote] provider`.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import secrets
from ..config import Config, RemoteConfig
from ..log import get_logger

_log = get_logger("remote")

# An env-fed credential helper: it echoes the git token from the (re-injected) env *at call time*, so
# the token is never on the command line (visible in `ps`) nor persisted in `.git/config`. Paired with
# `child_env(add=GIT_ENV)` so loopkit's own git authenticates while the agent's scrubbed shell cannot.
# Falls back GitHub → GitLab so an HTTPS push works on either forge (GitLab accepts a PAT as the
# password with any username); `glab`/`gh` read their native token vars from the same re-injected env.
CRED_HELPER = ('!f() { echo username=x; echo '
               '"password=${GITHUB_TOKEN:-${GH_TOKEN:-${GITLAB_TOKEN:-}}}"; }; f')
_USERINFO = re.compile(r"(https?://)[^/@]*@")


def sanitize_git_url(url: str) -> str:
    """Strip any `user:token@` userinfo from an https git URL so a token never lands in `.git/config`."""
    return _USERINFO.sub(r"\1", url or "")


def git_env() -> dict[str, str]:
    """A scrubbed subprocess env with only the git token re-injected (for loopkit's own git/gh)."""
    return secrets.current().child_env(add=secrets.GIT_ENV)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, env=git_env(), capture_output=True, text=True)


def _git_auth(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """git for an authenticated op (push), with the env-fed credential helper (no token in argv)."""
    return subprocess.run(["git", "-c", f"credential.helper={CRED_HELPER}", *args],
                          cwd=repo, env=git_env(), capture_output=True, text=True)


def _scan_push(repo: Path, branch: str, base: str) -> list[str]:
    """Labels of any secret-shaped tokens in the patch about to be pushed (empty list = clean).

    Scans the branch's diff against `base` (falling back to its recent commits if `base` is unknown
    locally). This is the deterministic backstop before a branch reaches the forge — the root control
    is that the agent's shell never holds a key (`secrets.child_env`); this catches the careless path.
    """
    diff = _git(repo, "diff", "--no-color", f"{base}...{branch}")
    patch = diff.stdout if diff.returncode == 0 and diff.stdout else \
        _git(repo, "log", "-p", "--no-color", "-n", "50", branch).stdout
    return secrets.scan_for_secrets(patch or "")


def remote_url(repo: Path, name: str = "origin") -> str | None:
    """The push URL of remote `name`, or None if it isn't configured."""
    out = _git(repo, "remote", "get-url", name)
    return out.stdout.strip() or None if out.returncode == 0 else None


def detect_provider(repo: Path, name: str = "origin") -> str:
    """Infer the forge from the remote URL: 'github' | 'gitlab' | 'unknown'."""
    url = (remote_url(repo, name) or "").lower()
    if "github.com" in url or "github" in url:
        return "github"
    if "gitlab" in url:
        return "gitlab"
    return "unknown"


def push_branch(repo: Path, *, remote: str, branch: str, base: str = "main",
                forbid: list[str] | None = None) -> bool:
    """Push `branch` to `remote`, setting upstream. Refuses forbidden branches; never force-pushes.

    Returns True on a clean push. Two refusals before the network: the forbidden-branch guard (a run
    pushes only its own work, never `main` — Ch 16), and a **pre-push secret scan** — the diff about
    to reach the forge must carry no credential value/shape (Phase 5a; the branch is public the moment
    it lands, before any human sees the draft PR). `detail` is redacted so a scanned secret can't leak
    into the log line itself.
    """
    if branch in (forbid or ["main", "master"]):
        _log.error("push.refused", branch=branch, reason="forbidden_branch")
        return False
    hits = _scan_push(repo, branch, base)
    if hits:
        _log.error("push.refused", branch=branch, reason="secret_scan",
                   hits=",".join(sorted(set(hits))))
        return False
    log = _log.bind(remote=remote, branch=branch)
    proc = _git_auth(repo, "push", "--set-upstream", remote, branch)   # no --force, ever
    if proc.returncode != 0:
        log.error("push.failed", detail=secrets.redact((proc.stderr or proc.stdout).strip()[-200:]))
        return False
    log.info("push.ok")
    return True


def open_pull_request(repo: Path, *, provider: str, branch: str, base: str, title: str,
                      body: str = "", draft: bool = True) -> str | None:
    """Open a PR (GitHub via `gh`) or MR (GitLab via `glab`). Returns the URL, or None on failure.

    Idempotent-ish: `gh`/`glab` refuse a duplicate PR for the same head branch, which we treat as
    a soft success (the branch is already proposed). Missing CLI → a clear log line, not a crash.
    """
    log = _log.bind(provider=provider, branch=branch, base=base)
    if provider == "github":
        cmd = ["gh", "pr", "create", "--head", branch, "--base", base,
               "--title", title, "--body", body or title]
        if draft:
            cmd.append("--draft")
    elif provider == "gitlab":
        cmd = ["glab", "mr", "create", "--source-branch", branch, "--target-branch", base,
               "--title", title, "--description", body or title, "--yes"]
        if draft:
            cmd.append("--draft")
    else:
        log.error("pr.unsupported_provider", provider=provider)
        return None

    try:
        # gh reads GH_TOKEN/GITHUB_TOKEN, glab reads GITLAB_TOKEN — give them the scrubbed env with
        # only those git tokens re-injected (GIT_ENV); no model key, nothing else.
        proc = subprocess.run(cmd, cwd=repo, env=git_env(), capture_output=True, text=True)
    except FileNotFoundError:
        cli = "gh" if provider == "github" else "glab"
        log.error("pr.cli_missing", cli=cli, hint=f"install {cli} to open PRs/MRs")
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    url = next((tok for tok in out.split() if tok.startswith("http")), None)
    if proc.returncode != 0 and url is None:
        log.error("pr.failed", detail=out.strip()[-200:])
        return None
    log.info("pr.ok", url=url or "-")
    return url


def sync_done(config: Config, repo: Path, *, title: str | None = None, body: str = "",
              issue: int | None = None) -> dict:
    """Run the configured outward steps for a finished branch: push, then (optionally) open a PR.

    Called once, after a run reaches DONE. Honours `config.remote`: a no-op when `enabled` is
    False, so the default install never reaches the network. When `issue` is given, the PR body is
    annotated so the forge auto-links (and closes) the source issue on merge.
    """
    rc: RemoteConfig = config.remote
    branch = config.branch
    result: dict = {"pushed": False, "pr_url": None}
    if not rc.enabled:
        return result

    if rc.push:
        result["pushed"] = push_branch(repo, remote=rc.name, branch=branch, base=rc.pr_base,
                                       forbid=config.safety.forbid_branches)
        if not result["pushed"]:
            return result                      # don't try to open a PR for an un-pushed branch

    if rc.open_pr:
        provider = rc.provider if rc.provider != "auto" else detect_provider(repo, rc.name)
        pr_title = title or f"loopkit: {config.goal[:60]}"
        pr_body = body or f"Automated by loopkit on branch `{branch}`.\n\nGoal: {config.goal}"
        if issue is not None:
            # "Closes #N" / "Closes !N issue ref" — GitHub + GitLab both auto-link + close on merge.
            pr_body += f"\n\nCloses #{issue}"
        result["pr_url"] = open_pull_request(repo, provider=provider, branch=branch,
                                             base=rc.pr_base, title=pr_title, body=pr_body,
                                             draft=rc.draft)
    return result
