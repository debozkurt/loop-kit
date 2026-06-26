"""The 2×2 adapter matrix + real cost accounting (Ch 1-3, 14).

All token-free: pricing is pure arithmetic, CLI cost parsing runs on canned vendor output, and the
API adapters' tool-calling loop is driven by an injected fake backend that returns scripted turns
with scripted usage. The point of these tests is that *the budget stop can bite on real cost* —
exercised here without a single token or network call.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from loopkit.agent import (
    ClaudeAPIAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    OpenAIAPIAdapter,
    _APIAdapter,
    _parse_claude_json,
    _parse_codex_usage,
    _ToolCall,
    _Turn,
    _WorkspaceTools,
    build_agent,
)
from loopkit.config import AgentConfig, Config, GateConfig, StopsConfig
from loopkit.gate import CallableGate
from loopkit.loop import run_loop
from loopkit.pricing import Usage, estimate_cost, known_model
from loopkit.stops import StopReason


# --------------------------------------------------------------------------------------------
# pricing.py — usage → dollars
# --------------------------------------------------------------------------------------------
def test_estimate_cost_input_output():
    # 1M input + 1M output on Opus 4.8 = $5 + $25.
    cost = estimate_cost("claude-opus-4-8", Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert cost == 30.0


def test_estimate_cost_cache_tiers():
    # Cache read is 0.1x input ($0.50), cache write is 1.25x input ($6.25) per 1M on Opus.
    read = estimate_cost("claude-opus-4-8", Usage(cache_read_tokens=1_000_000))
    write = estimate_cost("claude-opus-4-8", Usage(cache_write_tokens=1_000_000))
    assert abs(read - 0.5) < 1e-9
    assert abs(write - 6.25) < 1e-9


def test_unknown_model_is_free_and_flagged():
    # Unknown model -> 0.0 cost (budget can't bite; doctor warns). known_model reflects that.
    assert estimate_cost("totally-made-up", Usage(input_tokens=1_000_000)) == 0.0
    assert not known_model("totally-made-up")
    assert known_model("claude-opus-4-8")
    assert known_model("gpt-4o")


def test_usage_adds():
    total = Usage(input_tokens=10, output_tokens=2) + Usage(input_tokens=5, cache_read_tokens=3)
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens) == (15, 2, 3)


# --------------------------------------------------------------------------------------------
# CLI adapters — cost parsed from vendor output
# --------------------------------------------------------------------------------------------
def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr="")


def test_claude_json_parse():
    cost, text = _parse_claude_json('{"total_cost_usd": 0.0123, "result": "all set"}')
    assert cost == 0.0123
    assert text == "all set"


def test_claude_stream_json_takes_last_line():
    stream = '{"type":"system"}\n{"type":"result","total_cost_usd":0.5,"result":"ok"}'
    cost, text = _parse_claude_json(stream)
    assert cost == 0.5 and text == "ok"


def test_claude_json_empty_is_zero():
    assert _parse_claude_json("") == (0.0, "")
    assert _parse_claude_json("not json") == (0.0, "")


def test_claude_json_array_carries_cost():
    # Current `claude -p --output-format json` returns a top-level ARRAY of events; the final
    # subtype:"success" element carries total_cost_usd. Parsing the array is what keeps the budget
    # ceiling alive on current builds (without it the cost read 0.0 and the stop never fired).
    array = ('[{"type":"system","subtype":"init"},'
             '{"type":"assistant"},'
             '{"type":"result","subtype":"success","total_cost_usd":0.0839,"result":"done"}]')
    cost, text = _parse_claude_json(array)
    assert cost == 0.0839 and text == "done"


def test_claude_code_adapter_requests_json_and_parses_cost():
    adapter = ClaudeCodeAdapter()
    assert "--output-format" in adapter._command("do it")  # json output is added automatically
    result = adapter._result(_proc('{"total_cost_usd": 0.25, "result": "done"}'))
    assert result.ok and result.cost_usd == 0.25 and result.raw_tail == "done"


def test_claude_code_defaults_to_subscription_withholding_the_api_key():
    # Default: the agent's env carries only the subscription token — an ambient ANTHROPIC_API_KEY is
    # NOT handed to `claude`, so it can't silently bill the API instead of the subscription.
    from loopkit import secrets
    adapter = ClaudeCodeAdapter()
    assert adapter.cred_keys == secrets.CLAUDE_CODE_SUBSCRIPTION_KEYS
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-y"}
    env = secrets.CredentialStore().child_env(base=base, add=adapter.cred_keys)
    assert "ANTHROPIC_API_KEY" not in env                  # withheld → claude uses the subscription
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-y"     # subscription token passed through


def test_claude_code_api_key_opt_in_injects_the_billed_key():
    from loopkit import secrets
    adapter = ClaudeCodeAdapter(use_api_key=True)
    assert "ANTHROPIC_API_KEY" in adapter.cred_keys
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-x"}
    env = secrets.CredentialStore().child_env(base=base, add=adapter.cred_keys)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"          # opt-in → the billed API key is used


def test_build_agent_threads_use_api_key():
    sub = build_agent(AgentConfig(adapter="claude-code"))                       # default
    api = build_agent(AgentConfig(adapter="claude-code", use_api_key=True))     # opt-in
    assert "ANTHROPIC_API_KEY" not in sub.cred_keys
    assert "ANTHROPIC_API_KEY" in api.cred_keys


def test_doctor_claude_code_auth_note_surfaces_billing(monkeypatch):
    # doctor must make the BILLING path visible before a run (this is the gap that caused a surprise
    # API charge): subscription by default, the billed API key only on explicit opt-in.
    from loopkit.cli import _claude_code_auth_note
    sub = Config(goal="g", gate=GateConfig(iteration="true"),
                 agent=AgentConfig(adapter="claude-code"))
    api = Config(goal="g", gate=GateConfig(iteration="true"),
                 agent=AgentConfig(adapter="claude-code", use_api_key=True))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert "subscription" in _claude_code_auth_note(sub) and "withheld" in _claude_code_auth_note(sub)
    assert "billed API" in _claude_code_auth_note(api)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "withheld" not in _claude_code_auth_note(sub)        # nothing to withhold


def test_codex_usage_parse_and_cost():
    usage = _parse_codex_usage('{"type":"start"}\n{"usage":{"input_tokens":100,"output_tokens":50}}')
    assert usage.input_tokens == 100 and usage.output_tokens == 50
    # With a priced model the budget stop has a real number to work with.
    result = CodexAdapter(model="gpt-4o")._result(
        _proc('{"usage":{"input_tokens":1000000,"output_tokens":0}}'))
    assert result.cost_usd == estimate_cost("gpt-4o", Usage(input_tokens=1_000_000))


# --------------------------------------------------------------------------------------------
# API adapters — the in-process tool-calling loop, driven by a fake backend
# --------------------------------------------------------------------------------------------
class FakeBackend:
    """A scripted `_Backend`: returns the queued `_Turn`s in order, ignoring the transcript."""

    def __init__(self, model: str, turns: list[_Turn]) -> None:
        self.model = model
        self._turns = list(turns)
        self.calls = 0

    def complete(self, transcript, tools) -> _Turn:
        self.calls += 1
        return self._turns.pop(0)


def test_api_adapter_runs_tools_and_costs_usage(tmp_path: Path):
    backend = FakeBackend("claude-opus-4-8", [
        _Turn(text="", tool_calls=[_ToolCall("c1", "write_file",
                                             {"path": "out.txt", "content": "hello"})],
              usage=Usage(input_tokens=1000, output_tokens=200)),
        _Turn(text="done", tool_calls=[], usage=Usage(input_tokens=1200, output_tokens=50)),
    ])
    result = _APIAdapter(backend).act("make out.txt", tmp_path)

    assert (tmp_path / "out.txt").read_text() == "hello"            # the tool actually ran
    expected = estimate_cost("claude-opus-4-8", Usage(input_tokens=2200, output_tokens=250))
    assert abs(result.cost_usd - expected) < 1e-12                  # cost summed across both calls
    assert "toolCalls=1" in result.summary
    assert result.raw_tail == "done"
    assert backend.calls == 2


def test_api_adapter_backend_injection_via_public_class(tmp_path: Path):
    backend = FakeBackend("claude-opus-4-8",
                          [_Turn(text="hi", tool_calls=[], usage=Usage(output_tokens=10))])
    adapter = ClaudeAPIAdapter(backend=backend)
    assert adapter.model == "claude-opus-4-8"
    result = adapter.act("noop", tmp_path)
    assert result.cost_usd == estimate_cost("claude-opus-4-8", Usage(output_tokens=10))


def _config(repo: Path, **overrides) -> Config:
    base = dict(goal="make it pass", repo=str(repo), branch="loopkit/test",
                gate=GateConfig(iteration="true"))
    base.update(overrides)
    return Config(**base)


def test_api_adapter_reaches_done(git_repo: Path):
    backend = FakeBackend("claude-opus-4-8", [
        _Turn(text="", tool_calls=[_ToolCall("c1", "write_file",
                                             {"path": "solution.txt", "content": "ok"})],
              usage=Usage(input_tokens=500, output_tokens=20)),
        _Turn(text="done", tool_calls=[], usage=Usage(output_tokens=10)),
    ])
    gate = CallableGate(lambda ws: (ws / "solution.txt").exists())
    result = run_loop(_config(git_repo), ClaudeAPIAdapter(backend=backend),
                      iteration_gate=gate, acceptance_gate=gate)
    assert result.reason is StopReason.DONE
    assert result.iterations == 1
    assert result.cost_usd > 0          # a real, non-zero cost from native usage


class CyclingBackend:
    """Writes a fresh file every tick (so the tree changes — no false no-progress) but never solves;
    each write burns `input_tokens`, so cost accrues until the budget ceiling fires."""

    def __init__(self, model: str, input_tokens: int) -> None:
        self.model = model
        self._input = input_tokens
        self._n = 0

    def complete(self, transcript, tools) -> _Turn:
        i = self._n
        self._n += 1
        if i % 2 == 0:   # write turn
            return _Turn(text="", tool_calls=[_ToolCall(f"c{i}", "write_file",
                                                       {"path": f"f{i}.txt", "content": str(i)})],
                         usage=Usage(input_tokens=self._input))
        return _Turn(text="working", tool_calls=[], usage=Usage(output_tokens=10))


def test_api_adapter_budget_ceiling_bites(git_repo: Path):
    # 100k input/write on Opus = $0.50/tick; a $1.00 ceiling is crossed on tick 2 (the budget
    # stop is now real because the cost comes from native usage, not a hand-set number).
    backend = CyclingBackend("claude-opus-4-8", input_tokens=100_000)
    cfg = _config(git_repo, agent=AgentConfig(adapter="claude-api", max_cost_usd=1.0),
                  stops=StopsConfig(max_iter=20, no_progress_after=99))
    result = run_loop(cfg, ClaudeAPIAdapter(backend=backend, max_tool_calls=4),
                      iteration_gate=CallableGate(lambda ws: False))
    assert result.reason is StopReason.BUDGET_CEILING
    assert result.iterations == 2


# --------------------------------------------------------------------------------------------
# Workspace tools — sandboxed to the run's root
# --------------------------------------------------------------------------------------------
def test_workspace_tools_read_write_bash(tmp_path: Path):
    tools = _WorkspaceTools(tmp_path)
    out, err = tools.dispatch("write_file", {"path": "sub/a.txt", "content": "x"})
    assert not err and (tmp_path / "sub" / "a.txt").read_text() == "x"
    content, err = tools.dispatch("read_file", {"path": "sub/a.txt"})
    assert not err and content == "x"
    missing, err = tools.dispatch("read_file", {"path": "nope.txt"})
    assert err and "no such file" in missing
    shell, err = tools.dispatch("run_bash", {"command": "echo hi"})
    assert not err and "hi" in shell and "exit=0" in shell


def test_workspace_tools_block_traversal(tmp_path: Path):
    tools = _WorkspaceTools(tmp_path / "root")
    (tmp_path / "root").mkdir()
    _, err = tools.dispatch("read_file", {"path": "../secret"})
    assert err
    _, err = tools.dispatch("write_file", {"path": "/etc/evil", "content": "x"})
    assert err
    unknown, err = tools.dispatch("frobnicate", {})
    assert err and "unknown tool" in unknown


# --------------------------------------------------------------------------------------------
# build_agent resolves every adapter name (construction is SDK-free; the import is deferred)
# --------------------------------------------------------------------------------------------
def test_build_agent_resolves_matrix():
    assert isinstance(build_agent(AgentConfig(adapter="claude-code")), ClaudeCodeAdapter)
    assert isinstance(build_agent(AgentConfig(adapter="codex")), CodexAdapter)
    claude = build_agent(AgentConfig(adapter="claude-api"))
    openai = build_agent(AgentConfig(adapter="openai-api"))
    assert isinstance(claude, ClaudeAPIAdapter) and claude.model == "claude-opus-4-8"
    assert isinstance(openai, OpenAIAPIAdapter) and openai.model == "gpt-4o"


def test_build_agent_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        build_agent(AgentConfig(adapter="nope"))
