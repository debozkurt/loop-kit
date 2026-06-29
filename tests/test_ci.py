"""Tests for the CI deployment tier (Part III, Phase 5c) — `loopkit run` driven by a forge issue.

The CI tier is glue over already-tested parts (`parse_event`, `issues.fetch_issue`,
`remote.sync_done`), so this file proves the *wiring*, token-free and offline: an issue event/number
becomes the run's goal, `--open-pr` flips the outward edge on, and the captured issue number rides
into `sync_done` so the PR closes the issue on merge. `MockAgent` + a `gate = true` reach DONE with
zero spend; the forge CLIs and the network are never touched (`sync_done`/`fetch_issue` are stubbed).
The `init --ci` scaffold + the shipped `examples/ci/` templates are checked here too.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.cli import (
    _CI_GITHUB_CLAUDE_CODE_TEMPLATE,
    _CI_GITHUB_TEMPLATE,
    _CI_GITLAB_CLAUDE_CODE_TEMPLATE,
    _CI_GITLAB_TEMPLATE,
    app,
)

runner = CliRunner()
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _norm(output: str) -> str:
    """Collapse rich's line-wrapping so a phrase assertion isn't broken by a wrap-inserted newline."""
    return " ".join(output.split())


# A minimal loopkit.toml whose iteration gate (`true`) always passes, so a no-op MockAgent reaches
# DONE on tick 1 — the run mechanics are exercised elsewhere; here we only care about the CI glue.
_CONFIG = """\
goal = "placeholder — overridden by --from-event/--from-issue"
repo = "."
branch = "loopkit/run"

[agent]
adapter = "mock"

[gate]
iteration = "true"

[safety]
protected_paths = []
"""

_GH_EVENT = {
    "action": "opened",
    "issue": {"number": 7, "title": "Add a /health endpoint",
              "body": "Return 200 OK at /health.",
              "user": {"login": "alice"}, "labels": [{"name": "loopkit"}]},
    "repository": {"full_name": "acme/widgets", "clone_url": "https://github.com/acme/widgets.git"},
}


@pytest.fixture
def clean_creds(monkeypatch, tmp_path):
    """Point credential loading at an empty dir so `secrets.install` is a clean no-op.

    `loopkit run` loads creds first thing; an empty `LOOPKIT_CREDS_DIR` makes that read nothing and —
    crucially — never scrub the developer's real os.environ (the no-dir branch would). Also silence
    tracing so a stray LangSmith key in the dev env can't reach the network during a unit test.
    """
    d = tmp_path / "creds"
    d.mkdir()
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(d))
    for var in ("LANGSMITH_API_KEY", "LANGSMITH_TRACING", "LANGCHAIN_API_KEY", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(var, raising=False)
    return d


def _write_config(repo: Path) -> Path:
    # Commit the config so the working tree is clean — mirrors a real CI checkout and lets the run
    # pass the require_clean_tree preflight (an untracked loopkit.toml would read as a dirty tree).
    toml = repo / "loopkit.toml"
    toml.write_text(_CONFIG)
    subprocess.run(["git", "add", "loopkit.toml"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "add loopkit.toml"], cwd=repo, check=True, capture_output=True)
    return toml


# --------------------------------------------------------------------------------------------
# --from-event — goal sourced from a forge issue-event JSON, issue number threaded into the PR.
# --------------------------------------------------------------------------------------------
def test_run_from_event_sets_goal_and_opens_pr_with_issue(git_repo, tmp_path, monkeypatch, clean_creds):
    toml = _write_config(git_repo)
    event = tmp_path / "event.json"
    event.write_text(json.dumps(_GH_EVENT))

    captured: dict = {}

    def fake_sync_done(config, repo, *, title=None, body="", issue=None):
        captured.update(goal=config.goal, issue=issue, title=title,
                        enabled=config.remote.enabled, open_pr=config.remote.open_pr)
        return {"pushed": True, "pr_url": "https://github.com/acme/widgets/pull/1"}

    monkeypatch.setattr("loopkit.extensions.remote.sync_done", fake_sync_done)
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-event", str(event), "--adapter", "mock", "--open-pr"])

    assert result.exit_code == 0, result.output
    # The issue's title+body became the goal verbatim (the same builder the webhook path uses).
    assert captured["goal"].startswith("Add a /health endpoint")
    assert "Return 200 OK at /health." in captured["goal"]
    # --open-pr flipped the outward edge on, and the issue number rode into sync_done for `Closes #7`.
    assert captured["enabled"] is True and captured["open_pr"] is True
    assert captured["issue"] == 7
    assert captured["title"] == "loopkit: Add a /health endpoint"   # single-line PR title


def test_run_from_event_does_not_open_pr_without_flag(git_repo, tmp_path, monkeypatch, clean_creds):
    # Without --open-pr the run still sets the goal but never reaches the outward edge (remote off).
    toml = _write_config(git_repo)
    event = tmp_path / "event.json"
    event.write_text(json.dumps(_GH_EVENT))

    called = {"sync": False}
    monkeypatch.setattr("loopkit.extensions.remote.sync_done",
                        lambda *a, **k: called.__setitem__("sync", True) or {})
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-event", str(event), "--adapter", "mock"])
    assert result.exit_code == 0, result.output
    assert called["sync"] is False        # [remote] stayed off → no push/PR attempt


def test_run_from_gitlab_event_payload(git_repo, tmp_path, monkeypatch, clean_creds):
    # A GitLab issue payload (object_kind) is auto-detected by shape — same glue, other forge.
    toml = _write_config(git_repo)
    gl_event = {"object_kind": "issue",
                "object_attributes": {"action": "open", "iid": 12, "title": "Cache the feed",
                                      "description": "TTL 60s"},
                "project": {"path_with_namespace": "grp/app",
                            "git_http_url": "https://gitlab.com/grp/app.git"}}
    event = tmp_path / "gl.json"
    event.write_text(json.dumps(gl_event))

    captured: dict = {}
    monkeypatch.setattr("loopkit.extensions.remote.sync_done",
                        lambda config, repo, *, title=None, body="", issue=None:
                        captured.update(goal=config.goal, issue=issue) or {"pushed": True, "pr_url": None})
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-event", str(event), "--adapter", "mock", "--open-pr"])
    assert result.exit_code == 0, result.output
    assert captured["issue"] == 12
    assert captured["goal"].startswith("Cache the feed")


def test_run_from_event_rejects_non_issue_payload(git_repo, tmp_path, clean_creds):
    # A workflow_dispatch / push payload has no actionable issue → a clean refusal, not a crash.
    toml = _write_config(git_repo)
    event = tmp_path / "dispatch.json"
    event.write_text(json.dumps({"inputs": {"issue": "5"}}))     # no issue/object_kind
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-event", str(event), "--adapter", "mock", "--open-pr"])
    assert result.exit_code == 1
    assert "no actionable issue" in _norm(result.output)


def test_run_from_event_and_from_issue_are_mutually_exclusive(git_repo, clean_creds):
    toml = _write_config(git_repo)
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-event", "e.json", "--from-issue", "5", "--adapter", "mock"])
    assert result.exit_code == 1
    assert "only one of --from-event or --from-issue" in _norm(result.output)


# --------------------------------------------------------------------------------------------
# --from-issue — goal sourced by fetching one issue by number (gh/glab stubbed).
# --------------------------------------------------------------------------------------------
def test_run_from_issue_fetches_and_sets_goal(git_repo, monkeypatch, clean_creds):
    toml = _write_config(git_repo)

    def fake_fetch_issue(repo, number, *, provider="auto", remote="origin"):
        assert number == 42 and provider == "gitlab"
        return {"number": 42, "title": "Paginate results", "body": "20 per page", "url": "u"}

    captured: dict = {}
    monkeypatch.setattr("loopkit.extensions.issues.fetch_issue", fake_fetch_issue)
    monkeypatch.setattr("loopkit.extensions.remote.sync_done",
                        lambda config, repo, *, title=None, body="", issue=None:
                        captured.update(goal=config.goal, issue=issue) or {"pushed": True, "pr_url": None})
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-issue", "42", "--provider", "gitlab",
                                 "--adapter", "mock", "--open-pr"])
    assert result.exit_code == 0, result.output
    assert captured["issue"] == 42
    assert captured["goal"].startswith("Paginate results")
    assert "20 per page" in captured["goal"]


def test_run_from_issue_errors_when_fetch_fails(git_repo, monkeypatch, clean_creds):
    toml = _write_config(git_repo)
    monkeypatch.setattr("loopkit.extensions.issues.fetch_issue", lambda *a, **k: None)
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-issue", "99", "--adapter", "mock"])
    assert result.exit_code == 1
    assert "could not fetch issue #99" in _norm(result.output)


# --------------------------------------------------------------------------------------------
# --branch — per-run branch isolation so concurrent issue→PR runs don't collide on one branch.
# --------------------------------------------------------------------------------------------
def _stub_issue(monkeypatch):
    """gh/glab fetch → a fixed issue, so --from-issue reaches the run without touching the network."""
    monkeypatch.setattr("loopkit.extensions.issues.fetch_issue",
                        lambda repo, number, *, provider="auto", remote="origin":
                        {"number": number, "title": "Add X", "body": "do X", "url": "u"})


def test_run_branch_override_sets_the_durable_branch(git_repo, monkeypatch, clean_creds):
    # --branch overrides config `branch`: the run lands on it AND it rides into sync_done (so the PR
    # opens from the per-issue branch, not the shared default).
    toml = _write_config(git_repo)
    _stub_issue(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("loopkit.extensions.remote.sync_done",
                        lambda config, repo, *, title=None, body="", issue=None:
                        captured.update(branch=config.branch, issue=issue) or {"pushed": True, "pr_url": None})
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-issue", "42", "--branch", "loopkit/issue-42",
                                 "--adapter", "mock", "--open-pr"])
    assert result.exit_code == 0, result.output
    assert captured["branch"] == "loopkit/issue-42"      # the override reached the outward edge
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo,
                          capture_output=True, text=True).stdout.strip()
    assert head == "loopkit/issue-42"                    # the loop actually switched to it (durability)


def test_run_without_branch_uses_config_default(git_repo, monkeypatch, clean_creds):
    # No --branch → config `branch` ("loopkit/run") is untouched. Locks against a regression where the
    # override default ("" / None) leaks over the configured value.
    toml = _write_config(git_repo)
    _stub_issue(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("loopkit.extensions.remote.sync_done",
                        lambda config, repo, *, title=None, body="", issue=None:
                        captured.update(branch=config.branch) or {"pushed": True, "pr_url": None})
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-issue", "42", "--adapter", "mock", "--open-pr"])
    assert result.exit_code == 0, result.output
    assert captured["branch"] == "loopkit/run"


def test_run_branch_override_is_still_safety_checked(git_repo, monkeypatch, clean_creds):
    # The override is not an escape hatch: preflight validates it like the configured branch, so
    # --branch main is rejected (forbid_branches) and the outward edge is never reached.
    toml = _write_config(git_repo)
    _stub_issue(monkeypatch)
    called = {"sync": False}
    monkeypatch.setattr("loopkit.extensions.remote.sync_done",
                        lambda *a, **k: called.__setitem__("sync", True) or {})
    result = runner.invoke(app, ["run", "-c", str(toml), "--repo", str(git_repo),
                                 "--from-issue", "42", "--branch", "main",
                                 "--adapter", "mock", "--open-pr"])
    assert result.exit_code == 1
    assert "forbidden" in _norm(result.output)
    assert called["sync"] is False                       # never pushed/opened a PR from main


# --------------------------------------------------------------------------------------------
# init --ci — scaffold a CI workflow; the shipped examples stay byte-identical to the constants.
# --------------------------------------------------------------------------------------------
def test_init_ci_github_scaffolds_the_workflow(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--ci", "github"])
    assert result.exit_code == 0, result.output
    wf = tmp_path / ".github" / "workflows" / "loopkit.yml"
    assert wf.exists() and wf.read_text() == _CI_GITHUB_TEMPLATE
    assert (tmp_path / "loopkit.toml").exists()       # still scaffolds the base files


def test_init_ci_gitlab_scaffolds_the_workflow(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--ci", "gitlab"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".gitlab-ci.yml").read_text() == _CI_GITLAB_TEMPLATE


def test_init_ci_rejects_unknown_forge(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--ci", "bitbucket"])
    assert result.exit_code == 1
    assert "unknown --ci value" in _norm(result.output)


def test_init_without_ci_writes_no_workflow(tmp_path):
    runner.invoke(app, ["init", str(tmp_path)])
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / ".gitlab-ci.yml").exists()


def test_shipped_ci_examples_match_the_scaffold_constants():
    # Guard against drift: examples/ci/ is what `loopkit init --ci` writes, for repo browsers.
    assert (_REPO_ROOT / "examples/ci/github-actions.yml").read_text() == _CI_GITHUB_TEMPLATE
    assert (_REPO_ROOT / "examples/ci/gitlab-ci.yml").read_text() == _CI_GITLAB_TEMPLATE
    # The Claude Code subscription variant (claude-code + CLAUDE_CODE_OAUTH_TOKEN, no API key).
    assert (_REPO_ROOT / "examples/ci/github-actions-claude-code.yml").read_text() \
        == _CI_GITHUB_CLAUDE_CODE_TEMPLATE
    assert (_REPO_ROOT / "examples/ci/gitlab-ci-claude-code.yml").read_text() \
        == _CI_GITLAB_CLAUDE_CODE_TEMPLATE
