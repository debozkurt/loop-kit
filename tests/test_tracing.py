"""LangSmith tracing — the full-tree observability layer (Ch 14-15).

No real LangSmith and no network: the disabled path is a true no-op, and the enabled path is proved
by injecting a fake `trace` provider that records every span. We assert the *shape* of the tree a
run produces (run → tick → agent → llm/tool → gates) and that cost/usage metadata lands on it.
The fan-out cases (fleet/evolve) use a *nesting* fake that mimics langsmith's contextvar
parenting, so a broken tree — every span its own root — fails the test, not just a missing span.
"""
from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from loopkit import trace
from loopkit.agent import ClaudeAPIAdapter, MockAgent, _APIAdapter, _ToolCall, _Turn
from loopkit.config import Config, GateConfig
from loopkit.extensions.orchestrate import Supervisor, run_fleet
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


def test_auto_on_normalizes_sdk_env(monkeypatch):
    """Auto-on (API key, no flag) must set LANGSMITH_TRACING=true for the SDK.

    langsmith's `trace` context manager separately gates on that exact env value; without it the
    SDK silently neither posts nor parents spans, so loopkit's auto-on would upload nothing.
    """
    for var in ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING",
                "LANGCHAIN_TRACING", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_fake_key")
    trace._reset()
    assert trace._enabled()
    trace._resolve()
    import os
    assert os.environ["LANGSMITH_TRACING"] == "true"


def test_explicit_false_flag_beats_key_auto_on(monkeypatch):
    """LANGSMITH_TRACING=false turns tracing off even when an API key is present."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_fake_key")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    for var in ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(var, raising=False)
    trace._reset()
    assert not trace._enabled()


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


# --------------------------------------------------------------------------------------------
# Thread-safety: concurrent loops must never lose spans during first resolution
# --------------------------------------------------------------------------------------------
def test_provider_resolution_is_thread_safe(monkeypatch):
    """N loops opening their top-level span at once (fleet/evolve) all get REAL spans.

    Regression guard: `_provider()` used to mark itself resolved *before* the slow langsmith
    import finished, so every concurrent caller got a `None` provider and its span silently
    no-oped — orphaning that loop's children into separate root traces in LangSmith.
    """
    provider = _FakeProvider()
    resolve_calls: list[int] = []

    def slow_resolve():
        resolve_calls.append(1)
        time.sleep(0.05)            # stands in for the slow first `import langsmith`
        return provider

    trace._reset()
    monkeypatch.setattr(trace, "_resolve", slow_resolve)

    got_noop: list[bool] = []
    barrier = threading.Barrier(8)

    def open_span():
        barrier.wait()              # all threads hit _provider() together
        with trace.span("loopkit run") as handle:
            got_noop.append(handle is trace._NOOP)

    threads = [threading.Thread(target=open_span) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert resolve_calls == [1]     # resolved exactly once, not once per thread
    assert not any(got_noop)        # and nobody was silently no-oped mid-resolution
    assert len(provider.runs) == 8


# --------------------------------------------------------------------------------------------
# Fleet/evolve grouping: one umbrella trace, workers nested under it
# --------------------------------------------------------------------------------------------
_FAKE_PARENT: contextvars.ContextVar = contextvars.ContextVar("fake_parent", default=None)


class _NestingFakeProvider(_FakeProvider):
    """Mimics langsmith's contextvar parenting: each span records the span open at its enter.

    This is what lets these tests assert the *tree* — a worker span opened in a pool thread only
    sees the umbrella as parent if the supervisor propagated its context into that thread.
    """

    @contextmanager
    def _cm(self, run):
        run.parent = _FAKE_PARENT.get()
        token = _FAKE_PARENT.set(run)
        try:
            yield run
        finally:
            _FAKE_PARENT.reset(token)


def _install_nesting(monkeypatch) -> _NestingFakeProvider:
    provider = _NestingFakeProvider()
    monkeypatch.setattr(trace, "_provider", lambda: provider)
    return provider


def _writes_file(task: dict) -> MockAgent:
    def behavior(workspace: Path) -> str:
        (workspace / task["file"]).write_text("ok")
        return f"wrote {task['file']}"
    return MockAgent(behaviors=[behavior])


def _file_gates(task: dict, worktree: Path):
    gate = CallableGate(lambda ws: (ws / task["file"]).exists())
    return gate, gate


def test_fleet_is_one_trace(monkeypatch, git_repo: Path):
    provider = _install_nesting(monkeypatch)
    cfg = Config(goal="base", repo=str(git_repo), branch="loopkit/fleet",
                 gate=GateConfig(iteration="true"))
    tasks = [{"goal": f"feature {s}", "slug": s, "file": f"f_{s}.py"} for s in ("a", "b")]

    run_fleet(cfg, tasks, make_agent=_writes_file, make_gates=_file_gates, max_workers=2)

    umbrellas = [r for r in provider.runs if r.name == "loopkit fleet"]
    assert len(umbrellas) == 1
    assert umbrellas[0].outputs["done"] == 2

    # Every worker's run span nests under the ONE fleet span — not its own root trace.
    run_spans = [r for r in provider.runs if r.name == "loopkit run"]
    assert len(run_spans) == 2
    assert all(r.parent is umbrellas[0] for r in run_spans)
    # And each is attributable: the task's slug/branch stamped as metadata.
    assert {r.metadata["slug"] for r in run_spans} == {"a", "b"}

    # Ticks nest under their own run span (context propagation reached the pool threads).
    tick_spans = [r for r in provider.runs if r.name.startswith("tick ")]
    assert tick_spans and all(t.parent in run_spans for t in tick_spans)


def test_evolve_is_one_trace_with_score_and_revalidate(monkeypatch, git_repo: Path):
    provider = _install_nesting(monkeypatch)
    cfg = Config(goal="base", repo=str(git_repo), branch="loopkit/evolve",
                 gate=GateConfig(iteration="true"))
    sup = Supervisor(cfg, make_agent=_writes_file, make_gates=_file_gates, max_workers=2)

    result = sup.evolve({"id": "t", "goal": "solve it", "file": "sol.py"},
                        generations=1, population=2, keep=1,
                        score=lambda task, wt: 1.0,
                        revalidate=lambda task, wt: CallableGate(lambda ws: True))
    assert result.winner is not None

    umbrellas = [r for r in provider.runs if r.name == "loopkit evolve"]
    assert len(umbrellas) == 1
    assert umbrellas[0].outputs["winner"] == result.winner.branch

    # Both candidates' run spans nest under the ONE evolve span, stamped with their candidate ids.
    run_spans = [r for r in provider.runs if r.name == "loopkit run"]
    assert len(run_spans) == 2
    assert all(r.parent is umbrellas[0] for r in run_spans)
    assert {r.metadata["slug"] for r in run_spans} == {"g0-c0", "g0-c1"}

    # Selection is visible in the same tree: score per candidate + the held-out revalidation.
    score_spans = [r for r in provider.runs if r.name == "score"]
    assert len(score_spans) == 2 and all(s.parent is umbrellas[0] for s in score_spans)
    reval_spans = [r for r in provider.runs if r.name == "revalidate"]
    assert len(reval_spans) == 1 and reval_spans[0].parent is umbrellas[0]
    assert reval_spans[0].outputs["passed"] is True
