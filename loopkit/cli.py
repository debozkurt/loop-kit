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
        repo: str | None = typer.Option(None, "--repo", help="Override the target repo (config `repo`)."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Run the control flow, skip the agent."),
        max_iter: int | None = typer.Option(None, "--max-iter", help="Override stops.max_iter."),
        force: bool = typer.Option(False, "--force", help="Run even if preflight fails."),
        sandbox: bool = typer.Option(False, "--sandbox",
                                     help="Run the loop inside the loopkit Docker container (Ch 16).")) -> None:
    """Run the loop until it reaches a terminal. Point it at any repo via `repo` (or `--repo`)."""
    cfg = _load(config)
    if repo is not None:
        cfg.repo = repo
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
        f"[bold]{cfg.goal}[/]\nrepo {cfg.repo} · branch {cfg.branch} · adapter {cfg.agent.adapter} · "
        f"budget ${cfg.agent.max_cost_usd}",
        title="loopkit run"))
    result = run_loop(cfg, agent, dry_run=dry_run)
    _render(result)
    # Outward edge (Ch 16): push the solved branch + open a PR, only if [remote] is enabled.
    if not dry_run and result.reason is StopReason.DONE and cfg.remote.enabled:
        from .extensions.remote import sync_done
        sync = sync_done(cfg, cfg.repo_path(), title=f"loopkit: {cfg.goal[:60]}")
        if sync["pushed"]:
            console.print(f"[green]pushed[/] {cfg.branch} → {cfg.remote.name}")
        if sync["pr_url"]:
            console.print(f"[green]opened PR[/] {sync['pr_url']}")
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
        target: str | None = typer.Option(None, "--target", envvar="LOOPKIT_TARGET",
                                          help="Target repo path/URL to operate on (default: bundled demo-repo)."),
        gate_iteration: str | None = typer.Option(None, "--gate-iteration", help="Override the iteration gate."),
        gate_acceptance: str | None = typer.Option(None, "--gate-acceptance", help="Override the acceptance gate."),
        name: str = typer.Option("worker", "--name", envvar="WORKER_NAME",
                                 help="Worker name (rides logs as a tag; set from the pod name).")) -> None:
    """The executor: BRPOP a task, run the loop in an isolated clone of the target, HSET the result.

    Long-lived — runs as a pod or a host process. With no `--target` it runs the bundled demo-repo
    token-free (the `tilt up` smoke test). With `--target /path/or/url` it operates on YOUR repo:
    gates come from that repo's loopkit.toml unless overridden, and `[remote]` there controls
    whether a solved branch is pushed + a PR opened. Use `--adapter claude-code` for real solves.
    """
    from .extensions.fleet import RedisQueue, Worker, make_demo_runner, make_repo_runner
    queue = RedisQueue.from_url(redis_url)
    if target:
        tcfg = _maybe_target_config(target)
        iteration = gate_iteration or (tcfg.gate.iteration if tcfg else "python -m pytest -q")
        acceptance = gate_acceptance or (tcfg.gate.acceptance if tcfg and tcfg.gate.acceptance else "true")
        runner = make_repo_runner(
            target, mode="clone", adapter=adapter, max_iter=max_iter,
            gate_iteration=iteration, gate_acceptance=acceptance,
            protected_paths=tuple(tcfg.safety.protected_paths) if tcfg else ("tests/",),
            remote=tcfg.remote if tcfg else None)
        console.print(Panel.fit(
            f"worker [bold]{name}[/] · target {target} · adapter {adapter} · {redis_url}\n"
            f"gates: {iteration!r} / {acceptance!r} · remote {'on' if tcfg and tcfg.remote.enabled else 'off'}",
            title="loopkit fleet worker"))
    else:
        runner = make_demo_runner(adapter=adapter, max_iter=max_iter)
        console.print(Panel.fit(f"worker [bold]{name}[/] · demo-repo · adapter {adapter} · {redis_url}",
                                title="loopkit fleet worker"))
    try:
        Worker(queue, runner, name=name).run_forever()
    except KeyboardInterrupt:
        console.print("[yellow]worker stopped[/]")


@fleet_app.command("run")
def fleet_run(
        tasks: int = typer.Option(3, "--tasks", "-n", help="How many independent tasks to fan out."),
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL"),
        goal: str | None = typer.Option(None, "--goal", help="Override the per-task goal."),
        from_issues: bool = typer.Option(False, "--from-issues",
                                         help="Source tasks from open GitHub/GitLab issues."),
        target: str | None = typer.Option(None, "--target",
                                          help="Repo to read issues from (default: cwd). Used with --from-issues."),
        label: str | None = typer.Option(None, "--label", help="Only issues with this label become tasks."),
        provider: str = typer.Option("auto", "--provider", help="auto | github | gitlab.")) -> None:
    """Coordinator — enqueue tasks (N goals, or one per open issue) and collect a FleetResult.

    The queue decouples *what to do* (here) from *how to do it* (the workers). Make sure your
    workers were started with a matching `--target` so they operate on the same repo the issues
    came from.
    """
    from .extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url)
    if from_issues:
        from .extensions.issues import fetch_issues, issues_to_tasks
        src = Path(target or ".").expanduser()
        issues = fetch_issues(src, provider=provider, label=label)
        task_list = issues_to_tasks(issues)
        if not task_list:
            err.print(f"[yellow]no open issues found[/]"
                      f"{f' with label {label}' if label else ''} in {src}")
            raise typer.Exit(1)
        console.print(Panel.fit(
            f"enqueue [bold]{len(task_list)}[/] issue task(s) from {src}"
            f"{f' (label {label})' if label else ''} · {redis_url}",
            title="loopkit fleet run --from-issues"))
    else:
        task_list = [{"slug": f"t{i}", "branch": f"loopkit/run-t{i}", "goal": goal or PRICING_GOAL}
                     for i in range(tasks)]
        console.print(Panel.fit(f"fan out [bold]{tasks}[/] tasks · {redis_url}", title="loopkit fleet run"))
    result = Coordinator(queue).run_fleet(task_list)
    console.print(_fleet_table(result))
    raise typer.Exit(0 if result.workers and not result.failed else 2)


def _maybe_target_config(target: str) -> Config | None:
    """Load a target repo's loopkit.toml for its gates/safety/remote, or None if it has none."""
    toml = Path(target).expanduser() / "loopkit.toml"
    if not toml.exists():
        return None
    try:
        return load_config(toml)
    except Exception:                              # noqa: BLE001 — a malformed target toml -> use defaults
        return None


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
