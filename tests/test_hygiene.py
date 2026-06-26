"""Phase 5a — worker-side credential hygiene at the spawn sites (vectors 1, 2, 5, 6, 7).

Token-free. Proves the properties that make the per-submitter claim real: the agent's `run_bash` and
the held-out gate run credential-free; tool output is redacted before it re-enters the transcript/wire;
the pre-push secret scan refuses a leaking diff; a token-in-URL is stripped; and the worker-start load
scrubs a planted env var so a later spawn can't see it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from loopkit import secrets
from loopkit.agent import ClaudeCodeAdapter, _APIAdapter, _ToolCall, _Turn, _WorkspaceTools
from loopkit.extensions import remote
from loopkit.gate import ShellGate


@pytest.fixture(autouse=True)
def _clean_state():
    secrets.clear_registry()
    secrets.install(secrets.CredentialStore())
    yield
    secrets.clear_registry()
    secrets.install(secrets.CredentialStore())


# --------------------------------------------------------------------------------------------
# run_bash + gate run with NO credentials (vectors 1, 2, 6).
# --------------------------------------------------------------------------------------------
def test_run_bash_cannot_read_a_credential_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shouldbescrubbed")
    out, is_error = _WorkspaceTools(tmp_path).dispatch(
        "run_bash", {"command": "printenv ANTHROPIC_API_KEY || echo MISSING"})
    assert not is_error
    assert "sk-ant-shouldbescrubbed" not in out
    assert "MISSING" in out                                    # the agent's shell sees no key


def test_held_out_gate_runs_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-gatewouldleak")
    # A gate that tries to exfiltrate the key on collection; it exits non-zero so the output is fed back.
    res = ShellGate("printenv ANTHROPIC_API_KEY; exit 1").check(tmp_path)
    assert res.passed is False
    assert "sk-ant-gatewouldleak" not in (res.feedback or "")  # the trust anchor is no longer an exfil sink


def test_cli_adapter_gets_only_its_model_key_not_git_or_other_provider(tmp_path, monkeypatch):
    secrets.install(secrets.CredentialStore({"ANTHROPIC_API_KEY": "sk-ant-x",
                                             "CLAUDE_CODE_OAUTH_TOKEN": "oauth-z",
                                             "GITHUB_TOKEN": "ghp_tok", "OPENAI_API_KEY": "sk-oa-y"}))
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr("loopkit.agent.subprocess.run", fake_run)

    # Default: SUBSCRIPTION — the OAuth token is re-injected; an ambient ANTHROPIC_API_KEY is WITHHELD
    # (no surprise API billing); and never the git token or another provider's key.
    ClaudeCodeAdapter().act("do it", tmp_path)
    env = captured["env"]
    assert env is not None
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oauth-z"     # subscription token re-injected
    assert "ANTHROPIC_API_KEY" not in env                      # billed key withheld by default
    assert "GITHUB_TOKEN" not in env                           # no git token
    assert "OPENAI_API_KEY" not in env                         # no other provider's key

    # Opt-in (run --api-key / [agent] use_api_key): the billed API key IS injected.
    ClaudeCodeAdapter(use_api_key=True).act("do it", tmp_path)
    env = captured["env"]
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-x"          # billed key, on explicit opt-in
    assert "GITHUB_TOKEN" not in env and "OPENAI_API_KEY" not in env


# --------------------------------------------------------------------------------------------
# Redact at capture — tool output is scrubbed before it re-enters the transcript/wire (vector 7).
# --------------------------------------------------------------------------------------------
class _RecordingBackend:
    def __init__(self, model, turns):
        self.model = model
        self._turns = list(turns)
        self.transcripts: list = []

    def complete(self, transcript, tools):
        self.transcripts.append(json.loads(json.dumps(transcript, default=lambda o: o.__dict__)))
        return self._turns.pop(0)


def test_tool_output_is_redacted_before_it_reenters_the_transcript(tmp_path):
    secret = "sk-ant-modelknewthissecret0123"
    secrets.register_secret(secret, label="ANTHROPIC_API_KEY")
    backend = _RecordingBackend("claude-opus-4-8", [
        _Turn(text="", tool_calls=[_ToolCall("c1", "run_bash", {"command": f"echo {secret}"})]),
        _Turn(text="ok", tool_calls=[]),
    ])
    _APIAdapter(backend).act("echo the secret", tmp_path)
    # Redaction is at the tool-OUTPUT capture point (where a key the agent extracts surfaces); the
    # model's own command text is left intact (redacting it would desync the conversation). So assert
    # on the tool result content that re-enters the transcript/wire on the next call.
    results = [r for entry in backend.transcripts[1] if entry.get("role") == "tool"
               for r in entry["results"]]
    assert results, "expected a tool result in the second-call transcript"
    assert all(secret not in r["content"] for r in results)
    assert any("‹redacted:ANTHROPIC_API_KEY›" in r["content"] for r in results)


# --------------------------------------------------------------------------------------------
# Pre-push secret scan + URL sanitize (vectors 5, 7).
# --------------------------------------------------------------------------------------------
def _commit_on_branch(repo: Path, branch: str, name: str, content: str) -> None:
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True, capture_output=True)
    (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "work"], cwd=repo, check=True, capture_output=True)


def test_push_is_refused_when_the_diff_contains_a_secret(git_repo):
    _commit_on_branch(git_repo, "loopkit/x", "config.py", "TOKEN = 'ghp_0123456789abcdefABCDEF01'\n")
    assert remote._scan_push(git_repo, "loopkit/x", "main")     # the scan flags it
    pushed = remote.push_branch(git_repo, remote="origin", branch="loopkit/x", base="main")
    assert pushed is False                                      # refused before ever touching the network


def test_clean_diff_passes_the_scan(git_repo):
    _commit_on_branch(git_repo, "loopkit/y", "feature.py", "def add(a, b):\n    return a + b\n")
    assert remote._scan_push(git_repo, "loopkit/y", "main") == []


def test_sanitize_git_url_strips_userinfo_token():
    assert remote.sanitize_git_url("https://x:ghp_tok@github.com/o/r.git") == "https://github.com/o/r.git"
    assert remote.sanitize_git_url("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert remote.sanitize_git_url("git@github.com:o/r.git") == "git@github.com:o/r.git"   # ssh untouched


# --------------------------------------------------------------------------------------------
# Worker-start ordering — load scrubs a planted env var so a later spawn can't see it (G5).
# --------------------------------------------------------------------------------------------
def test_load_at_worker_start_closes_a_planted_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_plantedbeforeworkerstart")
    secrets.install(secrets.CredentialStore.load(None))        # what the entry point does, first
    import os
    assert "GITHUB_TOKEN" not in os.environ                    # scrubbed from the process env
    out, _ = _WorkspaceTools(tmp_path).dispatch(
        "run_bash", {"command": "printenv GITHUB_TOKEN || echo GONE"})
    assert "ghp_plantedbeforeworkerstart" not in out and "GONE" in out
