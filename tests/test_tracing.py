"""LangSmith tracing — the full-tree observability layer (Ch 14-15).

No real LangSmith and no network: the disabled path is a true no-op, and the enabled path is proved
by injecting a fake `trace` provider that records every span. We assert the *shape* of the tree a
run produces (run → tick → agent → llm/tool → gates) and that cost/usage metadata lands on it.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from loopkit import trace
from loopkit.agent import ClaudeAPIAdapter, _APIAdapter, _ToolCall, _Turn
from loopkit.config import Config, GateConfig
from loopkit.gate import CallableGate
from loopkit.loop import run_loop
from loopkit.pricing import Usage


@pytest.fixture(autouse=True)
def _reset_trace():
    yield
    trace.set_enabled(None)   # restore auto-detect so tests don't leak state
    trace._reset()


# --------------------------------------------------------------------------------------------
# Disabled path: a true no-op
# --------------------------------------------------------------------------------------------
def test_span_is_noop_when_disabled():
    trace.set_enabled(False)
    assert not trace.active()
    with trace.span("x", inputs={"a": 1}) as span:   # must not raise, must not record anything
        span.outputs(result="ok")
        span.metadata(cost_usd=1.23)


def test_clean_drops_none_and_caps_long_strings():
    cleaned = trace._clean({"keep": "v", "drop": None, "big": "z" * 60_000})
    assert "drop" not in cleaned
    assert cleaned["keep"] == "v"
    assert cleaned["big"].endswith("chars]") and len(cleaned["big"]) < 60_000


# --------------------------------------------------------------------------------------------
# Enabled path: a fake provider records the span tree
# --------------------------------------------------------------------------------------------
class _FakeRun:
    def __init__(self, name, run_type, inputs, metadata):
        self.name = name
        self.run_type = run_type
        self.inputs = inputs
        self.metadata = dict(metadata or {})
        self.outputs = {}

    def add_outputs(self, data):
        self.outputs.update(data)

    def add_metadata(self, data):
        self.metadata.update(data)


class _FakeProvider:
    """Stands in for langsmith's `trace` context-manager factory; records every span opened."""

    def __init__(self):
        self.runs: list[_FakeRun] = []

    def __call__(self, *, name, run_type, inputs, metadata, tags, project_name):
        run = _FakeRun(name, run_type, inputs, metadata)
        self.runs.append(run)
        return self._cm(run)

    @contextmanager
    def _cm(self, run):
        yield run


def _install(monkeypatch) -> _FakeProvider:
    provider = _FakeProvider()
    monkeypatch.setattr(trace, "_provider", lambda: provider)
    return provider


def _config(repo: Path) -> Config:
    return Config(goal="solve it", repo=str(repo), branch="loopkit/test",
                  gate=GateConfig(iteration="true"))


def test_run_loop_emits_full_tree(monkeypatch, git_repo: Path):
    provider = _install(monkeypatch)
    backend_turns = [
        _Turn(text="", tool_calls=[_ToolCall("c1", "write_file",
                                            {"path": "solution.txt", "content": "ok"})],
              usage=Usage(input_tokens=500, output_tokens=20)),
        _Turn(text="done", tool_calls=[], usage=Usage(output_tokens=10)),
    ]

    class _B:
        model = "claude-opus-4-8"

        def __init__(self):
            self._t = list(backend_turns)

        def complete(self, transcript, tools):
            return self._t.pop(0)

    gate = CallableGate(lambda ws: (ws / "solution.txt").exists())
    run_loop(_config(git_repo), ClaudeAPIAdapter(backend=_B()),
             iteration_gate=gate, acceptance_gate=gate)

    names = [r.name for r in provider.runs]
    assert "loopkit run" in names
    assert "tick 1" in names
    assert "agent" in names
    assert "iteration gate" in names and "acceptance gate" in names
    # The API adapter's own llm/tool spans nested in (whole-system tracing).
    assert any(n.startswith("llm:") for n in names)
    assert "tool:write_file" in names

    run_span = next(r for r in provider.runs if r.name == "loopkit run")
    assert run_span.outputs["terminal"] == "done"
    agent_span = next(r for r in provider.runs if r.name == "agent")
    assert "cost_usd" in agent_span.metadata


def test_llm_and_tool_spans_carry_cost_metadata(monkeypatch, tmp_path: Path):
    provider = _install(monkeypatch)
    backend_turns = [
        _Turn(text="", tool_calls=[_ToolCall("c1", "write_file",
                                            {"path": "a.txt", "content": "x"})],
              usage=Usage(input_tokens=1_000_000, output_tokens=0)),
        _Turn(text="done", tool_calls=[], usage=Usage(output_tokens=5)),
    ]

    class _B:
        model = "claude-opus-4-8"

        def __init__(self):
            self._t = list(backend_turns)

        def complete(self, transcript, tools):
            return self._t.pop(0)

    _APIAdapter(_B()).act("go", tmp_path)

    llm_spans = [r for r in provider.runs if r.name.startswith("llm:")]
    assert llm_spans and llm_spans[0].run_type == "llm"
    assert llm_spans[0].metadata["input_tokens"] == 1_000_000
    assert llm_spans[0].metadata["cost_usd"] == 5.0    # 1M input on Opus 4.8
    tool_span = next(r for r in provider.runs if r.name == "tool:write_file")
    assert tool_span.run_type == "tool"
    assert tool_span.outputs["is_error"] is False
