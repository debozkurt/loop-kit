"""loopkit command line — set up a loop (init), check it (doctor), run it (run).

Thin by design: the CLI validates and renders; all behaviour lives in the library modules so
the same loop is drivable from Python, a cron trigger, or a future supervisor without going
through argv.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import safety
from .agent import build_agent
from .config import Config, load_config
from .loop import RunResult, run_loop
from .stops import StopReason

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="A self-governed coding loop you can point at any repository.")
console = Console()
err = Console(stderr=True)

DEFAULT_CONFIG = Path("loopkit.toml")

_CONFIG_TEMPLATE = """\
# loopkit.toml — the whole loop as one object. Validate with `loopkit doctor`.
goal = "Describe exactly what 'done' means — the condition the loop drives toward."
repo = "."
branch = "loopkit/run"           # never main/master (Ch 16)

[agent]
adapter = "claude-code"          # mock | claude-code | codex
max_cost_usd = 5.0               # budget ceiling (Ch 14)

[prompt]
anchors = ["PROMPT.md"]          # fixed context reloaded each tick (Ch 4-5)

[gate]
iteration = "python -m pytest tests/seen -q"      # fast, in-sample (Ch 6-7)
acceptance = "python -m pytest tests/holdout -q"  # held-out, run once before done (Ch 9)

[stops]
max_iter = 20                    # Ch 13
no_progress_after = 3

[safety]
protected_paths = ["tests/"]     # the loop may not touch these (Ch 9 + 16)
require_clean_tree = true
allow_branches = ["loopkit/*"]
"""

_PROMPT_TEMPLATE = """\
# Task

<Describe the goal and state the spec precisely.>

The visible tests are an incomplete check — passing them is necessary but not sufficient.
Make the behaviour correct. Do not weaken, delete, or skip any test.
"""


@app.command()
def init(path: Path = typer.Argument(Path("."), help="Repository to set up.")) -> None:
    """Scaffold a starter loopkit.toml and PROMPT.md in PATH (never overwrites)."""
    path = path.expanduser().resolve()
    wrote: list[str] = []
    for name, content in (("loopkit.toml", _CONFIG_TEMPLATE), ("PROMPT.md", _PROMPT_TEMPLATE)):
        target = path / name
        if target.exists():
            err.print(f"[yellow]exists, skipped[/] {name}")
            continue
        target.write_text(content)
        wrote.append(name)
    body = "\n".join(f"[green]wrote[/] {w}" for w in wrote) or "nothing new to write"
    console.print(Panel.fit(body, title="loopkit init"))
    console.print("Next: edit the goal + gates, then [bold]loopkit doctor[/] to validate.")


@app.command()
def doctor(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c")) -> None:
    """Pre-flight checks: is this repo safe to point the loop at? (Ch 16)"""
    cfg = _load(config)
    table = Table(title="loopkit doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")

    pf = safety.preflight(cfg)
    if pf.ok:
        table.add_row("safety preflight", "[green]ok[/]", f"branch {cfg.branch}")
    else:
        for problem in pf.problems:
            table.add_row("safety preflight", "[red]fail[/]", problem)

    adapter = cfg.agent.adapter
    if adapter == "mock":
        table.add_row("agent", "[green]ok[/]", "mock (no binary needed)")
    else:
        binary = {"claude-code": "claude", "codex": "codex"}.get(adapter, adapter)
        found = shutil.which(binary)
        table.add_row("agent", "[green]ok[/]" if found else "[red]missing[/]",
                      found or f"{binary} not on PATH")

    table.add_row("iteration gate", "[green]set[/]", cfg.gate.iteration)
    if cfg.gate.acceptance:
        guarded = bool(cfg.safety.protected_paths)
        table.add_row("acceptance gate", "[green]set[/]" if guarded else "[yellow]unguarded[/]",
                      cfg.gate.acceptance)
    else:
        table.add_row("acceptance gate", "[yellow]none[/]", "no held-out check (Ch 9)")

    console.print(table)
    if not pf.ok:
        raise typer.Exit(1)


@app.command()
def run(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        dry_run: bool = typer.Option(False, "--dry-run", help="Run the control flow, skip the agent."),
        max_iter: int | None = typer.Option(None, "--max-iter", help="Override stops.max_iter."),
        force: bool = typer.Option(False, "--force", help="Run even if preflight fails.")) -> None:
    """Run the loop until it reaches a terminal."""
    cfg = _load(config)
    if max_iter is not None:
        cfg.stops.max_iter = max_iter

    pf = safety.preflight(cfg)
    if not pf.ok and not force:
        for problem in pf.problems:
            err.print(f"[red]preflight[/] {problem}")
        err.print("Fix these or pass [bold]--force[/]  (see [bold]loopkit doctor[/]).")
        raise typer.Exit(1)

    agent = build_agent(cfg.agent)
    console.print(Panel.fit(
        f"[bold]{cfg.goal}[/]\nbranch {cfg.branch} · adapter {cfg.agent.adapter} · "
        f"budget ${cfg.agent.max_cost_usd}",
        title="loopkit run"))
    result = run_loop(cfg, agent, dry_run=dry_run)
    _render(result)
    raise typer.Exit(0 if result.reason is StopReason.DONE else 2)


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


def main() -> None:
    app()
