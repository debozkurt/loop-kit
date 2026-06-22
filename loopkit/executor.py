"""Tool execution behind a seam — so the untrusted surface can be relocated off the key (Phase 6).

The agent's tool calls (`read_file`/`write_file`/`run_bash`) and the held-out gate are the one part
of a tick that runs **attacker-influenced commands** — a prompt-injected issue body can make the model
ask for any shell command. Phase 5a kept a real key out of their reach by *mitigation* (load the key
into loopkit's heap, then scrub the files + `os.environ` before any agent code spawns). That holds, but
it is timing-dependent and a same-uid `ptrace` of loopkit's heap still reaches the in-process key.

This module is the structural fix: a `ToolExecutor` seam so that tool execution is **dispatched** rather
than done in-process. Two implementations sit behind one protocol:

- **`LocalToolExecutor`** — the in-process default. Tool calls + the gate run in this process, exactly
  as before. Local `loopkit run` and the dev fleet keep it (trusted context, no split) — zero behavior
  change, and the Phase-5a `child_env()` scrub still applies, so it remains the containment for the
  non-split CI tier too.
- **`RemoteToolExecutor`** — a tiny Unix-socket client. The **cloud worker** injects it so the model's
  chosen commands and the agent-authored gate run in a separate **keyless, different-uid/PID-namespace
  container** (the `loopkit executor` sidecar) that never holds the credential. There is then nothing to
  `ptrace` and nothing to shred — the boundary is enforced by the kernel, not by code ordering.

Core + stdlib-only (importing it pulls nothing): `socket`/`struct`/`json` for the wire, `subprocess`
for the local path. The split is a cloud-only *wiring* choice; the core contracts (`Agent`, `Gate`,
`run_loop`) stay None-safe with `Local` as the default.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

from . import secrets
from .gate import GateResult
from .log import get_logger

if TYPE_CHECKING:                                  # pragma: no cover - typing only
    import threading

log = get_logger("executor")

# Cap tool output so one tick can't blow up the context (and the socket frame stays small).
MAX_OUTPUT = 8000

# Liveness bounds (Finding D) — a single tool/gate subprocess can't wedge a tick forever. A
# prompt-injected `run_bash "sleep infinity"` or a hung gate now fails the *call* after its deadline
# (the model sees the timeout and adapts) instead of blocking until the socket client gives up and a
# wedged pod runs until the node reaps it. The deadlines nest: the tool/gate subprocess deadline <
# the RemoteToolExecutor socket-client deadline (so the server returns a clean error first) < the
# Job `activeDeadlineSeconds` (cloudrun) that caps the whole run.
TOOL_TIMEOUT = 120                                   # seconds — one agent shell command
GATE_TIMEOUT = 600                                   # seconds — the held-out gate (a real suite is slow)

# Generic failure-signal markers for shaping a long gate log (test-runner-agnostic — pytest/unittest/
# jest/compilers/make all emit these). Used to surface the failing lines a blind tail would drop.
_FAILURE_MARKERS = ("error", "fail", "assert", "traceback", "exception", "panic", "not ok", "✗")


def shape_failure_output(text: str, *, budget: int = 2000) -> str:
    """Bound + shape a failing gate's output into high-signal feedback.

    A gate's feedback is the agent's primary signal *and* it spends the budget stop, so a 10k-line
    blind tail is both context-rot and money (Anthropic, *Writing tools for agents*; SWE-agent's ACI:
    feed the agent the failing lines + a steer, not raw output). Short output passes through unchanged
    (exact prior behavior). Long output keeps the **tail** — where test runners print their summary —
    and **surfaces failure-marker lines** from the part the tail truncated, so a failure early in a
    long log isn't lost behind teardown noise. Bounded to ~2×budget.
    """
    text = text or ""
    if len(text) <= budget:
        return text                                   # short: unchanged (preserves the prior contract)
    tail = text[-budget:]
    seen: set[str] = set()
    signal: list[str] = []
    for line in text[:-budget].splitlines():          # only the region the tail dropped
        stripped = line.rstrip()
        if stripped and stripped not in seen and any(m in line.lower() for m in _FAILURE_MARKERS):
            seen.add(stripped)
            signal.append(stripped)
    if not signal:
        return tail
    digest = "\n".join(signal[-12:])[:budget]         # the dozen most-recent failure lines, bounded
    return f"--- key failures (earlier in the log) ---\n{digest}\n--- output tail ---\n{tail}"


def validate_syntax(path: str, content: str) -> str | None:
    """Best-effort syntax check for languages we can parse cheaply (Python, JSON). Returns an error
    string to surface, or None when the content is acceptable / the language is unguarded.

    SWE-agent's most-cited ACI win: **reject a syntactically-broken edit at the tool boundary** so the
    bad state never lands, instead of letting the agent write garbage and spend turns discovering and
    unwinding it. We only guard what we can validate with the stdlib and no I/O; everything else writes
    as before. Empty content is allowed (an empty `.py` module is valid; an empty file is a legitimate
    intermediate).
    """
    suffix = Path(path).suffix.lower()
    if not content.strip():
        return None
    try:
        if suffix == ".py":
            compile(content, path, "exec")
        elif suffix == ".json":
            json.loads(content)
    except SyntaxError as exc:
        return f"{suffix} syntax error at line {exc.lineno}: {exc.msg}"
    except ValueError as exc:                          # json.JSONDecodeError is a ValueError
        return f"{suffix} parse error: {exc}"
    return None


class _WorkspaceTools:
    """Executes the API adapter's tool calls against the run's workspace, sandboxed to its root.

    Every path is resolved and confined to the workspace (no traversal out via `..`, symlinks, or
    absolute paths). Errors become tool *outputs* with `is_error=True` — never raised — so the model
    can read the failure and adapt on the next turn, exactly as a human-driven agent would.

    Lives here (not in `agent.py`) because in the cloud split it runs inside the keyless executor
    sidecar, not in loopkit-core. `agent.py` re-exports it for backward compatibility.
    """

    def __init__(self, workspace: Path) -> None:
        self.root = Path(workspace).resolve()

    def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            if name == "read_file":
                return self._read(str(args.get("path", "")))
            if name == "write_file":
                return self._write(str(args.get("path", "")), str(args.get("content", "")))
            if name == "run_bash":
                return self._bash(str(args.get("command", "")))
            return f"unknown tool {name!r}", True
        except Exception as exc:   # noqa: BLE001 — tool errors are fed back to the model, never raised
            return f"error: {exc}", True

    def _resolve(self, path: str) -> Path:
        candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"path {path!r} escapes the repository root")
        return candidate

    def _read(self, path: str) -> tuple[str, bool]:
        target = self._resolve(path)
        if not target.is_file():
            return f"no such file: {path}", True
        return target.read_text()[: MAX_OUTPUT], False

    def _write(self, path: str, content: str) -> tuple[str, bool]:
        target = self._resolve(path)
        # Edit-time guardrail (SWE-agent ACI): refuse a syntactically-broken edit before it lands.
        problem = validate_syntax(path, content)
        if problem is not None:
            return (f"edit REJECTED — {path} was NOT written ({problem}). "
                    f"Fix the syntax and write the complete, valid file.", True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path} ({len(content)} bytes)", False

    def _bash(self, command: str) -> tuple[str, bool]:
        # The agent's shell runs untrusted-derived commands, so it gets a credential-free env. In the
        # cloud split this process (the executor sidecar) holds no key at all; on the local/CI path the
        # key lives only in loopkit's heap and `child_env()` scrubs it from the child — either way the
        # subprocess sees nothing. loopkit's own git calls (in loopkit-core) re-inject the git token.
        try:
            proc = subprocess.run(command, shell=True, cwd=self.root,
                                  env=secrets.current().child_env(), capture_output=True,
                                  text=True, timeout=TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Liveness bound (Finding D): a hung/instructed-to-hang command fails the tool call rather
            # than wedging the tick. is_error=True so the model reads it and adapts on the next turn.
            return f"command timed out after {TOOL_TIMEOUT}s and was killed", True
        out = ((proc.stdout or "") + (proc.stderr or ""))[: MAX_OUTPUT]
        return f"exit={proc.returncode}\n{out}", False


@runtime_checkable
class ToolExecutor(Protocol):
    """The seam: where a tick's untrusted commands and the held-out gate actually run.

    `dispatch` runs one tool call; `run_gate` runs a gate command. Both take the workspace explicitly
    (the cloud split shares one emptyDir mounted at the same path in both containers, so the path is
    valid on either side of the socket). Returns mirror the in-process contracts so callers are
    executor-agnostic: `(output, is_error)` for a tool, `GateResult` for a gate.
    """

    def dispatch(self, name: str, args: dict, workspace: Path) -> tuple[str, bool]: ...

    def run_gate(self, command: str, workspace: Path, *, tail: int = 2000) -> GateResult: ...


class LocalToolExecutor:
    """The default: run tool calls + the gate in this process. Exact prior behavior (trusted/dev)."""

    def dispatch(self, name: str, args: dict, workspace: Path) -> tuple[str, bool]:
        return _WorkspaceTools(Path(workspace)).dispatch(name, args)

    def run_gate(self, command: str, workspace: Path, *, tail: int = 2000) -> GateResult:
        # The gate runs the project's tests — including any conftest/test the AGENT wrote — so it must
        # carry no credentials (the trust anchor was an exfil sink otherwise; Phase 5a). On the local/CI
        # path `child_env()` scrubs the key; in the cloud split this runs in the keyless executor.
        # PYTHONDONTWRITEBYTECODE keeps a python gate from littering __pycache__ into a protected path.
        env = {**secrets.current().child_env(), "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run(command, cwd=workspace, shell=True, env=env,
                                  capture_output=True, text=True, timeout=GATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Liveness bound (Finding D): a hung gate is a failed gate, not a wedged run. Fail-closed —
            # a DONE certification can never come from a gate that didn't actually finish passing.
            return GateResult(False, f"gate timed out after {GATE_TIMEOUT}s (treated as failed)")
        if proc.returncode == 0:
            return GateResult(True, None)
        # Shape the failing output into high-signal, budget-bounded feedback (not a blind tail).
        feedback = shape_failure_output((proc.stdout or "") + (proc.stderr or ""), budget=tail)
        return GateResult(False, feedback)


# --------------------------------------------------------------------------------------------
# Wire protocol — length-prefixed JSON over a Unix stream socket. One request per connection
# (connect → send → recv → close): no cross-request framing state, so a crashed handler can't
# corrupt the next call. stdlib-only (socket/struct/json), so the executor module pulls nothing.
# --------------------------------------------------------------------------------------------
def _recvall(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None                                          # peer closed mid-frame
        buf += chunk
    return bytes(buf)


def _send_frame(sock: socket.socket, obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv_frame(sock: socket.socket) -> dict | None:
    header = _recvall(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    body = _recvall(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


class RemoteToolExecutor:
    """A `ToolExecutor` that forwards each call to a `loopkit executor` server over a Unix socket.

    The cloud worker injects this into the API adapter and `run_loop`, so the model's chosen commands
    and the held-out gate run in the keyless executor sidecar — not in loopkit-core, which holds the
    key. A failure to reach the executor degrades to a tool/gate **error** (not a crash): the tick sees
    a clear failure and the loop's stops handle it, rather than the run dying on a transport hiccup.
    """

    def __init__(self, socket_path: str | os.PathLike[str],
                 *, timeout: float = GATE_TIMEOUT + 60.0) -> None:
        # The socket deadline sits *above* the gate's own subprocess deadline (GATE_TIMEOUT) so the
        # executor returns a clean failed-gate result first, rather than the client severing the
        # connection mid-gate and surfacing a transport error (Finding D — nested deadlines).
        self._path = str(socket_path)
        self._timeout = timeout

    def _call(self, request: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(self._path)
            _send_frame(sock, request)
            response = _recv_frame(sock)
        if response is None:
            raise ConnectionError("executor closed the connection before responding")
        return response

    def dispatch(self, name: str, args: dict, workspace: Path) -> tuple[str, bool]:
        try:
            resp = self._call({"op": "dispatch", "name": name, "args": args,
                               "workspace": str(workspace)})
            return str(resp.get("output", "")), bool(resp.get("is_error", False))
        except Exception as exc:   # noqa: BLE001 — a transport failure is a tool error, not a crash
            log.error("executor.dispatch_unreachable", tool=name, err=type(exc).__name__)
            return f"executor unavailable ({type(exc).__name__})", True

    def run_gate(self, command: str, workspace: Path, *, tail: int = 2000) -> GateResult:
        try:
            resp = self._call({"op": "run_gate", "command": command,
                               "workspace": str(workspace), "tail": tail})
            return GateResult(bool(resp.get("passed", False)), resp.get("feedback"))
        except Exception as exc:   # noqa: BLE001 — a transport failure fails the gate, doesn't crash
            log.error("executor.gate_unreachable", err=type(exc).__name__)
            return GateResult(False, f"executor unavailable ({type(exc).__name__})")


def _handle_connection(conn: socket.socket, executor: ToolExecutor) -> None:
    """Serve one request on `conn` and reply. Errors become an error *response*, never an exception
    that takes down the server — the executor surface is exactly where untrusted code runs."""
    try:
        request = _recv_frame(conn)
        if request is None:
            return
        op = request.get("op")
        if op == "dispatch":
            output, is_error = executor.dispatch(
                str(request.get("name", "")), request.get("args") or {},
                Path(str(request.get("workspace", "."))))
            _send_frame(conn, {"output": output, "is_error": is_error})
        elif op == "run_gate":
            result = executor.run_gate(
                str(request.get("command", "")), Path(str(request.get("workspace", "."))),
                tail=int(request.get("tail", 2000)))
            _send_frame(conn, {"passed": result.passed, "feedback": result.feedback})
        else:
            _send_frame(conn, {"output": f"unknown op {op!r}", "is_error": True,
                               "passed": False, "feedback": f"unknown op {op!r}"})
    except Exception as exc:   # noqa: BLE001 — keep the server alive across a bad request
        log.error("executor.handle_failed", err=type(exc).__name__)
        try:
            _send_frame(conn, {"output": f"executor error ({type(exc).__name__})", "is_error": True,
                               "passed": False, "feedback": f"executor error ({type(exc).__name__})"})
        except OSError:
            pass


def serve(socket_path: str | os.PathLike[str], *, executor: ToolExecutor | None = None,
          ready: Callable[[], None] | None = None, stop: "threading.Event | None" = None,
          backlog: int = 16) -> None:
    """Run the keyless tool-execution server, listening on a Unix socket (the cloud `executor` sidecar).

    Dispatches every request to a `LocalToolExecutor` (the same `_WorkspaceTools`/gate code as the
    in-process path) — but in a container that **never loads a credential**, so the untrusted surface
    has no key to read. The socket is `0660` (group-connectable: loopkit-core shares the pod's
    `fsGroup`); `umask 002` makes agent-written files group-writable so loopkit-core can commit them.

    `ready`/`stop` are test seams: `ready()` fires once the socket is bound; a `stop` Event ends the
    accept loop (production passes neither — it serves until the pod is torn down).
    """
    executor = executor or LocalToolExecutor()
    path = str(socket_path)
    os.umask(0o002)                                              # group-writable shared workspace
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(path)                                          # clear a stale socket from a prior pod
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        try:
            os.chmod(path, 0o660)                                # loopkit-core (same gid) may connect
        except OSError:                                          # pragma: no cover - platform dependent
            pass
        server.listen(backlog)
        if stop is not None:
            server.settimeout(0.5)                              # so the loop can observe `stop`
        log.info("executor.serving", path=path)
        if ready is not None:
            ready()
        while stop is None or not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:                              # only with a stop Event set
                continue
            with conn:
                _handle_connection(conn, executor)
    finally:
        server.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        log.info("executor.stopped", path=path)
