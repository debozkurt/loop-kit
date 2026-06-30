"""Shared CLI scaffolding: the composed Typer apps, the consoles, and the load/render helpers.

The command modules (`local`, `fleet`, `cloud`) import the app objects from here and register their
commands onto them; `cli/__init__` then imports those modules to trigger registration and exposes the
composed `app`. Keeping the Typer instances in this leaf module — which imports no command module — is
what avoids an import cycle.
"""
from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel

from ..config import Config, load_config
from ..loop import RunResult
from ..stops import StopReason

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="A self-governed coding loop you can point at any repository.")
fleet_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Run the loop as a queue-driven fleet of workers (Chapter 12).")
app.add_typer(fleet_app, name="fleet")
cloud_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Drive the loop on a managed Kubernetes cluster (Part III). "
                             "Pins the expected DOKS context and refuses any other.")
app.add_typer(cloud_app, name="cloud")
creds_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Register per-submitter agent/git credentials (Phase 5a). Keys are read "
                             "from the environment — never passed as an argument.")
cloud_app.add_typer(creds_app, name="creds")
console = Console()
err = Console(stderr=True)

DEFAULT_REDIS_URL = "redis://localhost:6379"

DEFAULT_CONFIG = Path("loopkit.toml")


def _load(config: Path) -> Config:
    if not config.exists():
        err.print(f"[red]no config[/] {config}  (run [bold]loopkit init[/])")
        raise typer.Exit(1)
    try:
        return load_config(config)
    except Exception as exc:                       # noqa: BLE001 — surface any validation error cleanly
        err.print(f"[red]invalid config[/] {exc}")
        raise typer.Exit(1)


def _render(result: RunResult) -> None:
    if result.reason is StopReason.DONE:
        color = "green"
    elif result.reason is StopReason.SAFETY:
        color = "red"
    else:
        color = "yellow"
    lines = [f"reason: [{color}]{result.reason.value}[/]",
             f"iterations: {result.iterations}",
             f"cost: ${result.cost_usd:.2f}"]
    if result.overfit:
        lines.append("[yellow]overfit: the iteration gate passed but the held-out gate did not[/]")
    if result.detail:
        lines.append(result.detail)
    console.print(Panel.fit("\n".join(lines), title="result"))


# --- Shared command-body helpers (the cross-cutting idioms, in one place) ---------------------

def fail(tag: str, message: str) -> NoReturn:
    """Print ``[red]tag[/] message`` to stderr and exit 1 — the CLI's uniform error-exit.

    ``message`` may contain rich markup; the caller escapes untrusted text (e.g. exception strings)
    with ``rich.markup.escape`` exactly as before. Typed ``NoReturn`` so call sites need no ``raise``.
    """
    err.print(f"[red]{tag}[/] {message}")
    raise typer.Exit(1)


def kc_str(kubeconfig: Path | None) -> str | None:
    """A ``--kubeconfig`` Path option as the ``str | None`` the extension functions expect."""
    return str(kubeconfig) if kubeconfig else None


def confirm_or_abort(prompt: str, *, yes: bool, in_cluster: bool = False) -> None:
    """Confirm a mutating action or exit cleanly. Skipped when ``--yes`` or running ``in_cluster``
    (a CronJob/webhook pod has no TTY — consent was given when the schedule/listener was created)."""
    if not yes and not in_cluster and not typer.confirm(prompt):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
