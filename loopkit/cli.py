"""loopkit command line — set up a loop (init), check it (doctor), run it (run).

Thin by design: the CLI validates and renders; all behaviour lives in the library modules so
the same loop is drivable from Python, a cron trigger, or the orchestration supervisor
(`extensions/orchestrate.py`) without going through argv. The `fleet` sub-app is the coordinator
+ worker entrypoints for the deployable fleet (Ch 12): `fleet worker` is the container entrypoint,
`fleet run`/`fleet evolve` drive the fleet over Redis. The `redis` import is deferred into those
commands, so the core CLI loads without the optional `[fleet]` dependency.
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
fleet_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Run the loop as a queue-driven fleet of workers (Chapter 12).")
app.add_typer(fleet_app, name="fleet")
console = Console()
err = Console(stderr=True)

DEFAULT_REDIS_URL = "redis://localhost:6379"

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


@fleet_app.command("worker")
def fleet_worker(
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL",
                                      help="Redis the worker drains tasks from."),
        adapter: str = typer.Option("mock", "--adapter",
                                    help="Agent the worker runs: mock (no tokens) | claude-code."),
        max_iter: int = typer.Option(6, "--max-iter", help="Per-task iteration cap."),
        name: str = typer.Option("worker", "--name", envvar="WORKER_NAME",
                                 help="Worker name (rides logs as a tag; set from the pod name).")) -> None:
    """The container entrypoint: BRPOP a task, run the loop in an isolated clone, HSET the result.

    Long-lived — this is what a worker pod runs. Ctrl-C / pod termination stops it. The default
    `mock` adapter solves the demo-repo with no tokens, so `tilt up` brings a green fleet for free.
    """
    from .extensions.fleet import RedisQueue, Worker, make_demo_runner
    queue = RedisQueue.from_url(redis_url)
    console.print(Panel.fit(f"worker [bold]{name}[/] · adapter {adapter} · {redis_url}",
                            title="loopkit fleet worker"))
    runner = make_demo_runner(adapter=adapter, max_iter=max_iter)
    try:
        Worker(queue, runner, name=name).run_forever()
    except KeyboardInterrupt:
        console.print("[yellow]worker stopped[/]")


@fleet_app.command("run")
def fleet_run(
        tasks: int = typer.Option(3, "--tasks", "-n", help="How many independent tasks to fan out."),
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL"),
        goal: str | None = typer.Option(None, "--goal", help="Override the per-task goal.")) -> None:
    """Coordinator — blind fan-out: enqueue N tasks, collect their outcomes into a FleetResult."""
    from .extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url)
    task_list = [{"slug": f"t{i}", "branch": f"loopkit/run-t{i}", "goal": goal or PRICING_GOAL}
                 for i in range(tasks)]
    console.print(Panel.fit(f"fan out [bold]{tasks}[/] tasks · {redis_url}", title="loopkit fleet run"))
    result = Coordinator(queue).run_fleet(task_list)
    console.print(_fleet_table(result))
    raise typer.Exit(0 if result.workers and not result.failed else 2)


@fleet_app.command("evolve")
def fleet_evolve(
        generations: int = typer.Option(2, "--generations", "-g"),
        population: int = typer.Option(4, "--population", "-p"),
        keep: int = typer.Option(2, "--keep", "-k"),
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL")) -> None:
    """Coordinator — evolutionary search: keep top-k, re-validate survivors, reseed the winner.

    The Ch 9 selection-inflation guard runs at fleet scale: a candidate only reseeds the next
    generation after passing a held-out gate (computed in the worker) it never competed on.
    """
    from .extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url)
    console.print(Panel.fit(
        f"evolve · {generations} gen × {population} pop, keep {keep} · {redis_url}",
        title="loopkit fleet evolve"))
    result = Coordinator(queue).evolve({"goal": PRICING_GOAL}, generations=generations,
                                       population=population, keep=keep)
    console.print(_evolution_table(result))
    winner = result.winner
    console.print(f"winner: [green]{winner.branch}[/]" if winner else "[yellow]no validated winner[/]")
    raise typer.Exit(0 if winner else 2)


def _fleet_table(fleet) -> Table:
    table = Table(title="fleet result", header_style="bold")
    table.add_column("task")
    table.add_column("branch")
    table.add_column("terminal")
    table.add_column("iters", justify="right")
    for worker in fleet.workers:
        reason = worker.result.reason.value if worker.result else (worker.error or "error")
        color = "green" if worker.done else "yellow"
        iters = str(worker.result.iterations) if worker.result else "-"
        table.add_row(str(worker.task.get("slug", worker.task.get("id", "?"))),
                      worker.branch, f"[{color}]{reason}[/]", iters)
    return table


def _evolution_table(result) -> Table:
    table = Table(title="evolution result", header_style="bold")
    table.add_column("gen", justify="right")
    table.add_column("best (score)")
    table.add_column("confirmed")
    table.add_column("inflation")
    for gen in result.generations:
        best = (f"{gen.survivors[0].branch} ({gen.survivors[0].score:.2f})"
                if gen.survivors else "-")
        confirmed = gen.confirmed.branch if gen.confirmed else "-"
        flag = "[yellow]caught[/]" if gen.inflated else "[green]clean[/]"
        table.add_row(str(gen.index), best, confirmed, flag)
    return table


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
