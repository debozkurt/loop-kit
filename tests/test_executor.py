"""Phase 6 — the ToolExecutor seam + the keyless-executor socket round-trip (token-free).

Proves the agent-isolation split works as a *mechanism*: the API adapter drives its tool calls and the
held-out gate over a Unix socket to a `loopkit executor` server (here, an in-process thread), so in the
cloud pod those run in a different, keyless container. We exercise the wire (write/read/bash/gate, path
confinement), the degrade-on-unreachable behavior, and that an injected `RemoteToolExecutor` is what the
adapter and `ShellGate` actually use. The live "ptrace from the executor fails" proof needs a DOKS pod
(separate PID namespace) and is asserted structurally in `test_cloudrun.py`.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from loopkit.agent import _APIAdapter, _ToolCall, _Turn
from loopkit.executor import LocalToolExecutor, RemoteToolExecutor, serve
from loopkit.gate import ShellGate


@pytest.fixture
def executor_socket():
    """Spin a `serve()` loop in a daemon thread on a Unix socket; yield its path; stop it on teardown.

    The socket lives in a short `/tmp` dir, not pytest's deep `tmp_path` — AF_UNIX paths are capped at
    ~104 chars (macOS), and the cloud pod uses a short `/var/run/...` path anyway.
    """
    sock_dir = tempfile.mkdtemp(prefix="lke", dir="/tmp")
    sock = str(Path(sock_dir) / "x.sock")
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(
        target=serve, args=(sock,), kwargs={"ready": ready.set, "stop": stop}, daemon=True)
    thread.start()
    assert ready.wait(timeout=5), "executor server did not bind in time"
    try:
        yield sock
    finally:
        stop.set()
        thread.join(timeout=5)
        shutil.rmtree(sock_dir, ignore_errors=True)


# --------------------------------------------------------------------------------------------
# Wire round-trip — the tool surface behaves identically across the socket as in-process.
# --------------------------------------------------------------------------------------------
def test_remote_write_then_read(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    remote = RemoteToolExecutor(executor_socket)
    out, is_error = remote.dispatch("write_file", {"path": "a/b.txt", "content": "hi"}, ws)
    assert not is_error and "wrote a/b.txt" in out
    assert (ws / "a" / "b.txt").read_text() == "hi"         # the write actually hit the shared workspace
    out, is_error = remote.dispatch("read_file", {"path": "a/b.txt"}, ws)
    assert not is_error and out == "hi"


def test_remote_run_bash(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    out, is_error = RemoteToolExecutor(executor_socket).dispatch(
        "run_bash", {"command": "echo hello-from-executor"}, ws)
    assert not is_error and "hello-from-executor" in out and "exit=0" in out


def test_remote_path_traversal_is_confined(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    out, is_error = RemoteToolExecutor(executor_socket).dispatch(
        "read_file", {"path": "../../../../etc/hosts"}, ws)
    assert is_error and "escapes the repository root" in out


def test_remote_unknown_tool_is_error(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    out, is_error = RemoteToolExecutor(executor_socket).dispatch("frobnicate", {}, ws)
    assert is_error and "unknown tool" in out


def test_remote_run_gate_pass_and_fail(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    remote = RemoteToolExecutor(executor_socket)
    passed = remote.run_gate("true", ws)
    assert passed.passed and passed.feedback is None
    failed = remote.run_gate("echo boom-diagnostics >&2; exit 1", ws)
    assert failed.passed is False and "boom-diagnostics" in (failed.feedback or "")


# --------------------------------------------------------------------------------------------
# The adapter + the gate actually USE the injected remote executor (the cloud wiring is real).
# --------------------------------------------------------------------------------------------
class _FakeBackend:
    """One tool turn (write a file), then a closing message — no tokens, no SDK."""

    model = "claude-opus-4-8"

    def __init__(self) -> None:
        self._turns = [
            _Turn(text="", tool_calls=[
                _ToolCall("c1", "write_file", {"path": "solution.txt", "content": "solved"})]),
            _Turn(text="done", tool_calls=[]),
        ]

    def complete(self, transcript, tools):
        return self._turns.pop(0)


def test_api_adapter_dispatches_tools_through_the_remote_executor(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    adapter = _APIAdapter(_FakeBackend(), executor=RemoteToolExecutor(executor_socket))
    result = adapter.act("solve it", ws)
    assert result.ok
    # The only way this file exists is if the tool call crossed the socket to the executor server.
    assert (ws / "solution.txt").read_text() == "solved"


def test_shellgate_runs_the_command_in_the_remote_executor(executor_socket, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    gate = ShellGate("test -f marker.txt", executor=RemoteToolExecutor(executor_socket))
    assert gate.check(ws).passed is False                   # marker absent → fail (ran remotely)
    (ws / "marker.txt").write_text("x")
    assert gate.check(ws).passed is True


# --------------------------------------------------------------------------------------------
# Degrade, don't crash — an unreachable executor surfaces as a tool/gate error.
# --------------------------------------------------------------------------------------------
def test_unreachable_executor_degrades_to_an_error(tmp_path):
    remote = RemoteToolExecutor(tmp_path / "nope.sock")     # nothing is listening
    out, is_error = remote.dispatch("run_bash", {"command": "echo hi"}, tmp_path)
    assert is_error and "executor unavailable" in out
    gate = remote.run_gate("true", tmp_path)
    assert gate.passed is False and "executor unavailable" in (gate.feedback or "")


def test_local_and_remote_executors_satisfy_the_protocol():
    # Both are ToolExecutors (duck-typed) — the seam the adapter/gate/run_loop accept.
    for ex in (LocalToolExecutor(), RemoteToolExecutor("/tmp/x.sock")):
        assert hasattr(ex, "dispatch") and hasattr(ex, "run_gate")


# --------------------------------------------------------------------------------------------
# Liveness bounds (Finding D) — a hung tool/gate fails the call instead of wedging the tick.
# --------------------------------------------------------------------------------------------
def test_run_bash_times_out_instead_of_wedging(tmp_path, monkeypatch):
    import loopkit.executor as ex
    monkeypatch.setattr(ex, "TOOL_TIMEOUT", 1)               # 1s deadline vs a 30s sleep
    out, is_error = LocalToolExecutor().dispatch("run_bash", {"command": "sleep 30"}, tmp_path)
    assert is_error and "timed out after 1s" in out          # killed, surfaced as a tool error


def test_gate_times_out_and_fails_closed(tmp_path, monkeypatch):
    import loopkit.executor as ex
    monkeypatch.setattr(ex, "GATE_TIMEOUT", 1)               # 1s deadline vs a 30s sleep
    result = LocalToolExecutor().run_gate("sleep 30", tmp_path)
    assert result.passed is False and "timed out after 1s" in (result.feedback or "")


def test_remote_socket_timeout_sits_above_the_gate_deadline():
    # Nested deadlines: the socket client must outlast the gate's own subprocess deadline so the
    # executor returns a clean failed-gate result rather than the client severing the connection.
    import loopkit.executor as ex
    assert RemoteToolExecutor("/tmp/x.sock")._timeout > ex.GATE_TIMEOUT
