"""Credential hygiene — keep a real key out of the prompt-injected agent's reach (Part III, Phase 5a).

loopkit runs an autonomous agent on **untrusted input** (issue bodies) while holding a **real
credential** (an API key that costs money + a git token that can push). The agent's `run_bash`, the
held-out gate, and the vendor CLI all run **in the same container as loopkit, as the same uid** — so
the only durable containment is to make sure no credential is reachable by the code paths the agent
drives. This module is that containment, and it is **core + stdlib-only** (importing it pulls nothing)
because the worker loop, the gates, and the tracer all depend on it.

Three jobs, each a project invariant:

- **Load then shred.** The per-run Secret is delivered as files on a memory-backed tmpfs (see
  `cloudrun._pod_spec`'s init container). `CredentialStore.load` reads them into process memory, then
  **`os.remove`s the files and deletes the vars from `os.environ`** — so by the time any agent code
  runs, there is no readable credential file and no credential env var. The keys live only in this
  process's heap (the irreducible residual: a same-uid `ptrace` of that heap, closed only by a
  separate-PID-namespace agent container — a later phase).
- **Scrub every untrusted-driven subprocess.** `child_env()` returns an environment with all
  credential vars stripped; only the few a *loopkit-controlled* subprocess legitimately needs (e.g.
  the git token for `git push`) are re-injected via `add=`. The agent's `run_bash` and the gate get
  the bare scrubbed env — nothing.
- **Redact by value, everywhere a payload can escape.** Tool output, trace spans, and exception
  details can carry a key the agent coaxed into view. `register_secret`/`redact` scrub the known
  values from any string before it leaves the process. This is a **best-effort backstop, not a
  boundary** (base64/hex/split defeats substring matching) — the boundary is withholding the key
  above; redaction catches the careless paths.

None-safe: with no `install()` (laptop / tests) `current()` is a no-op store that still scrubs
`os.environ` defensively and falls back to the ambient env, so single-loop runs behave exactly as
before.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Mapping

from .log import get_logger

log = get_logger("secrets")

# Known credential env-var names. The agent never needs any of these; loopkit injects the few a
# controlled subprocess needs via `child_env(add=...)`.
GIT_ENV: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")
# Adapter → the env var(s) that adapter's SDK/binary authenticates with (first present wins).
ADAPTER_KEYS: dict[str, tuple[str, ...]] = {
    "claude-code": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "claude-api": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY",),
    "openai-api": ("OPENAI_API_KEY",),
    "mock": (),
}
# The key an API adapter's SDK authenticates with — precise, NOT the projection set: a
# `CLAUDE_CODE_OAUTH_TOKEN` is valid for the claude-code CLI but NOT the Anthropic SDK, so it must
# never be handed to `anthropic.Anthropic(api_key=…)`. CLI adapters have no SDK key (they get the
# whole `ADAPTER_KEYS` set via `child_env` instead).
_SDK_KEY: dict[str, str] = {"claude-api": "ANTHROPIC_API_KEY", "openai-api": "OPENAI_API_KEY"}
_KNOWN_CREDENTIAL_ENV: frozenset[str] = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", *GIT_ENV})
# A defensive suffix sweep so a credential var we didn't enumerate is still scrubbed from children.
_CREDENTIAL_SUFFIX = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

# Smallest value worth redacting / treating as a secret — short strings cause false-positive blowups.
_MIN_SECRET_LEN = 8

# High-signal credential token shapes, for the pre-push scan (C5) and an entropy backstop. These are
# *prefixes attackers can't easily strip*, not an exhaustive list — the registry (exact values) is the
# reliable path; this catches a key the run wasn't even issued.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-fine", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("gcp", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
)


def is_credential_var(name: str) -> bool:
    """True if `name` is a credential env var (known set, or a `*_API_KEY/_TOKEN/_SECRET` suffix)."""
    if name in _KNOWN_CREDENTIAL_ENV:
        return True
    upper = name.upper()
    return any(upper.endswith(suffix) for suffix in _CREDENTIAL_SUFFIX)


# --------------------------------------------------------------------------------------------
# Redaction registry — module-global so the tracer/logger can scrub without threading a store.
# --------------------------------------------------------------------------------------------
_REGISTRY: dict[str, str] = {}            # exact secret value -> short label (for the placeholder)


def register_secret(value: str | None, *, label: str = "secret") -> None:
    """Record a secret *value* so `redact` scrubs it from any string. No-op for short/empty values."""
    if value and len(value) >= _MIN_SECRET_LEN:
        _REGISTRY[value] = label


def clear_registry() -> None:
    """Drop all registered secrets (tests; not used in the run path)."""
    _REGISTRY.clear()


def redact(text: str) -> str:
    """Replace every registered secret value in `text` with `‹redacted:label›`. Best-effort backstop.

    Longest-first so a key that contains a shorter registered fragment is replaced whole. This stops
    the *careless* paths (a key echoed verbatim into tool output / a trace / an exception); it does
    **not** stop an agent that base64s the key first — that's why the real control is withholding the
    key (see module docstring), with this as defense in depth.
    """
    if not text or not _REGISTRY:
        return text
    for value in sorted(_REGISTRY, key=len, reverse=True):
        if value in text:
            text = text.replace(value, f"‹redacted:{_REGISTRY[value]}›")
    return text


def redact_obj(obj):
    """Recursively `redact` every string inside a dict/list/tuple (for structured trace payloads)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_obj(v) for v in obj)
    return obj


def scan_for_secrets(text: str) -> list[str]:
    """Return labels of any credential-shaped tokens in `text` (registry values + known patterns).

    Used by the pre-push secret scan (a hard gate before a branch reaches the forge) and as a trace
    backstop. Registry hits are labelled `registered:<label>`; pattern hits by their kind.
    """
    if not text:
        return []
    hits: list[str] = []
    for value, label in _REGISTRY.items():
        if value in text:
            hits.append(f"registered:{label}")
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            hits.append(kind)
    return hits


# --------------------------------------------------------------------------------------------
# CredentialStore — the in-memory home for a run's keys + the env-scrubbing entry point.
# --------------------------------------------------------------------------------------------
class CredentialStore:
    """In-memory credential holder. Built by `load()` (cloud worker) or empty (laptop/tests).

    Holds the run's keys in process heap only; provides a scrubbed `child_env()` for subprocesses and
    explicit `api_key()` for the in-process SDK so nothing relies on the (now-deleted) env vars.
    """

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    # ---- construction -----------------------------------------------------------------------
    @classmethod
    def load(cls, creds_dir: str | os.PathLike[str] | None = None) -> "CredentialStore":
        """Load creds from the memory-tmpfs `creds_dir` (one file per var) else from `os.environ`,
        then **shred**: remove the tmpfs files, delete the vars from `os.environ`, register the values
        for redaction, and disable core dumps. After this returns no credential file or env var
        survives, so a later `printenv`/`cat` by agent code finds nothing.
        """
        values: dict[str, str] = {}
        directory = Path(creds_dir) if creds_dir else None
        if directory and directory.is_dir():
            for path in sorted(directory.iterdir()):
                if path.is_file():
                    try:
                        values[path.name] = path.read_text().rstrip("\n")
                    except OSError as exc:                       # unreadable file — skip, don't crash
                        log.warn("creds.read_failed", file=path.name, err=type(exc).__name__)
            _shred_dir(directory)
        else:
            for name in list(os.environ):
                if is_credential_var(name):
                    values[name] = os.environ[name]
        store = cls(values)
        store._harden()
        log.info("creds.loaded", source="tmpfs" if directory else "env", keys=len(values),
                 names=",".join(sorted(values)) or "-")
        return store

    def _harden(self) -> None:
        """Delete loaded vars from `os.environ`, register them for redaction, drop core dumps."""
        for name, value in self._values.items():
            os.environ.pop(name, None)
            register_secret(value, label=name)
        _disable_core_dumps()

    # ---- access -----------------------------------------------------------------------------
    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def api_key(self, adapter: str) -> str | None:
        """The **SDK** API key for an API adapter (`claude-api`/`openai-api`), or None — to fall back
        to the ambient env (laptop), or because the adapter is a CLI/mock one with no SDK key. Uses the
        precise `_SDK_KEY` (never an OAuth token, which the Anthropic SDK rejects)."""
        return self._values.get(_SDK_KEY.get(adapter, ""))

    def has_any(self, names: Iterable[str]) -> bool:
        return any(self._values.get(n) for n in names)

    def child_env(self, *, base: Mapping[str, str] | None = None,
                  add: Iterable[str] = ()) -> dict[str, str]:
        """A subprocess environment with **all** credential vars stripped, plus only `add` re-injected.

        `add` is the allow-list a *loopkit-controlled* subprocess needs (e.g. `GIT_ENV` for a git
        push). Each re-injected var is taken from this store first, then from the base env (so a
        laptop run, where the store is empty but the env still has the token, still works). The
        agent's `run_bash`/gate call `child_env()` with no `add` → a credential-free env.
        """
        source = os.environ if base is None else base
        env = {k: v for k, v in source.items() if not is_credential_var(k)}
        for name in add:
            value = self._values.get(name) or source.get(name)
            if value:
                env[name] = value
        return env


# --------------------------------------------------------------------------------------------
# Process-global installed store (set once at a worker/run entry point; default = empty no-op).
# --------------------------------------------------------------------------------------------
_STORE = CredentialStore()


def install(store: CredentialStore) -> None:
    """Install the process's credential store (call once, FIRST, at the worker/run entry point)."""
    global _STORE
    _STORE = store


def current() -> CredentialStore:
    """The installed store, or the empty default (which still scrubs env in `child_env`)."""
    return _STORE


# --------------------------------------------------------------------------------------------
# Internals.
# --------------------------------------------------------------------------------------------
def _shred_dir(directory: Path) -> None:
    """Best-effort: remove EVERY entry in the tmpfs creds dir (files *and* any subdir) so no readable
    copy of a key survives load — including a stray k8s `..data`/`..<timestamp>` dir if one is ever
    copied in. The mount point itself stays; only its contents are wiped."""
    import shutil
    for path in directory.iterdir():
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
        except OSError:                                          # readOnly mount / already gone
            pass


def _disable_core_dumps() -> None:
    """Set RLIMIT_CORE to 0 so a crash never dumps the in-memory key to the filesystem."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:                                           # noqa: BLE001 — non-POSIX / not permitted
        pass
