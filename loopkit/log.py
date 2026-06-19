"""Structured, greppable logging in the house style (Chapter 15 needs the run id anyway).

Every event is one line:

    <ISO8601> LEVEL [loopkit][<component>] <message> run=<id> key=value key=value ...

A short correlation id — the run id — rides on every line via a sticky field, so a single
grep reconstructs a whole run across components. Levels carry meaning: INFO = lifecycle +
each handled unit of work; DEBUG = drops/dupes/ignored noise; WARN = self-healing trouble;
ERROR = needs a human.

Never log payload content, credentials, or tokens — ids, types, lengths, and counts only
(`promptLen=42`, never `prompt=...`). Logs must always be safe to share.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, TextIO

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


class Logger:
    """A component logger with sticky fields (the run id, the component tag, the tick)."""

    def __init__(self, component: str, run_id: str = "-", level: str = "INFO",
                 stream: TextIO | None = None, sticky: dict[str, Any] | None = None) -> None:
        self.component = component
        self.run_id = run_id
        self._threshold = _LEVELS.get(level, 20)
        self._stream = stream or sys.stderr
        self._sticky = dict(sticky or {})

    def bind(self, **fields: Any) -> "Logger":
        """Return a child logger that carries extra sticky fields on every line."""
        child = Logger(self.component, self.run_id, stream=self._stream,
                       sticky={**self._sticky, **fields})
        child._threshold = self._threshold
        return child

    def _emit(self, level: str, message: str, fields: dict[str, Any]) -> None:
        if _LEVELS[level] < self._threshold:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        parts = [ts, level, f"[loopkit][{self.component}]", message, f"run={self.run_id}"]
        for key, value in {**self._sticky, **fields}.items():
            parts.append(f"{key}={_fmt(value)}")
        print(" ".join(parts), file=self._stream, flush=True)

    def debug(self, message: str, **fields: Any) -> None:
        self._emit("DEBUG", message, fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit("INFO", message, fields)

    def warn(self, message: str, **fields: Any) -> None:
        self._emit("WARN", message, fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit("ERROR", message, fields)


def _fmt(value: Any) -> str:
    """Render a field value as a single token (no spaces) so the line stays greppable."""
    text = str(value)
    return text.replace(" ", "_") if " " in text else text


def get_logger(component: str, run_id: str = "-", level: str = "INFO",
               stream: TextIO | None = None) -> Logger:
    return Logger(component, run_id=run_id, level=level, stream=stream)
