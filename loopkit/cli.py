"""loopkit command line — set up a loop (init), check it (doctor), run it (run).

Thin by design: the CLI validates and renders; all behaviour lives in the library modules so
the same loop is drivable from Python, a cron trigger, or a future supervisor without going
through argv.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import safety, scenarios
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
        force: bool = typer.Option(False, "--force", help="Run even if preflight fails."),
        sandbox: bool = typer.Option(False, "--sandbox",
                                     help="Run the loop inside the loopkit Docker container (Ch 16).")) -> None:
    """Run the loop until it reaches a terminal."""
    cfg = _load(config)
    if max_iter is not None:
        cfg.stops.max_iter = max_iter

    if sandbox:
        _run_sandboxed(cfg, config, dry_run=dry_run, max_iter=max_iter, force=force)
        return

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


@app.command()
def demo(chapter: int | None = typer.Argument(None, help="Course chapter number, e.g. 9. Omit to list."),
         live: bool = typer.Option(False, "--live", help="Use the real claude-code agent where supported.")) -> None:
    """Run a chapter's scenario straight through (the loop's logs are the play-by-play)."""
    if chapter is None:
        _list_scenarios()
        return
    scenarios.play(chapter, console, live=live, pause=False)


@app.command()
def learn(chapter: int | None = typer.Argument(None, help="Course chapter number. Omit to list."),
          live: bool = typer.Option(False, "--live", help="Use the real claude-code agent where supported.")) -> None:
    """Walk a chapter's scenario with narration and a pause between beats."""
    if chapter is None:
        _list_scenarios()
        return
    scenarios.play(chapter, console, live=live, pause=True)


def _list_scenarios() -> None:
    table = Table(title="loopkit scenarios", header_style="bold")
    table.add_column("chapter")
    table.add_column("topic")
    table.add_column("teaches", overflow="fold")
    table.add_column("mode")
    for scenario in scenarios.available():
        table.add_row(str(scenario.chapter), scenario.title, scenario.teaches,
                      "live or scripted" if scenario.live_supported else "scripted")
    console.print(table)
    console.print("Run one with [bold]loopkit demo <chapter>[/] or [bold]loopkit learn <chapter>[/].")


def _run_sandboxed(cfg: Config, config_path: Path, *, dry_run: bool, max_iter: int | None,
                   force: bool) -> None:
    """Re-invoke `loopkit run` inside the container, with the repo bind-mounted at /work (Ch 16)."""
    if shutil.which("docker") is None:
        err.print("[red]sandbox[/] docker not found on PATH (build the image: docker build -t loopkit .)")
        raise typer.Exit(1)
    repo = cfg.repo_path()
    inner = ["loopkit", "run", "-c", config_path.name]
    if dry_run:
        inner.append("--dry-run")
    if max_iter is not None:
        inner += ["--max-iter", str(max_iter)]
    if force:
        inner.append("--force")
    cmd = ["docker", "run", "--rm", "-v", f"{repo}:/work", "-w", "/work", "loopkit", *inner[1:]]
    console.print(Panel.fit(" ".join(cmd), title="loopkit run --sandbox"))
    raise typer.Exit(subprocess.call(cmd))


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
