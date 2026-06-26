"""The agent: the model as a subroutine the loop invokes (Chapters 1-3).

The loop never speaks to a specific vendor — it calls `Agent.act(prompt, workspace)` and gets back
an `AgentResult`. Swapping one provider for another (or a deterministic `MockAgent` for tests and
demos) is a one-line config change. Each adapter is also responsible for the one thing the loop's
economics (Ch 14) depend on: reporting the cost of the tick in dollars, normalized across vendors.

Two axes, four real adapters (the "2×2 matrix"), plus the token-free mock:

| | **CLI** (shell out to an agent binary that loops internally) | **API** (in-process tool loop via the SDK) |
|------------|--------------------------------------------------------------|--------------------------------------------|
| **Claude** | `claude-code` — `claude -p`                                   | `claude-api` — Anthropic SDK + tool calls  |
| **OpenAI** | `codex` — `codex` CLI headless                                | `openai-api` — OpenAI SDK + function calls  |

CLI adapters are fast to ship and match how people run these tools today, but loopkit only sees
stdout — cost has to be *parsed* from it. API adapters implement the per-tick edit/bash loop
in-process, so they get **native `usage`** and an exact `cost_usd` (via `pricing.py`) — which is
what makes the budget ceiling actually bite. The SDK clients are optional extras (`loopkit[claude]`
/ `loopkit[openai]`); the import is deferred into the backend so `pip install loopkit` pulls
neither, and the core/tests run without them.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from . import secrets, trace
from .executor import LocalToolExecutor, ToolExecutor, _WorkspaceTools  # noqa: F401 — _WorkspaceTools re-exported
from .pricing import DEFAULT_MODELS, Usage, estimate_cost


@dataclass
class AgentResult:
    """The outcome of one invocation. `ok` means the call ran, not that the work is done."""

    ok: bool
    cost_usd: float = 0.0          # normalized cost of this tick (Ch 14)
    summary: str = ""              # short, payload-free description for logs
    raw_tail: str = ""             # last chars of agent output, for feedback only


@runtime_checkable
class Agent(Protocol):
    def act(self, prompt: str, workspace: Path) -> AgentResult: ...


class MockAgent:
    """A deterministic agent for tests, demos, and dry runs.

    Driven by a list of `behaviors`: each is called with the workspace path on its tick and may
    mutate files to simulate the agent's edits, returning a short summary string. When the behaviors
    run out, the agent is a no-op — which the loop reads as 'no progress'. Cost is charged per tick,
    so budget stops (Ch 14) are exercisable without spending a cent.
    """

    def __init__(self, behaviors: list[Callable[[Path], str]] | None = None,
                 cost_per_tick: float = 0.5) -> None:
        self._behaviors = list(behaviors or [])
        self._cost = cost_per_tick
        self._i = 0

    def act(self, prompt: str, workspace: Path) -> AgentResult:
        summary = "noop"
        if self._i < len(self._behaviors):
            summary = self._behaviors[self._i](workspace) or "edit"
        self._i += 1
        return AgentResult(ok=True, cost_usd=self._cost, summary=summary)


# --------------------------------------------------------------------------------------------------
# CLI adapters — shell out to a headless coding-agent binary; cost is parsed from its output.
# --------------------------------------------------------------------------------------------------

class _CLIAdapter:
    """Shared base for agents that shell out to a headless coding-agent CLI."""

    binary: str = ""
    cred_keys: tuple[str, ...] = ()           # the env var(s) THIS vendor binary needs (scrub the rest)

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None,
                 prompt_flag: str = "-p") -> None:
        self.model = model
        self.extra_args = list(extra_args or [])
        self.prompt_flag = prompt_flag

    def _command(self, prompt: str) -> list[str]:
        cmd = [self.binary, self.prompt_flag, prompt]
        if self.model:
            cmd += ["--model", self.model]
        return cmd + self.extra_args

    def act(self, prompt: str, workspace: Path) -> AgentResult:
        # The vendor binary runs its own loop on untrusted instructions: hand it ONLY its model key
        # (no git token, no other provider's key) via a scrubbed env (Part III Phase 5a containment).
        env = secrets.current().child_env(add=self.cred_keys)
        proc = subprocess.run(self._command(prompt), cwd=workspace, env=env,
                              capture_output=True, text=True)
        return self._result(proc)

    def _result(self, proc: subprocess.CompletedProcess) -> AgentResult:
        out = (proc.stdout or "") + (proc.stderr or "")
        return AgentResult(ok=proc.returncode == 0,
                           cost_usd=self._parse_cost(proc.stdout or ""),
                           summary=f"rc={proc.returncode} outLen={len(out)}",
                           raw_tail=secrets.redact(out[-2000:]))

    @staticmethod
    def _parse_cost(stdout: str) -> float:
        # Vendors print cost differently and change it between versions; keep parsing per-adapter.
        # Unknown cost is 0.0 (the budget stop then can't fire — `loopkit doctor` warns about it).
        return 0.0


class ClaudeCodeAdapter(_CLIAdapter):
    """`claude -p "<prompt>" --output-format json` headless. Primary adapter.

    The JSON output is what makes the budget stop usable on the CLI path: it carries
    `total_cost_usd` (and `usage`) alongside the `result` text, so we get a real per-tick cost
    without scraping human prose. The `--output-format json` flag is added automatically unless the
    caller already pinned an output format in their own args.

    Billing: by default the agent's scrubbed env carries only the **subscription** token (or nothing,
    so `claude` uses its on-disk login) — `ANTHROPIC_API_KEY` is withheld so an ambient shell key can't
    silently bill the API. `use_api_key=True` (run --api-key / `[agent] use_api_key`) re-injects the
    full `ADAPTER_KEYS` set, so a present `ANTHROPIC_API_KEY` is used (the billed API).
    """

    binary = "claude"
    cred_keys = secrets.CLAUDE_CODE_SUBSCRIPTION_KEYS          # default: subscription only

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None,
                 use_api_key: bool = False) -> None:
        args = list(extra_args or [])
        if "--output-format" not in args:
            args += ["--output-format", "json"]
        super().__init__(model=model, extra_args=args)
        self.cred_keys = (secrets.ADAPTER_KEYS["claude-code"] if use_api_key
                          else secrets.CLAUDE_CODE_SUBSCRIPTION_KEYS)

    def _result(self, proc: subprocess.CompletedProcess) -> AgentResult:
        out = (proc.stdout or "") + (proc.stderr or "")
        cost, result_text = _parse_claude_json(proc.stdout or "")
        return AgentResult(ok=proc.returncode == 0, cost_usd=cost,
                           summary=f"rc={proc.returncode} costUsd={round(cost, 4)}",
                           raw_tail=secrets.redact((result_text or out)[-2000:]))


class CodexAdapter(_CLIAdapter):
    """Codex parity: same contract, different binary. Cost is derived from token usage in the CLI's
    JSON event stream (`pricing.py` × the configured model) — best-effort, since the Codex output
    format is less stable than Claude's; an unknown model or no usage event yields 0.0."""

    binary = "codex"
    cred_keys = secrets.ADAPTER_KEYS["codex"]

    def _result(self, proc: subprocess.CompletedProcess) -> AgentResult:
        out = (proc.stdout or "") + (proc.stderr or "")
        usage = _parse_codex_usage(proc.stdout or "")
        cost = estimate_cost(self.model, usage)
        return AgentResult(ok=proc.returncode == 0, cost_usd=cost,
                           summary=(f"rc={proc.returncode} in={usage.input_tokens} "
                                    f"out={usage.output_tokens} costUsd={round(cost, 4)}"),
                           raw_tail=secrets.redact(out[-2000:]))


def _parse_claude_json(stdout: str) -> tuple[float, str]:
    """Pull (cost_usd, result_text) out of `claude -p --output-format json` output.

    Handles the three shapes the CLI has shipped across versions: a single result object; a top-level
    **JSON array** of events (current `--output-format json` — the final `subtype:"success"` element
    carries the cost); and stream-json (one object per line). Without the array case the cost parsed
    as 0.0 on current claude builds, which silently defeats the budget ceiling (Ch 14). Returns
    (0.0, "") if nothing parses."""
    text = stdout.strip()
    if not text:
        return 0.0, ""
    data = _loads_lenient(text)
    if isinstance(data, list):
        # Array of events (current CLI): the last element carrying a cost is the final result object.
        data = next((o for o in reversed(data)
                     if isinstance(o, dict) and ("total_cost_usd" in o or "cost_usd" in o)), None)
    elif not isinstance(data, dict):
        # stream-json: one JSON object per line — take the last line that carries a cost.
        for line in reversed(text.splitlines()):
            obj = _loads_lenient(line.strip())
            if isinstance(obj, dict) and ("total_cost_usd" in obj or "cost_usd" in obj):
                data = obj
                break
    if not isinstance(data, dict):
        return 0.0, ""
    raw = data.get("total_cost_usd")
    cost = _as_float(raw if raw is not None else data.get("cost_usd"))
    return cost, str(data.get("result") or "")


def _parse_codex_usage(stdout: str) -> Usage:
    """Best-effort: scan JSON lines for the last usage-like object and normalize it to `Usage`."""
    last: Usage | None = None
    for line in stdout.splitlines():
        obj = _loads_lenient(line.strip())
        usage = _find_usage(obj)
        if usage is not None:
            last = usage
    return last or Usage()


def _find_usage(obj: object) -> Usage | None:
    """Recursively locate a token-usage dict (input/output token counts) anywhere in `obj`."""
    if isinstance(obj, dict):
        keys = obj.keys()
        if "input_tokens" in keys or "output_tokens" in keys:
            return Usage(input_tokens=_as_int(obj.get("input_tokens")),
                         output_tokens=_as_int(obj.get("output_tokens")),
                         cache_read_tokens=_as_int(obj.get("cached_input_tokens")
                                                   or obj.get("cache_read_input_tokens")))
        for value in obj.values():
            found = _find_usage(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_usage(item)
            if found is not None:
                return found
    return None


def _loads_lenient(text: str) -> object:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------------------------------
# API adapters — implement the per-tick tool-calling loop in-process; cost comes from native usage.
# --------------------------------------------------------------------------------------------------
#
# Design seam: the provider-agnostic loop, the workspace tools, and the cost accounting live in
# `_APIAdapter` (loopkit's logic, fully tested with an injected fake backend — zero tokens). Each
# `_Backend` is the thin, provider-specific edge: one model call + translating the neutral transcript
# to the SDK's message shape and the SDK's `usage` back to a normalized `Usage`. That edge is the
# analogue of the subprocess call in the CLI adapters — not unit-tested without tokens, but small.

@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    parameters: dict          # JSON Schema for the tool's input


@dataclass
class _ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class _Turn:
    """One model response: assistant text, any tool calls it wants run, and the call's usage."""

    text: str
    tool_calls: list[_ToolCall]
    usage: Usage = field(default_factory=Usage)


class _Backend(Protocol):
    model: str

    def complete(self, transcript: list[dict], tools: list[_ToolSpec]) -> _Turn: ...


# The loopkit-defined tools an API adapter exposes to the model. Deliberately minimal — read, write,
# run — which is the whole surface a coding agent needs for one tick; the loop's safety guard, gates,
# and commit-every-tick wrap whatever edits result, unchanged from the CLI path.
_TOOLS: list[_ToolSpec] = [
    _ToolSpec("read_file", "Read a UTF-8 text file by its path relative to the repository root.",
              {"type": "object", "properties": {"path": {"type": "string"}},
               "required": ["path"], "additionalProperties": False}),
    _ToolSpec("write_file", "Create or overwrite a UTF-8 text file (path relative to the repo root).",
              {"type": "object",
               "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
               "required": ["path", "content"], "additionalProperties": False}),
    _ToolSpec("run_bash", "Run a shell command in the repository root; returns exit code + output.",
              {"type": "object", "properties": {"command": {"type": "string"}},
               "required": ["command"], "additionalProperties": False}),
]


class _APIAdapter:
    """Provider-agnostic tool-calling loop for one tick. Drives a `_Backend` until the model stops
    requesting tools (or a per-tick cap is hit), executing each tool call against the workspace and
    accumulating native usage into an exact `cost_usd` via `pricing.estimate_cost`.

    Tool execution is dispatched through an injected `ToolExecutor` (default `LocalToolExecutor`, the
    in-process path). The cloud worker injects a `RemoteToolExecutor` so the model's chosen commands
    run in a keyless, isolated container — loopkit-core (this process) keeps the key for the LLM call
    but never executes a model-chosen command itself (Phase 6 agent isolation)."""

    def __init__(self, backend: _Backend, *, max_tool_calls: int = 25,
                 executor: ToolExecutor | None = None) -> None:
        self._backend = backend
        self._max = max_tool_calls
        self._executor = executor or LocalToolExecutor()

    @property
    def model(self) -> str:
        return self._backend.model

    def act(self, prompt: str, workspace: Path) -> AgentResult:
        transcript: list[dict] = [{"role": "user", "content": prompt}]
        total = Usage()
        last_text = ""
        n_calls = 0
        # +1 so the model gets a turn to speak after its final tool batch (the closing message).
        for _ in range(self._max + 1):
            # One `llm` span per model call: messages in, text + tool calls out, usage/cost metadata.
            # Nests under the loop's `agent` span via LangSmith contextvars (no tracer threaded in).
            with trace.span(f"llm:{self._backend.model}", run_type="llm",
                            inputs={"messages": _trace_messages(transcript)},
                            metadata={"model": self._backend.model}) as llm_span:
                turn = self._backend.complete(transcript, _TOOLS)
                llm_span.outputs(
                    text=turn.text or None,
                    tool_calls=[{"name": c.name, "args": c.args} for c in turn.tool_calls] or None)
                llm_span.metadata(input_tokens=turn.usage.input_tokens,
                                  output_tokens=turn.usage.output_tokens,
                                  cache_read_tokens=turn.usage.cache_read_tokens,
                                  cache_write_tokens=turn.usage.cache_write_tokens,
                                  cost_usd=round(estimate_cost(self._backend.model, turn.usage), 6))
            total = total + turn.usage
            if turn.text:
                last_text = turn.text
            if not turn.tool_calls:
                break
            transcript.append({"role": "assistant", "text": turn.text, "calls": turn.tool_calls})
            results = []
            for call in turn.tool_calls:
                n_calls += 1
                with trace.span(f"tool:{call.name}", run_type="tool", inputs=call.args) as tool_span:
                    output, is_error = self._executor.dispatch(call.name, call.args, workspace)
                    # Redact at capture: this output re-enters the transcript and is re-sent to the
                    # model on the next tick (the wire), so scrub a key here, not only in the trace.
                    output = secrets.redact(output)
                    tool_span.outputs(output=output, is_error=is_error)
                results.append({"id": call.id, "name": call.name,
                                "content": output, "is_error": is_error})
            transcript.append({"role": "tool", "results": results})
        cost = estimate_cost(self._backend.model, total)
        return AgentResult(
            ok=True, cost_usd=cost,
            summary=(f"toolCalls={n_calls} in={total.input_tokens} "
                     f"out={total.output_tokens} costUsd={round(cost, 4)}"),
            raw_tail=secrets.redact(last_text[-2000:]))


class ClaudeAPIAdapter(_APIAdapter):
    """Claude via the Anthropic SDK (`loopkit[claude]`). Default model: `claude-opus-4-8`.

    `backend` is injectable for token-free tests/scenarios; left None, it builds a real
    `_AnthropicBackend` whose SDK import is deferred until first use.
    """

    def __init__(self, model: str | None = None, *, client: object | None = None,
                 backend: _Backend | None = None, max_tool_calls: int = 25,
                 executor: ToolExecutor | None = None) -> None:
        backend = backend or _AnthropicBackend(model or DEFAULT_MODELS["claude-api"], client=client)
        super().__init__(backend, max_tool_calls=max_tool_calls, executor=executor)


class OpenAIAPIAdapter(_APIAdapter):
    """OpenAI via the OpenAI SDK (`loopkit[openai]`). Default model: `gpt-4o`. `backend` injectable."""

    def __init__(self, model: str | None = None, *, client: object | None = None,
                 backend: _Backend | None = None, max_tool_calls: int = 25,
                 executor: ToolExecutor | None = None) -> None:
        backend = backend or _OpenAIBackend(model or DEFAULT_MODELS["openai-api"], client=client)
        super().__init__(backend, max_tool_calls=max_tool_calls, executor=executor)


class _AnthropicBackend:
    """One Anthropic Messages API call + neutral⇄SDK translation. SDK import deferred (optional dep)."""

    def __init__(self, model: str, *, client: object | None = None, max_tokens: int = 8000) -> None:
        self.model = model
        self._client = client
        self._max_tokens = max_tokens

    def _ensure_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:   # pragma: no cover - exercised only without the extra
                raise RuntimeError("the claude-api adapter needs the Anthropic SDK: "
                                   "pip install 'loopkit[claude]'") from exc
            # Pass the key explicitly: the worker scrubs it from os.environ at load (Phase 5a), so a
            # zero-arg `Anthropic()` would find nothing. None (laptop/no-store) lets the SDK read env.
            self._client = anthropic.Anthropic(api_key=secrets.current().api_key("claude-api"))
        return self._client

    def complete(self, transcript: list[dict], tools: list[_ToolSpec]) -> _Turn:
        client = self._ensure_client()
        tool_defs = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                     for t in tools]
        resp = client.messages.create(model=self.model, max_tokens=self._max_tokens,
                                      tools=tool_defs, messages=_to_anthropic_messages(transcript))
        text_parts: list[str] = []
        calls: list[_ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(_ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
        u = resp.usage
        usage = Usage(input_tokens=getattr(u, "input_tokens", 0) or 0,
                      output_tokens=getattr(u, "output_tokens", 0) or 0,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0)
        return _Turn(text="\n".join(text_parts), tool_calls=calls, usage=usage)


class _OpenAIBackend:
    """One OpenAI Chat Completions call + neutral⇄SDK translation. SDK import deferred (optional dep)."""

    def __init__(self, model: str, *, client: object | None = None, max_tokens: int = 4000) -> None:
        self.model = model
        self._client = client
        self._max_tokens = max_tokens

    def _ensure_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError as exc:   # pragma: no cover - exercised only without the extra
                raise RuntimeError("the openai-api adapter needs the OpenAI SDK: "
                                   "pip install 'loopkit[openai]'") from exc
            # Explicit key for the same reason as the Anthropic backend (env is scrubbed at load).
            self._client = openai.OpenAI(api_key=secrets.current().api_key("openai-api"))
        return self._client

    def complete(self, transcript: list[dict], tools: list[_ToolSpec]) -> _Turn:
        client = self._ensure_client()
        tool_defs = [{"type": "function",
                      "function": {"name": t.name, "description": t.description,
                                   "parameters": t.parameters}} for t in tools]
        resp = client.chat.completions.create(model=self.model, tools=tool_defs,
                                              messages=_to_openai_messages(transcript))
        message = resp.choices[0].message
        calls: list[_ToolCall] = []
        for tc in (message.tool_calls or []):
            calls.append(_ToolCall(id=tc.id, name=tc.function.name,
                                   args=_loads_lenient(tc.function.arguments or "{}") or {}))
        u = resp.usage
        cached = 0
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        prompt_tokens = getattr(u, "prompt_tokens", 0) or 0
        usage = Usage(input_tokens=max(prompt_tokens - cached, 0),
                      output_tokens=getattr(u, "completion_tokens", 0) or 0,
                      cache_read_tokens=cached)
        return _Turn(text=message.content or "", tool_calls=calls, usage=usage)


def _to_anthropic_messages(transcript: list[dict]) -> list[dict]:
    """Neutral transcript → Anthropic content-block messages (tool_use on assistant, tool_result
    inside a *user* turn, as the Messages API requires)."""
    messages: list[dict] = []
    for entry in transcript:
        if entry["role"] == "user":
            messages.append({"role": "user", "content": entry["content"]})
        elif entry["role"] == "assistant":
            content: list[dict] = []
            if entry.get("text"):
                content.append({"type": "text", "text": entry["text"]})
            for call in entry.get("calls", []):
                content.append({"type": "tool_use", "id": call.id,
                                "name": call.name, "input": call.args})
            messages.append({"role": "assistant", "content": content})
        elif entry["role"] == "tool":
            content = [{"type": "tool_result", "tool_use_id": r["id"],
                        "content": r["content"], "is_error": r["is_error"]}
                       for r in entry["results"]]
            messages.append({"role": "user", "content": content})
    return messages


def _to_openai_messages(transcript: list[dict]) -> list[dict]:
    """Neutral transcript → OpenAI chat messages (assistant `tool_calls` + one `tool` message per
    result, keyed by `tool_call_id`)."""
    messages: list[dict] = []
    for entry in transcript:
        if entry["role"] == "user":
            messages.append({"role": "user", "content": entry["content"]})
        elif entry["role"] == "assistant":
            message: dict = {"role": "assistant", "content": entry.get("text") or None}
            calls = entry.get("calls", [])
            if calls:
                message["tool_calls"] = [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.name, "arguments": json.dumps(call.args)}}
                    for call in calls]
            messages.append(message)
        elif entry["role"] == "tool":
            for r in entry["results"]:
                messages.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
    return messages


def _trace_messages(transcript: list[dict]) -> list[dict]:
    """Render the neutral transcript into plain, human-readable dicts for an `llm` span's input
    (the `_ToolCall` objects aren't JSON-able, so flatten them to name/args)."""
    out: list[dict] = []
    for entry in transcript:
        if entry["role"] == "user":
            out.append({"role": "user", "content": entry["content"]})
        elif entry["role"] == "assistant":
            item: dict = {"role": "assistant"}
            if entry.get("text"):
                item["text"] = entry["text"]
            if entry.get("calls"):
                item["tool_calls"] = [{"name": c.name, "args": c.args} for c in entry["calls"]]
            out.append(item)
        elif entry["role"] == "tool":
            out.append({"role": "tool",
                        "results": [{"name": r["name"], "content": r["content"],
                                     "is_error": r["is_error"]} for r in entry["results"]]})
    return out


def build_agent(cfg, *, executor: ToolExecutor | None = None) -> Agent:
    """Resolve the configured adapter name to a concrete Agent (AgentConfig in).

    `executor` is the Phase-6 seam: the cloud worker passes a `RemoteToolExecutor` so an API adapter's
    tool calls run in the keyless executor sidecar, not in this (key-holding) process. None ⇒ the
    in-process `LocalToolExecutor` — exact prior behavior for local runs and the dev fleet. CLI/mock
    adapters ignore it (a vendor binary loops internally; the mock has no tools).
    """
    name = cfg.adapter
    if name == "mock":
        return MockAgent()
    if name == "claude-code":
        return ClaudeCodeAdapter(model=cfg.model, extra_args=cfg.args, use_api_key=cfg.use_api_key)
    if name == "codex":
        return CodexAdapter(model=cfg.model, extra_args=cfg.args)
    if name == "claude-api":
        return ClaudeAPIAdapter(model=cfg.model, max_tool_calls=cfg.max_tool_calls, executor=executor)
    if name == "openai-api":
        return OpenAIAPIAdapter(model=cfg.model, max_tool_calls=cfg.max_tool_calls, executor=executor)
    raise ValueError(f"unknown agent adapter: {name!r} "
                     "(expected: mock | claude-code | codex | claude-api | openai-api)")
