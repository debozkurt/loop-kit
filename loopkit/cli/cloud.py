"""Cloud control-plane commands (Part III): the `cloud` sub-app + the nested `creds` sub-app.

Every mutating command runs the context-safety guard first (it refuses any context but the pinned one)
and confirms before touching a paid cluster. That guard is **structural**: every command is registered
through `@guarded_command`, which maps any `cloud.ContextError` — from the upfront `guard_context`
check OR the library's re-check on every mutation — to a uniform `[red]refused[/]` exit, so a command
physically cannot let a guard refusal escape as a traceback or skip the refusal path.

The `kubernetes` client + the cloud extension modules are imported function-locally, so importing the
CLI never requires the [cloud] extra.
"""
from __future__ import annotations

import functools
import importlib.util
import os
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .. import secrets
from ._support import cloud_app, confirm_or_abort, console, creds_app, err, fail, kc_str


# `kubernetes` is the [cloud] extra. The cloud extension module imports it lazily, but the *read*
# commands here still need it present to talk to a cluster — so guard with a clear install hint
# rather than letting an ImportError surface raw.
def _require_cloud_extra() -> None:
    if importlib.util.find_spec("kubernetes") is None:
        fail("cloud", r"the kubernetes client is not installed (pip install 'loopkit\[cloud]').")


def guarded_command(name: str, *, on: typer.Typer = cloud_app):
    """Register a cloud command and map any context-guard refusal in its body to a uniform
    `[red]refused[/]` exit. This is what makes the Ch 16 context guard structural: a `ContextError`
    can never escape as a traceback, and the refusal path can't be forgotten when a command is added.
    """
    def decorate(func):
        @functools.wraps(func)                       # carries the signature + annotations Typer reads
        def wrapper(*args, **kwargs):
            from ..extensions import cloud
            try:
                return func(*args, **kwargs)
            except cloud.ContextError as exc:
                fail("refused", escape(str(exc)))
        return on.command(name)(wrapper)
    return decorate


def guard_context(kubeconfig: str | Path | None, context: str | None, *, in_cluster: bool = False) -> str:
    """Run the context-safety guard and return the pinned current context.

    Raises `cloud.ContextError` on refusal, which `@guarded_command` renders as a clean
    `[red]refused[/]` exit — so a mutating command guards with a single line and no try/except.
    """
    from ..extensions import cloud
    return cloud.check_context(cloud.current_context(kubeconfig, in_cluster=in_cluster), context)


@guarded_command("context")
def cloud_context(
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT",
                                            help="Expected cluster context to pin (allowlist; comma-separated)."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG",
                                                help="Kubeconfig file (default: KUBECONFIG / ~/.kube/config).")) -> None:
    """Show the active kube context and whether the guard would allow a mutating command (read-only).

    Safe to run against any cluster — it only *reads* the current context. Use it before `bootstrap`
    to confirm you're pointed at the intended DOKS cluster.
    """
    _require_cloud_extra()
    from ..extensions import cloud
    current = cloud.current_context(kubeconfig)
    expected = cloud.resolve_expected(context)
    try:
        cloud.check_context(current, context)
        status, detail = "[green]allowed[/]", f"context {current} is pinned"
    except cloud.ContextError as exc:
        status, detail = "[red]refused[/]", str(exc)
    table = Table(title="loopkit cloud context", header_style="bold")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("current context", escape(str(current or "—")))
    table.add_row("expected (pinned)", escape(", ".join(expected) or "— (nothing pinned)"))
    table.add_row("guard", f"{status} {escape(detail)}")
    console.print(table)


@guarded_command("doctor")
def cloud_doctor(
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT",
                                            help="Expected cluster context to pin (allowlist; comma-separated)."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """Pre-flight the cloud control plane: extra installed, kubeconfig readable, context pinned + matching."""
    table = Table(title="loopkit cloud doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")

    have_extra = importlib.util.find_spec("kubernetes") is not None
    table.add_row("kubernetes client", "[green]ok[/]" if have_extra else "[red]missing[/]",
                  r"loopkit\[cloud] installed" if have_extra else r"pip install 'loopkit\[cloud]'")
    if not have_extra:
        console.print(table)
        raise typer.Exit(1)

    from ..extensions import cloud
    expected = cloud.resolve_expected(context)
    table.add_row("expected context", "[green]pinned[/]" if expected else "[red]unpinned[/]",
                  ", ".join(expected) or f"set --context or ${cloud.ENV_CONTEXT} (fail-closed)")
    try:
        current = cloud.current_context(kubeconfig)
        table.add_row("kubeconfig", "[green]ok[/]", f"active context {current or '—'}")
    except cloud.ContextError as exc:
        table.add_row("kubeconfig", "[red]fail[/]", escape(str(exc)))
        console.print(table)
        raise typer.Exit(1)
    try:
        cloud.check_context(current, context)
        table.add_row("context guard", "[green]ok[/]", f"{current} is pinned — mutations allowed")
        ok = True
    except cloud.ContextError as exc:
        table.add_row("context guard", "[red]refused[/]", escape(str(exc)))
        ok = False

    manifests = sorted(p.name for p in cloud.DEFAULT_MANIFEST_DIR.glob("*.yaml"))
    table.add_row("system manifests", "[green]ok[/]" if manifests else "[yellow]none[/]",
                  f"{len(manifests)} in {cloud.DEFAULT_MANIFEST_DIR.name}/: {', '.join(manifests)}")
    if ok:
        # Per-submitter creds (Phase 5a): the most common misconfig is no `fleet` default → every
        # unregistered run silently runs creds-less. Read-only; tolerate an offline failure.
        try:
            from ..extensions import creds as credmod
            regs = credmod.list_credentials(kubeconfig=kc_str(kubeconfig))
            fleet = any(r.submitter == "fleet" for r in regs)
            table.add_row("credentials", "[green]ok[/]" if regs else "[yellow]none[/]",
                          f"{len(regs)} registered · fleet default {'present' if fleet else 'MISSING'}")
        except Exception as exc:   # noqa: BLE001 — a read failure must not break doctor
            table.add_row("credentials", "[yellow]?[/]", f"could not list ({type(exc).__name__})")
    console.print(table)
    raise typer.Exit(0 if ok else 1)


@guarded_command("bootstrap")
def cloud_bootstrap(
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT",
                                            help="Expected cluster context to pin (allowlist; comma-separated)."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """One-time: apply ns/loopkit-system (Redis, RBAC, NetworkPolicy) — guarded by the context pin.

    The context-safety guard runs first and refuses any context but the pinned one, so this can never
    mutate the wrong cluster. Idempotent: re-running converges (already-present objects are skipped).
    """
    _require_cloud_extra()
    from ..extensions import cloud
    # Show the target + guard verdict before doing anything; mutating a cloud cluster needs intent.
    current = guard_context(kubeconfig, context)
    console.print(Panel.fit(
        f"apply [bold]ns/loopkit-system[/] (Redis · RBAC · NetworkPolicy)\n"
        f"context [bold]{current}[/] · manifests {cloud.DEFAULT_MANIFEST_DIR}",
        title="loopkit cloud bootstrap"))
    confirm_or_abort(f"Apply system manifests to '{current}'?", yes=yes)
    result = cloud.bootstrap(expected=context, kubeconfig=kc_str(kubeconfig))
    console.print(f"[green]bootstrapped[/] {result.context} · applied {len(result.applied)} manifest(s): "
                  f"{', '.join(result.applied)}")


def _run_creds_from_env() -> dict[str, str]:
    """Collect the agent + git credentials present in the environment (the `--from-env` escape hatch).

    Transitional: a one-off run from a machine that already has keys exported, or before any engineer
    has registered. The default `cloud run` path resolves per-submitter Secrets instead (`--as`). Not
    available on the webhook/cron paths — those are multi-tenant.
    """
    creds: dict[str, str] = {}
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            creds[var] = os.environ[var]
    return creds


def _resolve_submitter(explicit: str | None) -> str:
    """Who a run is for: explicit `--as` → `$LOOPKIT_SUBMITTER` → the `fleet` default."""
    return explicit or os.environ.get("LOOPKIT_SUBMITTER") or "fleet"


def _resolve_run_creds(spec, *, from_env: bool, allow_fleet_fallback: bool, in_cluster: bool,
                       yes: bool, kubeconfig: str | None) -> tuple[dict[str, str], str]:
    """Resolve a run's creds per the Phase-5a policy. Returns (projected creds, source). Exits on refusal.

    mock → none; `--from-env` → the env grab (projected); else the per-submitter resolver with the
    fail-closed fallback policy: the submitter's own key wins; absent it, the shared `fleet` key is
    used only if `--allow-fleet-fallback` (non-interactive) or the operator confirms (interactive);
    otherwise the run is refused (no key is silently borrowed).
    """
    from ..extensions import creds as credmod
    if spec.adapter == "mock":
        return {}, "mock"
    if from_env:
        return credmod.project(_run_creds_from_env(), spec.adapter), "from-env"
    ident = credmod.Identity(spec.submitter, spec.env_name, spec.adapter)
    rc = credmod.resolve_for_run(ident, allow_fleet_fallback=False, kubeconfig=kubeconfig,
                                 in_cluster=in_cluster)
    if rc.source == "submitter":
        return rc.data, rc.source
    fleet = credmod.resolve_for_run(ident, allow_fleet_fallback=True, kubeconfig=kubeconfig,
                                    in_cluster=in_cluster)
    interactive = not in_cluster and not yes
    if fleet.source == "fleet":
        if allow_fleet_fallback or (interactive and typer.confirm(
                f"No key registered for '{spec.submitter}'. Use the shared 'fleet' key? "
                "Its budget is shared and a leak isn't attributable to you.")):
            err.print(f"[yellow]using the shared 'fleet' key[/] for '{spec.submitter}' (not attributable)")
            return fleet.data, "fleet-fallback"
        fail("run", f"no key for '{spec.submitter}' and fleet fallback not permitted "
                    "(pass --allow-fleet-fallback, or register: loopkit cloud creds set --as <you>).")
    fail("run", f"no credentials for submitter '{spec.submitter}' and no fleet default. "
                "Register one: loopkit cloud creds set --as <you> --adapter <adapter>.")


@guarded_command("run")
def cloud_run(
        target: str = typer.Option(..., "--target", help="Repo URL/path the workers clone + operate on."),
        goal: str | None = typer.Option(None, "--goal", help="The per-task goal (one of --goal | --from-issues)."),
        from_issues: bool = typer.Option(False, "--from-issues", help="Source tasks from the target's open issues."),
        label: str | None = typer.Option(None, "--label", help="Only issues with this label (with --from-issues)."),
        provider: str = typer.Option("auto", "--provider", help="Issue forge: auto | github | gitlab (with --from-issues)."),
        workers: int = typer.Option(1, "--workers", "-w", help="Worker pod parallelism (fan-out width)."),
        adapter: str = typer.Option("claude-code", "--adapter", help="mock | claude-code | claude-api | codex | openai-api."),
        evolve: bool = typer.Option(False, "--evolve", help="Generational search instead of blind fan-out."),
        generations: int = typer.Option(2, "--generations", "-g"),
        population: int = typer.Option(4, "--population", "-p"),
        keep: int = typer.Option(2, "--keep", "-k"),
        image: str | None = typer.Option(None, "--image", envvar="LOOPKIT_WORKER_IMAGE",
                                         help="Worker image (ghcr.io/<owner>/loopkit-worker:<tag>)."),
        skills_repo: str | None = typer.Option(None, "--skills-repo", envvar="LOOPKIT_SKILLS_REPO",
                                               help="loopkit-skills git repo for the cross-run flywheel (Phase 5b)."),
        skills_branch: str = typer.Option("main", "--skills-branch",
                                          help="Branch of the skills repo to read/write."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag (selects per-submitter creds)."),
        as_submitter: str | None = typer.Option(None, "--as", help="Submitter whose registered key this run spends."),
        from_env: bool = typer.Option(False, "--from-env",
                                      help="Transitional: take creds from THIS environment (not the resolver)."),
        allow_fleet_fallback: bool = typer.Option(False, "--allow-fleet-fallback",
                                                  help="Permit the shared 'fleet' key when the submitter has none."),
        name: str | None = typer.Option(None, "--name", help="Run id (default: a generated short id)."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        in_cluster: bool = typer.Option(False, "--in-cluster",
                                        help="Authenticate via the pod's ServiceAccount (the CronJob/webhook path)."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Start a run: build ns/run-<id> + coordinator/worker Jobs and apply them (guarded).

    The context-safety guard runs first, so a run can never land on the wrong cluster. One of
    --goal | --from-issues is required (unless --evolve, which uses the built-in pricing goal).
    `--in-cluster` is how the CronJob (and any in-pod caller) submits — it skips the kubeconfig and
    the interactive confirm and uses the pod's ServiceAccount (the guard pins `in-cluster`).
    """
    _require_cloud_extra()
    import uuid
    from ..extensions import cloudrun
    if not image:
        fail("run", "no worker image — pass --image or set $LOOPKIT_WORKER_IMAGE "
                    "(ghcr.io/<owner>/loopkit-worker:<tag>).")
    if not evolve and not goal and not from_issues:
        fail("run", "need one of --goal, --from-issues, or --evolve.")
    run_id = name or uuid.uuid4().hex[:8]
    submitter = _resolve_submitter(as_submitter)
    try:
        spec = cloudrun.RunSpec(
            run_id=run_id, image=image, target=target, workers=workers, adapter=adapter,
            goal=goal, from_issues=from_issues, label=label, provider=provider,
            mode="evolve" if evolve else "fanout",
            generations=generations, population=population, keep=keep, env_name=env_name,
            submitter=submitter, skills_repo=skills_repo, skills_branch=skills_branch)
    except ValueError as exc:
        fail("run", escape(str(exc)))
    kc = kc_str(kubeconfig)
    # Show the plan + guard verdict before mutating a (paid) cloud cluster.
    current = guard_context(kc, context, in_cluster=in_cluster)
    work = f"{population}×{generations} evolve" if evolve else (
        "issues" if from_issues else f"goal ×{spec.parallelism}")
    console.print(Panel.fit(
        f"run [bold]{spec.run_id}[/] → ns/{spec.namespace}\n"
        f"target {target} · {work} · adapter {adapter} · {spec.parallelism} worker(s)\n"
        f"submitter [bold]{submitter}[/] · context [bold]{current}[/] · image {image}",
        title="loopkit cloud run"))
    # In-cluster (cron/webhook) is non-interactive: there's no TTY to confirm at, so --in-cluster
    # implies --yes (the human already consented when they created the schedule, guarded).
    confirm_or_abort(f"Start run '{spec.run_id}' on '{current}'?", yes=yes, in_cluster=in_cluster)
    # Resolve the submitter's key (fail-closed fallback policy) BEFORE creating the run.
    creds, source = _resolve_run_creds(spec, from_env=from_env, allow_fleet_fallback=allow_fleet_fallback,
                                       in_cluster=in_cluster, yes=yes, kubeconfig=kc)
    if source != "mock":
        spec.extra_labels["loopkit.dev/creds"] = source     # attribution: submitter | fleet-fallback | from-env
    namespace = cloudrun.create_run(spec, expected=context, kubeconfig=kc,
                                    in_cluster=in_cluster, creds=creds)
    console.print(f"[green]started[/] run {spec.run_id} in ns/{namespace} "
                  f"(creds: {source}) · `loopkit cloud status {spec.run_id}`")


@guarded_command("ls")
def cloud_ls(
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """List runs across run-* namespaces with their phase + worker counts (read-only)."""
    _require_cloud_extra()
    from ..extensions import cloudrun
    runs = cloudrun.list_runs(kubeconfig=kc_str(kubeconfig))
    if not runs:
        console.print("[dim]no runs[/]")
        return
    table = Table(title="loopkit cloud runs", header_style="bold")
    for col in ("run", "namespace", "phase", "active", "ok", "failed"):
        table.add_column(col, justify="right" if col in ("active", "ok", "failed") else "left")
    for r in runs:
        color = {"complete": "green", "failed": "red", "running": "cyan"}.get(r.phase, "yellow")
        table.add_row(r.run_id, r.namespace, f"[{color}]{r.phase}[/]",
                      str(r.workers_active), str(r.workers_succeeded), str(r.workers_failed))
    console.print(table)


@guarded_command("status")
def cloud_status(
        run: str = typer.Argument(..., help="Run id."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """Show one run's phase + worker counts (read-only)."""
    _require_cloud_extra()
    from ..extensions import cloudrun
    summary = cloudrun.run_status(run, kubeconfig=kc_str(kubeconfig))
    if summary is None:
        err.print(f"[yellow]no such run[/] {run} (namespace gone — GC'd or never created)")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"run [bold]{summary.run_id}[/] · ns/{summary.namespace}\n"
        f"phase {summary.phase} · workers active {summary.workers_active} / "
        f"ok {summary.workers_succeeded} / failed {summary.workers_failed}",
        title="loopkit cloud status"))


@guarded_command("logs")
def cloud_logs(
        run: str = typer.Argument(..., help="Run id."),
        role: str = typer.Option("worker", "--role", help="worker | coordinator."),
        tail: int | None = typer.Option(None, "--tail", help="Tail only the last N lines per pod."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """Print a run's pod logs (read-only; kubectl-logs under the hood)."""
    _require_cloud_extra()
    from ..extensions import cloudrun
    out = cloudrun.run_logs(run, role=role, tail_lines=tail, kubeconfig=kc_str(kubeconfig))
    console.print(escape(out))


@guarded_command("kill")
def cloud_kill(
        run: str = typer.Argument(..., help="Run id."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Delete a run's namespace (and everything in it) — guarded by the context pin."""
    _require_cloud_extra()
    from ..extensions import cloudrun
    current = guard_context(kubeconfig, context)
    confirm_or_abort(f"Delete run '{run}' (ns/run-{run}) on '{current}'?", yes=yes)
    namespace = cloudrun.delete_run(run, expected=context, kubeconfig=kc_str(kubeconfig))
    console.print(f"[green]killed[/] run {run} (deleted ns/{namespace})")


@guarded_command("schedule")
def cloud_schedule(
        name: str = typer.Argument(..., help="Schedule name (becomes the CronJob name)."),
        target: str = typer.Option(..., "--target", help="Repo the scheduled run operates on."),
        cron: str = typer.Option(..., "--cron", help="Crontab schedule, e.g. \"0 9 * * *\"."),
        from_issues: bool = typer.Option(False, "--from-issues", help="Each firing sweeps open issues."),
        goal: str | None = typer.Option(None, "--goal", help="Fixed recurring goal (instead of --from-issues)."),
        label: str | None = typer.Option(None, "--label", help="Only issues with this label (with --from-issues)."),
        provider: str = typer.Option("auto", "--provider", help="Issue forge: auto | github | gitlab (with --from-issues)."),
        workers: int = typer.Option(1, "--workers", "-w", help="Worker pod parallelism per firing."),
        adapter: str = typer.Option("claude-api", "--adapter", help="API adapter only on this untrusted path (claude-api | openai-api)."),
        image: str | None = typer.Option(None, "--image", envvar="LOOPKIT_WORKER_IMAGE",
                                         help="Worker image (ghcr.io/<owner>/loopkit-worker:<tag>)."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        as_submitter: str | None = typer.Option(None, "--as", help="Submitter whose key each firing spends."),
        allow_fleet_fallback: bool = typer.Option(False, "--allow-fleet-fallback",
                                                  help="Permit the shared 'fleet' key (cron is operator-authored)."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Create a CronJob that fires `loopkit cloud run --in-cluster` on a schedule (guarded).

    The CronJob runs as the loopkit-control SA and reuses the exact `cloud run` path — one of
    --from-issues | --goal is required. It carries no static credentials; each firing resolves the
    `--as` submitter's key in-cluster. The context guard runs first, so a schedule can never be
    created on the wrong cluster.
    """
    _require_cloud_extra()
    from ..extensions import triggers
    if not image:
        fail("schedule", "no worker image — pass --image or set $LOOPKIT_WORKER_IMAGE.")
    try:
        spec = triggers.ScheduleSpec(
            name=name, schedule=cron, target=target, image=image, from_issues=from_issues,
            goal=goal, label=label, provider=provider, adapter=adapter, workers=workers,
            env_name=env_name, submitter=_resolve_submitter(as_submitter),
            allow_fleet_fallback=allow_fleet_fallback)
    except ValueError as exc:
        fail("schedule", escape(str(exc)))
    current = guard_context(kubeconfig, context)
    work = "issues" + (f" (label {label})" if label else "") if from_issues else "fixed goal"
    console.print(Panel.fit(
        f"schedule [bold]{spec.name}[/] · cron \"{cron}\" → loopkit-system\n"
        f"target {target} · {work} · adapter {adapter} · {workers} worker(s)\n"
        f"context [bold]{current}[/] · image {image}",
        title="loopkit cloud schedule"))
    confirm_or_abort(f"Create schedule '{spec.name}' on '{current}'?", yes=yes)
    created = triggers.create_schedule(spec, expected=context, kubeconfig=kc_str(kubeconfig))
    console.print(f"[green]scheduled[/] {created} (\"{cron}\") · `loopkit cloud schedules`")


@guarded_command("schedules")
def cloud_schedules(
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """List loopkit CronJobs in loopkit-system (read-only)."""
    _require_cloud_extra()
    from ..extensions import triggers
    schedules = triggers.list_schedules(kubeconfig=kc_str(kubeconfig))
    if not schedules:
        console.print("[dim]no schedules[/]")
        return
    table = Table(title="loopkit cloud schedules", header_style="bold")
    for col in ("name", "cron", "suspended", "last run"):
        table.add_column(col)
    for s in schedules:
        table.add_row(s.name, s.schedule, "[yellow]yes[/]" if s.suspended else "no",
                      s.last_run or "—")
    console.print(table)


@guarded_command("unschedule")
def cloud_unschedule(
        name: str = typer.Argument(..., help="Schedule name to delete."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Delete a CronJob by name — guarded by the context pin."""
    _require_cloud_extra()
    from ..extensions import triggers
    current = guard_context(kubeconfig, context)
    confirm_or_abort(f"Delete schedule '{name}' on '{current}'?", yes=yes)
    removed = triggers.delete_schedule(name, expected=context, kubeconfig=kc_str(kubeconfig))
    console.print(f"[green]unscheduled[/] {removed}")


@guarded_command("webhook")
def cloud_webhook(
        image: str | None = typer.Option(None, "--image", envvar="LOOPKIT_WORKER_IMAGE",
                                         help="Worker image for the runs this listener starts."),
        secret: str | None = typer.Option(None, "--secret", envvar="LOOPKIT_WEBHOOK_SECRET",
                                          help="Webhook secret — GitHub HMAC or GitLab token (fail-closed: required)."),
        provider: str = typer.Option("github", "--provider", envvar="LOOPKIT_WEBHOOK_PROVIDER",
                                     help="Which forge this listener serves: github | gitlab."),
        label: str | None = typer.Option(None, "--label", envvar="LOOPKIT_TRIGGER_LABEL",
                                         help="Only issues bearing this label dispatch a run."),
        adapter: str = typer.Option("claude-api", "--adapter",
                                    help="API adapter only on this untrusted path (claude-api | openai-api)."),
        as_submitter: str | None = typer.Option(None, "--as", envvar="LOOPKIT_SUBMITTER",
                                                help="Pinned submitter identity (required for GitLab; a fallback for GitHub)."),
        allow_fleet_fallback: bool = typer.Option(False, "--allow-fleet-fallback",
                                                  help="Permit the shared 'fleet' key for an unregistered submitter."),
        workers: int = typer.Option(1, "--workers", "-w", help="Worker parallelism per triggered run."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        host: str = typer.Option("0.0.0.0", "--host"),
        port: int = typer.Option(8080, "--port"),
        redis_url: str | None = typer.Option(None, "--redis-url", envvar="REDIS_URL",
                                             help="Shared idempotency store (needed if replicas > 1)."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT")) -> None:
    """Serve the GitHub/GitLab webhook listener: signed issue events → one guarded in-cluster run.

    Runs in-cluster (the pod's ServiceAccount). Fail-closed: refuses to start without a secret or with
    a CLI adapter; every delivery is authenticated, the submitter (issue author / pinned identity) is
    resolved to a registered key BEFORE the dedupe reservation, and one run per issue. One listener
    serves one forge — pick it with --provider.
    """
    _require_cloud_extra()
    from ..extensions import cloudrun
    from ..extensions import creds as credmod
    from ..extensions import triggers
    if not secret:
        fail("webhook", "no secret — set --secret or $LOOPKIT_WEBHOOK_SECRET "
                        "(refusing to serve an unauthenticated endpoint).")
    if not image:
        fail("webhook", "no worker image — pass --image or set $LOOPKIT_WORKER_IMAGE.")
    if adapter in triggers.CLI_ADAPTERS:
        fail("webhook", f"adapter '{adapter}' is refused on the untrusted webhook path "
                        "(a CLI adapter holds the key in its own loop). Use --adapter claude-api.")
    try:
        forge = triggers.provider_for(provider)
    except ValueError as exc:
        fail("webhook", escape(str(exc)))
    if forge.name == "gitlab" and not as_submitter:
        fail("webhook", "GitLab requires a pinned identity (--as <submitter>): its token "
                        "isn't bound to the body, so the payload's author is not trusted.")
    # The listener submits in-cluster; verify the guard would allow it before binding the socket.
    guard_context(None, context, in_cluster=True)

    def resolve(spec: cloudrun.RunSpec):
        return credmod.resolve_for_run(
            credmod.Identity(spec.submitter, spec.env_name, spec.adapter),
            allow_fleet_fallback=allow_fleet_fallback, in_cluster=True)

    def start(spec: cloudrun.RunSpec, creds_data: dict) -> str:
        try:
            return cloudrun.create_run(spec, expected=context, in_cluster=True, creds=creds_data)
        finally:
            creds_data.clear()           # zero the tenant's key out of the listener heap after use (G13)

    store = (triggers.RedisIdempotencyStore.from_url(redis_url) if redis_url
             else triggers.InMemoryIdempotencyStore())
    app_obj = triggers.WebhookApp(
        secret=secret, image=image, create=start, resolve=resolve, store=store, provider=forge,
        adapter=adapter, workers=workers, env_name=env_name, trigger_label=label,
        listener_submitter=as_submitter)
    server = triggers.serve(app_obj, host=host, port=port)
    console.print(Panel.fit(
        f"listening on [bold]{host}:{port}[/] · forge {forge.name} · adapter {adapter} · "
        f"label {label or '—'} · dedupe {'redis' if redis_url else 'in-memory'}",
        title="loopkit cloud webhook"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("[yellow]webhook listener stopped[/]")
        server.shutdown()


@guarded_command("set", on=creds_app)
def creds_set(
        as_submitter: str = typer.Option(..., "--as", help="The engineer/identity to register."),
        adapter: str = typer.Option("claude-code", "--adapter",
                                    help="Which adapter's key to read from the environment."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Register a submitter's keys (read from THIS environment) into a Secret in loopkit-system.

    Reads the adapter's key + git token from the environment — NEVER from an argument, so a value
    can't land in shell history or `ps`. Merges into the submitter's Secret, so run it once per
    adapter to accumulate (e.g. `--adapter claude-api` then `--adapter openai-api`).
    """
    _require_cloud_extra()
    from ..extensions import creds as credmod
    data = credmod.project(dict(os.environ), adapter)
    if not data:
        wanted = ", ".join((*secrets.ADAPTER_KEYS.get(adapter, ()), *secrets.GIT_ENV))
        fail("creds set", f"no credentials in the environment for adapter '{adapter}' "
                          f"(expected one of: {wanted}). Export them, then re-run.")
    kc = kc_str(kubeconfig)
    current = guard_context(kc, context)
    console.print(Panel.fit(
        f"register [bold]{as_submitter}[/] ({env_name}) · keys {', '.join(sorted(data))}\n"
        f"→ ns/loopkit-system · context [bold]{current}[/]", title="loopkit cloud creds set"))
    confirm_or_abort(f"Store {as_submitter}'s credentials on '{current}'?", yes=yes)
    name = credmod.set_credential(as_submitter, data, env_name=env_name, expected=context, kubeconfig=kc)
    console.print(f"[green]registered[/] {as_submitter} → {name} (keys: {', '.join(sorted(data))})")


@guarded_command("ls", on=creds_app)
def creds_ls(kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """List registered submitters in loopkit-system (key NAMES only, never values)."""
    _require_cloud_extra()
    from ..extensions import creds as credmod
    rows = credmod.list_credentials(kubeconfig=kc_str(kubeconfig))
    if not rows:
        console.print("[dim]no registered credentials[/] — `loopkit cloud creds set --as <you>`")
        return
    table = Table(title="loopkit credentials", header_style="bold")
    for col in ("submitter", "env", "keys"):
        table.add_column(col)
    for r in sorted(rows, key=lambda r: (r.env_name, r.submitter)):
        table.add_row(r.submitter, r.env_name, ", ".join(r.keys) or "—")
    console.print(table)


@guarded_command("rm", on=creds_app)
def creds_rm(
        as_submitter: str = typer.Option(..., "--as", help="The submitter to remove."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Delete a submitter's credential Secret — guarded by the context pin."""
    _require_cloud_extra()
    from ..extensions import creds as credmod
    kc = kc_str(kubeconfig)
    current = guard_context(kc, context)
    confirm_or_abort(f"Delete {as_submitter}'s credentials ({env_name}) on '{current}'?", yes=yes)
    name = credmod.delete_credential(as_submitter, env_name=env_name, expected=context, kubeconfig=kc)
    console.print(f"[green]removed[/] {name}")
