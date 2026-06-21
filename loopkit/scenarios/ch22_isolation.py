"""Ch 22 — agent isolation: the untrusted tool surface runs in a keyless container.

Phase 5a withheld the key from a prompt-injected agent by *mitigation*: load it into loopkit's heap,
then scrub the files + `os.environ` before any agent code spawns. Correct, but timing-dependent — and a
same-uid `ptrace` of loopkit's heap still reaches the in-process key. This chapter shows the structural
fix: **isolation by construction**. The agent's tool calls (`run_bash`/read/write) and the held-out gate
are *dispatched* over a Unix socket to a separate **keyless executor** — so there is nothing to ptrace
and nothing to shred; the boundary is the container/uid, not code ordering.

The seam is `executor.ToolExecutor`: the API adapter and the gate call `dispatch`/`run_gate` on an
injected executor. Locally that's the in-process `LocalToolExecutor` (no split); the cloud worker injects
a `RemoteToolExecutor` pointed at the `loopkit executor` sidecar. This lab stands up a real socket server
(a thread here; a different-uid/PID-namespace container in the pod) and watches loopkit-core — which
holds the key — drive tools through it without the key ever crossing to the executor.

Scripted, token-free: no model, no cluster. The point is the *plumbing of the boundary*, like Ch 20.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from rich.table import Table

from .. import secrets
from ..executor import RemoteToolExecutor, serve
from . import Scenario, Stage

FAKE_KEY = "sk-ant-loopkitcoreholdsthis0123456789"   # stands in for the real per-run API key


def run(stage: Stage) -> None:
    stage.beat("Phase 5a kept the key out of a hijacked agent's reach by [italic]shredding[/] it off "
               "the filesystem and `os.environ` before any agent code ran. Correct — but it's "
               "[bold]timing-dependent[/], and the agent's `run_bash` runs as the [bold]same uid[/] as "
               "loopkit, so a `ptrace` of loopkit's heap still reaches the in-process key. This chapter "
               "is the structural fix: [bold]run the untrusted tool surface as a different identity that "
               "never has the key[/].")

    # loopkit-core holds the key (the LLM call + git need it). In the pod this is the uid-1000 container.
    secrets.install(secrets.CredentialStore({"ANTHROPIC_API_KEY": FAKE_KEY}))
    secrets.register_secret(FAKE_KEY, label="ANTHROPIC_API_KEY")
    stage.beat(f"[bold]loopkit-core[/] (uid 1000) holds the key — `…{FAKE_KEY[-6:]}` — because it makes "
               "the [italic]LLM call[/] and the [italic]git push[/]. It runs only trusted code: the "
               "loop, the model call, clone/commit/push. It [bold]never[/] runs a model-chosen command.")

    sock_dir = tempfile.mkdtemp(prefix="lke", dir="/tmp")
    sock = str(Path(sock_dir) / "exec.sock")
    workspace = Path(tempfile.mkdtemp(prefix="lkws"))
    ready, stop = threading.Event(), threading.Event()
    server = threading.Thread(target=serve, args=(sock,),
                              kwargs={"ready": ready.set, "stop": stop}, daemon=True)
    rows: list[tuple[str, str]] = []
    try:
        server.start()
        ready.wait(timeout=5)
        executor = RemoteToolExecutor(sock)
        stage.beat("The [bold]executor[/] is up on a Unix socket. Here it's a thread; in the cloud "
                   "worker pod it's a [bold]separate container — uid 1001, its own PID namespace, no "
                   "credential mount[/]. loopkit-core dispatches every model-chosen command to it.")

        # 1. A tool call crosses the wire and runs in the executor (the file appears in the workspace).
        out, _ = executor.dispatch("write_file", {"path": "fix.py", "content": "# the agent's edit\n"},
                                   workspace)
        landed = (workspace / "fix.py").exists()
        rows.append(("write_file fix.py", f"{out}  → file present: {landed}"))
        stage.beat(f"The agent's [bold]write_file[/] was dispatched over the socket and executed in the "
                   f"executor against the [bold]shared workspace[/] — the file is there ({landed}). The "
                   "edit happens on the keyless side; loopkit-core only commits it afterward.")

        # 2. The boundary: a prompt-injected `printenv` in the executor finds NO key.
        out, _ = executor.dispatch(
            "run_bash", {"command": "printenv ANTHROPIC_API_KEY || echo NO-KEY-HERE"}, workspace)
        leaked = FAKE_KEY in out
        tail = out.strip().splitlines()[-1] if out.strip() else ""
        rows.append(("run_bash printenv KEY", f"{tail}   (leaked: {leaked})"))
        stage.beat(f"The classic injection — [italic]print the key[/] — runs in the executor and finds "
                   f"[bold]nothing[/] (`{tail}`). In the pod the executor [bold]never loaded a "
                   "credential[/]: it's not in its env, its files, or its address space. There is no "
                   "shred to race and no heap to ptrace — the key simply isn't on that side of the socket.")

        # 3. The held-out gate (agent-authored tests) also runs in the keyless executor.
        gate = executor.run_gate("test -f fix.py && echo gate-ran", workspace)
        rows.append(("run_gate (held-out)", f"passed={gate.passed}"))
        stage.beat("The [bold]held-out gate[/] runs agent-authored tests — so it, too, goes to the "
                   "keyless executor (a malicious `conftest.py` there has no key to exfiltrate). The "
                   "[bold]protected-path guard[/] and [bold]commit-every-tick[/] stay in loopkit-core "
                   "(trusted), operating on the shared workspace — only the [italic]gate command[/] and "
                   "the [italic]tool calls[/] cross over.")

        stage.console.print(_ledger(rows))
    finally:
        stop.set()
        server.join(timeout=5)
        shutil.rmtree(sock_dir, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)
        secrets.clear_registry()
        secrets.install(secrets.CredentialStore())

    stage.beat("That's the whole move: [bold]the untrusted thing has no key[/], enforced by the kernel "
               "(a different uid in its own PID namespace), not by getting the shred order right. It "
               "[italic]replaces[/] the timing-dependent mitigation for the cloud worker — while the "
               "in-process shred stays as the containment for the tiers with no sidecar (CI, local). "
               "Same `ToolExecutor` seam everywhere: `Local` in-process by default, `Remote` to the "
               "sidecar in the pod — a one-line wiring choice, `None`-safe.")


def _ledger(rows: list[tuple[str, str]]) -> Table:
    table = Table(title="loopkit-core → keyless executor (over the socket)", header_style="bold")
    table.add_column("dispatched call")
    table.add_column("result in the executor")
    for call, result in rows:
        table.add_row(call, result)
    return table


SCENARIO = Scenario(chapter=22, slug="isolation", title="Agent isolation (the keyless executor)",
                    teaches="The untrusted tool surface (run_bash + the held-out gate) runs in a "
                            "keyless, different-uid/PID-namespace executor — isolation by construction, "
                            "a kernel boundary that replaces Phase 5a's timing-dependent key-shred.",
                    live_supported=False, run=run)
