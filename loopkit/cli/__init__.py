"""loopkit command line — set up a loop (init), check it (doctor), run it (run).

Thin by design: the CLI validates and renders; behaviour lives in the library modules. The surface is
split by deployment tier — `local` (the single loop + the course), `fleet` (the Redis-queue fleet),
and `cloud` (the Part III control plane + creds) — each registering onto the shared Typer apps in
`_support`. Importing this package composes the full `app`; the extension/optional-dep imports stay
function-local, so `import loopkit.cli` never pulls [fleet]/[cloud]/an SDK.
"""
from __future__ import annotations

from ._support import app
from . import batch, cloud, fleet, local  # noqa: F401 — importing registers each tier's commands on `app`

# Re-exported so they stay importable from `loopkit.cli` (tests + the init scaffolder):
from .._templates import (  # noqa: F401
    _CI_GITHUB_CLAUDE_CODE_TEMPLATE,
    _CI_GITHUB_TEMPLATE,
    _CI_GITLAB_CLAUDE_CODE_TEMPLATE,
    _CI_GITLAB_TEMPLATE,
    _CI_TEMPLATES,
    _CONFIG_TEMPLATE,
    _PROMPT_TEMPLATE,
)
from .local import _claude_code_auth_note  # noqa: F401 — imported by tests/test_adapters.py

__all__ = ["app", "main"]


def main() -> None:
    app()
