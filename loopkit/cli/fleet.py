"""Fleet commands (Chapter 12): the coordinator + worker entrypoints over a Redis queue.

`fleet worker` is the container entrypoint (it can dispatch tool calls to a keyless executor sidecar
and read/write a skills repo); `fleet run`/`fleet evolve` are the coordinator. The `redis` import is
deferred into each command so importing the CLI doesn't require the [fleet] extra.
"""
from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from .. import secrets, trace
from ..config import Config, load_config
from ..log import get_logger
from ._support import DEFAULT_REDIS_URL, console, err, fail, fleet_app


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
        redis_namespace: str = typer.Option("loopkit", "--redis-namespace", envvar="REDIS_NAMESPACE",
                                             help="Per-run Redis keyspace ({ns}:tasks/:results). Cloud sets this to the run id."),
        executor_socket: str | None = typer.Option(
            None, "--executor-socket", envvar="LOOPKIT_EXECUTOR_SOCKET",
            help="Dispatch the agent's tool calls + the held-out gate to a keyless executor sidecar "
                 "over this Unix socket (Phase 6). Unset = run them in-process (local/dev)."),
        skills_repo: str | None = typer.Option(
            None, "--skills-repo", envvar="LOOPKIT_SKILLS_REPO",
            help="A dedicated loopkit-skills git repo: clone its lessons into each prompt and push a "
                 "gated write-back on DONE (Phase 5b cross-run flywheel). Unset = no skills."),
        skills_branch: str = typer.Option("main", "--skills-branch", envvar="LOOPKIT_SKILLS_BRANCH",
                                          help="Branch of the skills repo to read/write (default: main)."),
        name: str = typer.Option("worker", "--name", envvar="WORKER_NAME",
                                 help="Worker name (rides logs as a tag; set from the pod name).")) -> None:
    """The executor: BRPOP a task, run the loop in an isolated clone of the target, HSET the result.

    Long-lived — runs as a pod or a host process. With no `--target` it runs the bundled demo-repo
    token-free (the `tilt up` smoke test). With `--target /path/or/url` it operates on YOUR repo:
    gates come from that repo's loopkit.toml unless overridden, and the repo's remote config there
    controls whether a solved branch is pushed + a PR opened. Use `--adapter claude-code` for real
    solves.

    `--executor-socket` is the Phase-6 agent-isolation seam: when set (the cloud worker pod), the
    agent's chosen commands + the held-out gate are dispatched to the keyless `loopkit executor`
    sidecar (a different uid / PID namespace, no credential) instead of running in this key-holding
    process. Unset, they run in-process (`LocalToolExecutor`) — exact prior behavior.
    """
    # FIRST: load the per-run creds into the in-process store and shred them out of os.environ, BEFORE
    # the first git clone inherits them (Phase 5a). The Phase-6 cloud pod delivers them via envFrom into
    # *this* (trusted) loopkit-core container — the untrusted run_bash/gate run in the keyless executor
    # sidecar, so there's nothing to shred there. A tmpfs dir is still honored if set (back-compat).
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    # Fail-closed (G7): in a cloud pod (LOOPKIT_CREDS_EXPECTED is set), an API adapter with no resolved
    # key is a misconfiguration — stop with a clear, attributable error rather than 401-ing deep in a tick.
    if (os.environ.get("LOOPKIT_CREDS_EXPECTED") and adapter in ("claude-api", "openai-api")
            and secrets.current().api_key(adapter) is None):
        fail("worker", f"no credential for adapter '{adapter}' — the per-run Secret had no "
                       f"{'/'.join(secrets.ADAPTER_KEYS.get(adapter, ()))}. Register: loopkit cloud creds set.")
    from ..extensions.fleet import RedisQueue, Worker, make_demo_runner, make_repo_runner
    trace.configure(None)                 # auto-on from env; each worker traces its own runs (Ch 12)
    queue = RedisQueue.from_url(redis_url, namespace=redis_namespace)
    # Phase 6: when a socket is configured, the agent's tool calls + the held-out gate run in the
    # keyless executor sidecar; loopkit-core (here) keeps the key for the LLM call + git. None = local.
    tool_executor = None
    if executor_socket:
        from ..executor import RemoteToolExecutor
        # Group-writable umask so the executor sidecar (a different uid, same fsGroup) can edit files
        # loopkit-core clones — and loopkit-core can commit files the executor wrote (Phase 6).
        os.umask(0o002)
        tool_executor = RemoteToolExecutor(executor_socket)
        get_logger("worker").bind(task=name).info("executor.remote", socket=executor_socket)
    if target:
        tcfg = _maybe_target_config(target)
        if tcfg is not None:
            trace.configure(tcfg.trace)   # honor the target repo's [trace] project/toggle
        iteration = gate_iteration or (tcfg.gate.iteration if tcfg else "python -m pytest -q")
        acceptance = gate_acceptance or (tcfg.gate.acceptance if tcfg and tcfg.gate.acceptance else "true")
        runner = make_repo_runner(
            target, mode="clone", adapter=adapter, max_iter=max_iter,
            gate_iteration=iteration, gate_acceptance=acceptance,
            protected_paths=tuple(tcfg.safety.protected_paths) if tcfg else ("tests/",),
            remote=tcfg.remote if tcfg else None, executor=tool_executor,
            skills_repo=skills_repo, skills_branch=skills_branch)
        console.print(Panel.fit(
            f"worker [bold]{name}[/] · target {target} · adapter {adapter} · {redis_url}\n"
            f"gates: {iteration!r} / {acceptance!r} · remote {'on' if tcfg and tcfg.remote.enabled else 'off'}"
            + (f" · executor {executor_socket}" if executor_socket else "")
            + (f" · skills {skills_repo}" if skills_repo else ""),
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
        provider: str = typer.Option("auto", "--provider", help="auto | github | gitlab."),
        redis_namespace: str = typer.Option("loopkit", "--redis-namespace", envvar="REDIS_NAMESPACE",
                                             help="Per-run Redis keyspace. Cloud sets this to the run id."),
        drain_workers: int | None = typer.Option(None, "--drain-workers",
                                                  help="On completion, enqueue N sentinels so N ephemeral worker pods exit (cloud).")) -> None:
    """Coordinator — enqueue tasks (N goals, or one per open issue) and collect a FleetResult.

    The queue decouples *what to do* (here) from *how to do it* (the workers). Make sure your
    workers were started with a matching `--target` so they operate on the same repo the issues
    came from.
    """
    # FIRST: the coordinator pod gets git-only creds via the memory-tmpfs mount (Phase 5a) — load +
    # shred them so `--from-issues` (`gh issue list`) can authenticate; without this the token (now a
    # file, not an env var) is invisible to the scrubbed subprocess env.
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from ..extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url, namespace=redis_namespace)
    if from_issues:
        from ..extensions.issues import fetch_issues, issues_to_tasks
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
    result = Coordinator(queue).run_fleet(task_list, drain_workers=drain_workers)
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
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL"),
        redis_namespace: str = typer.Option("loopkit", "--redis-namespace", envvar="REDIS_NAMESPACE",
                                             help="Per-run Redis keyspace. Cloud sets this to the run id."),
        drain_workers: int | None = typer.Option(None, "--drain-workers",
                                                  help="After the final generation, enqueue N sentinels so N worker pods exit (cloud).")) -> None:
    """Coordinator — evolutionary search: keep top-k, re-validate survivors, reseed the winner.

    The Ch 9 selection-inflation guard runs at fleet scale: a candidate only reseeds the next
    generation after passing a held-out gate (computed in the worker) it never competed on.
    """
    # FIRST: load + shred any tmpfs-mounted creds before doing work (uniform with the other entrypoints).
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from ..extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url, namespace=redis_namespace)
    console.print(Panel.fit(
        f"evolve · {generations} gen × {population} pop, keep {keep} · {redis_url}",
        title="loopkit fleet evolve"))
    result = Coordinator(queue).evolve({"goal": PRICING_GOAL}, generations=generations,
                                       population=population, keep=keep, drain_workers=drain_workers)
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

