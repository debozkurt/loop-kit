"""Tests for the outward edges: remote sync (push/PR) and issue-sourced tasks.

No network, no tokens, no real `gh`/`glab`: the forge CLIs are shelled out to, so here we test the
pure logic around them — issue→task mapping, provider detection from a remote URL, the push safety
guard, and the generalised repo runner driving run_loop on an arbitrary repo with a MockAgent.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, RemoteConfig
from loopkit.extensions.fleet import make_repo_runner
from loopkit.extensions.issues import issue_to_task, issues_to_tasks
from loopkit.extensions.remote import detect_provider, open_pull_request, push_branch, sync_done


def _set_remote(repo: Path, url: str) -> None:
    subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True, capture_output=True)


# --------------------------------------------------------------------------------------------
# Issues -> tasks
# --------------------------------------------------------------------------------------------
def test_issue_to_task_maps_number_title_body():
    task = issue_to_task({"number": 42, "title": "Fix off-by-one in pager",
                          "body": "page 2 shows page 1 rows"})
    assert task["id"] == "issue-42"
    assert task["branch"] == "loopkit/issue-42"
    assert task["issue"] == 42
    assert task["goal"].startswith("Fix off-by-one in pager")
    assert "page 2 shows page 1 rows" in task["goal"]


def test_issues_to_tasks_skips_empty_titles():
    issues = [{"number": 1, "title": "real bug", "body": "x"},
              {"number": 2, "title": "   ", "body": "no title"}]
    tasks = issues_to_tasks(issues)
    assert [t["id"] for t in tasks] == ["issue-1"]


def test_issue_task_branch_prefix_is_configurable():
    task = issue_to_task({"number": 7, "title": "t", "body": ""}, base_branch="bot")
    assert task["branch"] == "bot/issue-7"


# --------------------------------------------------------------------------------------------
# Provider detection + push safety (no network)
# --------------------------------------------------------------------------------------------
def test_detect_provider_from_remote_url(git_repo: Path):
    _set_remote(git_repo, "https://github.com/acme/widgets.git")
    assert detect_provider(git_repo) == "github"


def test_detect_provider_gitlab(git_repo: Path):
    _set_remote(git_repo, "git@gitlab.com:acme/widgets.git")
    assert detect_provider(git_repo) == "gitlab"


def test_detect_provider_unknown_when_no_remote(git_repo: Path):
    assert detect_provider(git_repo) == "unknown"


def test_push_refuses_forbidden_branch(git_repo: Path):
    # The Ch 16 guard at the outward edge: never push main, even on request — and without ever
    # invoking git push (so no network/remote is needed for the test).
    assert push_branch(git_repo, remote="origin", branch="main", forbid=["main", "master"]) is False


def test_open_pr_unknown_provider_returns_none(git_repo: Path):
    assert open_pull_request(git_repo, provider="unknown", branch="loopkit/run-x", base="main",
                             title="t") is None


def test_sync_done_is_a_noop_when_remote_disabled(git_repo: Path):
    cfg = Config(goal="g", repo=str(git_repo), gate=GateConfig(iteration="true"),
                 remote=RemoteConfig(enabled=False))
    result = sync_done(cfg, git_repo)
    assert result == {"pushed": False, "pr_url": None}


# --------------------------------------------------------------------------------------------
# The generalised repo runner — any repo, via run_loop (MockAgent, no tokens)
# --------------------------------------------------------------------------------------------
def test_make_repo_runner_solves_an_arbitrary_repo(git_repo: Path):
    # Clone the seeded repo, run a loop whose gate is satisfied once the agent writes a file.
    def agent_factory(task):
        def behavior(workspace: Path) -> str:
            (workspace / "solution.txt").write_text("done")
            return "wrote solution.txt"
        return MockAgent(behaviors=[behavior])

    runner = make_repo_runner(
        str(git_repo), mode="clone", adapter="mock", max_iter=4, protected_paths=(),
        gate_iteration="test -f solution.txt", gate_acceptance="test -f solution.txt",
        agent_factory=agent_factory)

    outcome = runner({"id": "x", "branch": "loopkit/run-x", "goal": "create solution.txt"})
    assert outcome.done is True
    assert outcome.branch == "loopkit/run-x"
    assert outcome.score == 1.0 and outcome.revalidated is True


def test_make_repo_runner_reports_failure_without_raising(git_repo: Path):
    # Agent never satisfies the gate -> the runner returns a non-done outcome, doesn't raise.
    runner = make_repo_runner(
        str(git_repo), mode="clone", adapter="mock", max_iter=3, protected_paths=(),
        gate_iteration="test -f never.txt", gate_acceptance="test -f never.txt",
        agent_factory=lambda task: MockAgent(behaviors=[]))
    outcome = runner({"id": "y", "branch": "loopkit/run-y", "goal": "won't happen"})
    assert outcome.done is False and outcome.error is None
