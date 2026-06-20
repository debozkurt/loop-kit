"""LangSmith tracing — full-tree observability for a run (optional extra, auto-on).

This is a **peer to `log.py`**, the two halves of loopkit's observability story:

- **Logs** (`log.py`) are the always-on, payload-free flight recorder: one greppable line per event,
  ids/lengths/counts only — safe to ship anywhere.
- **Traces** (this module) are the rich, opt-in picture: a nested run tree where each span carries the
  **full human-readable input/output**, **every tool call**, and **organized cost/usage/model
  metadata**. A trace backend (LangSmith) is the one place payloads belong — controlled, access-gated,
  not stdout — so capturing them here does not violate the never-log-payloads rule.

Shape of a traced run (the whole system, single loop *and* fleet workers):

    loopkit run "<goal>"                 chain   inputs: goal/repo/branch · meta: run-id/adapter/model/budget
     ├─ tick 1                           chain   meta: tick=1
     │   ├─ agent                        chain   inputs: prompt · out: summary · meta: cost_usd
     │   │   ├─ llm:<model>              llm     inputs: messages · out: text+tool_calls · meta: tokens/cost
     │   │   ├─ tool:write_file          tool    inputs: path/content · out: result
     │   │   └─ tool:run_bash            tool    inputs: command · out: exit+output
     │   ├─ iteration gate               tool    out: passed
     │   └─ acceptance gate              tool    out: passed
     └─ DONE  cost=$0.21  iters=2

**Design invariants.** `langsmith` is an optional dependency behind the `loopkit[trace]` extra; the
import is **deferred** so importing this module never pulls it and the disabled path is a cheap
function call. Tracing **auto-activates** when `langsmith` is installed AND a LangSmith API key (or
`LANGSMITH_TRACING`) is present in the environment — no config flag required (`[trace] enabled` can
force it on/off). When inactive, every span is a no-op that returns the prior behavior exactly, so
core modules can call `trace.span(...)` unconditionally. Spans auto-nest via LangSmith's contextvars,
so an adapter's `llm`/`tool` spans parent themselves under whatever span the loop has open — no tracer
object is threaded through the `Agent` contract.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from .log import get_logger

# Module-level configuration, set once by `configure()` at an entry point (CLI run / fleet worker).
_FORCED: bool | None = None        # None = auto-detect from env; True/False = explicit override
_PROJECT: str | None = None        # LangSmith project name; None falls back to env, then "loopkit"
_PROVIDER_CACHE: object | None = None   # the resolved langsmith `trace` factory (or None), cached
_RESOLVED = False                  # whether _PROVIDER_CACHE has been computed yet

_MAX_FIELD_CHARS = 50_000          # cap any single traced field so a pathological blob can't balloon

_log = get_logger("trace")


def configure(cfg: object | None) -> None:
    """Apply a `TraceConfig` (or anything with `.enabled`/`.project`) at an entry point.

    `enabled=None` keeps auto-detection; `True`/`False` forces it. Re-reads the provider next call.
    """
    global _FORCED, _PROJECT
    if cfg is not None:
        _FORCED = getattr(cfg, "enabled", None)
        _PROJECT = getattr(cfg, "project", None)
    _reset()


def set_enabled(value: bool | None) -> None:
    global _FORCED
    _FORCED = value
    _reset()


def _reset() -> None:
    global _PROVIDER_CACHE, _RESOLVED
    _PROVIDER_CACHE, _RESOLVED = None, False


def _enabled() -> bool:
    """True if tracing should run: explicit override wins; else auto-detect a LangSmith key/flag."""
    if _FORCED is not None:
        return _FORCED
    return bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
                or _truthy(os.getenv("LANGSMITH_TRACING")) or _truthy(os.getenv("LANGCHAIN_TRACING_V2")))


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def project() -> str:
    """The LangSmith project traces land in: config → env → 'loopkit'."""
    return _PROJECT or os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or "loopkit"


def active() -> bool:
    """Whether tracing is currently live (enabled *and* langsmith importable). For `doctor`."""
    return _provider() is not None


_TRUSTSTORE_TRIED = False


def _ensure_os_trust() -> None:
    """Route TLS verification through the OS trust store, once, when tracing turns on.

    **Local-dev-only workaround.** On a corp dev network a TLS-intercepting proxy (Zscaler) presents
    a corp-signed certificate that Python's bundled CAs don't trust ("certificate verify failed:
    unable to get local issuer certificate"), so the LangSmith uploader silently drops every trace.
    `truststore` makes Python verify against the OS trust store (macOS keychain / Windows store),
    which holds the corp root CA. It is a **dev-only dependency** (`loopkit[dev]`, never the prod
    `[trace]` extra): prod talks to LangSmith over normal TLS with standard CAs and needs no
    interception workaround. This call injects truststore **only if it's importable**, so it
    self-activates in local dev and is a clean no-op in prod (truststore absent). It never disables
    verification, and never breaks the run if injection fails.
    """
    global _TRUSTSTORE_TRIED
    if _TRUSTSTORE_TRIED:
        return
    _TRUSTSTORE_TRIED = True
    try:
        import truststore
        truststore.inject_into_ssl()
        _log.info("trace.truststore", detail="OS_trust_store_injected_for_TLS")
    except Exception:   # noqa: BLE001 — truststore absent / injection failed; carry on uninjected
        pass


def _provider():
    """Resolve and cache the langsmith `trace` context-manager factory, or None if off/absent."""
    global _PROVIDER_CACHE, _RESOLVED
    if _RESOLVED:
        return _PROVIDER_CACHE
    _RESOLVED = True
    if not _enabled():
        _PROVIDER_CACHE = None
        return None
    _ensure_os_trust()
    try:
        from langsmith import trace as ls_trace
    except ImportError:
        # Enabled by env but the extra isn't installed — say so once, then stay a no-op.
        _log.warn("trace.unavailable", detail="LANGSMITH key set but langsmith not installed "
                  "(pip install 'loopkit[trace]')")
        _PROVIDER_CACHE = None
        return None
    _log.info("trace.enabled", project=project())
    _PROVIDER_CACHE = ls_trace
    return _PROVIDER_CACHE


@contextmanager
def span(name: str, *, run_type: str = "chain", inputs: dict | None = None,
         metadata: dict | None = None, tags: list[str] | None = None) -> Iterator["_Span"]:
    """Open a trace span (no-op when tracing is inactive). Nests under any enclosing span.

    `run_type` is a LangSmith run type — `"llm"` for model calls (so usage/cost render natively),
    `"tool"` for tool calls, `"chain"` for structural spans (run/tick/agent). Use the yielded
    handle's `.outputs(**)` / `.metadata(**)` to attach human-readable results and organized metadata.
    """
    factory = _provider()
    if factory is None:
        yield _NOOP
        return
    try:
        cm = factory(name=name, run_type=run_type, inputs=_clean(inputs) or {},
                     metadata=_clean(metadata), tags=tags, project_name=project())
    except Exception as exc:   # noqa: BLE001 — tracing must never break the run it observes
        _log.warn("trace.open_failed", name=name, err=str(exc))
        yield _NOOP
        return
    with cm as run_tree:
        handle = _Span(run_tree)
        try:
            yield handle
        except BaseException as exc:   # record the failure on the span, then re-raise unchanged
            handle._error(f"{type(exc).__name__}: {exc}")
            raise


class _Span:
    """Handle to a live span. Failures to record are swallowed — observability never breaks the run."""

    def __init__(self, run_tree: object) -> None:
        self._rt = run_tree

    def outputs(self, **fields: object) -> None:
        self._apply("add_outputs", _clean(fields))

    def metadata(self, **fields: object) -> None:
        self._apply("add_metadata", _clean(fields))

    def _error(self, message: str) -> None:
        try:
            self._rt.add_metadata({"error": message})
        except Exception:   # noqa: BLE001
            pass

    def _apply(self, method: str, payload: dict | None) -> None:
        if not payload:
            return
        try:
            getattr(self._rt, method)(payload)
        except Exception:   # noqa: BLE001 — never let a tracing call raise into the run
            pass


class _NoopSpan:
    """The disabled-path span: every recording call is a cheap no-op."""

    def outputs(self, **fields: object) -> None: ...
    def metadata(self, **fields: object) -> None: ...
    def _error(self, message: str) -> None: ...


_NOOP = _NoopSpan()


def _clean(data: dict | None) -> dict | None:
    """Drop None values and cap oversized strings so traced payloads stay readable, not pathological."""
    if not data:
        return None
    out: dict = {}
    for key, value in data.items():
        if value is None:
            continue
        out[key] = _cap(value)
    return out or None


def _cap(value: object) -> object:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + f"…[+{len(value) - _MAX_FIELD_CHARS} chars]"
    if isinstance(value, list):
        return [_cap(v) for v in value]
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    return value
