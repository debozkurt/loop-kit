"""`loopkit batch` — the parallel-batch shape as one command: a tasks manifest in, draft PRs out.

The third of the three shapes (one task / sequential backlog / parallel batch), with no infra: the
manifest declares the tasks and how they may collide (`group` serialization, `after` dependencies),
and the command runs them through the in-process fleet — N concurrent loops, each in its own clone
on its own branch. Issue-sourced goals resolve up front (a bad issue number fails fast, before any
worker starts), and `--dry-run` prints the resolved schedule so the plan is reviewable before a
token is spent.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .. import secrets, trace
from ..config import Config, load_config
from ._support import app, console, err, fail

# The terminal → color map for the summary table: green = merged-ready, yellow = needs a human look,
# dim = never ran (skipped / validate-aborted — not failures, but not successes either).
_REASON_COLORS = {"done": "green", "skipped": "dim", "validate_abort": "dim"}


@app.command("batch")
def batch(tasks_file: Path = typer.Option(..., "--tasks", "-t",
                                          help="TOML tasks manifest: a defaults table + task rows "
                                               "with id, goal|issue, config, group, after."),
          jobs: int = typer.Option(3, "--jobs", "-j", min=1,
                                   help="How many loops run concurrently (worker threads)."),
          only: list[str] | None = typer.Option(None, "--only",
                                                help="Run only these task ids (repeatable). Their "
                                                     "`after` dependencies must be included too."),
          provider: str | None = typer.Option(None, "--provider",
                                              help="Forge for issue-sourced goals: auto | github | "
                                                   "gitlab (default: the manifest's [defaults] "
                                                   "provider)."),
          open_pr: bool = typer.Option(False, "--open-pr",
                                       help="Enable push + draft PR for every task (overrides each "
                                            "config's [remote]), exactly like `run --open-pr`."),
          dry_run: bool = typer.Option(False, "--dry-run",
                                       help="Resolve configs + goals and print the schedule; run nothing."),
          timeout: float | None = typer.Option(None, "--timeout",
                                               help="Wall-clock stall guard in seconds: unfinished "
                                                    "tasks error out when it expires."),
          out: Path | None = typer.Option(None, "--out",
                                          help="Write the full JSON batch result here.")) -> None:
    """Run a manifest of tasks as a parallel batch — one isolated loop per task, conflict-aware.

    Tasks sharing a `group` run serially in manifest order (predicted file conflicts, shared test
    DBs); `after` edges gate a task on its dependencies reaching DONE and skip it if they don't.
    Everything else runs concurrently, capped by `--jobs`. Each task carries its own full config
    (gates, budget, stops, protected paths) via its `config` path, else the manifest's default.
    """
    # FIRST: load creds into memory + scrub them out of os.environ, before any subprocess (git, the
    # forge CLI, the agents) can inherit them — same discipline as every other entrypoint.
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from ..extensions.batch import BatchResult, load_manifest, make_batch_runner, plan_waves, run_batch
    try:
        manifest = load_manifest(tasks_file)
    except FileNotFoundError:
        fail("batch", f"no manifest at {tasks_file}")
    except Exception as exc:                              # noqa: BLE001 — surface validation cleanly
        fail("batch", f"invalid manifest: {escape(str(exc))}")

    specs = manifest.task
    if only:
        known = {s.id for s in specs}
        unknown = [i for i in only if i not in known]
        if unknown:
            fail("batch", f"--only names unknown task(s): {', '.join(unknown)}")
        keep = set(only)
        for spec in specs:
            if spec.id in keep:
                missing = [d for d in spec.after if d not in keep]
                if missing:
                    fail("batch", f"task '{spec.id}' depends on {', '.join(missing)} — include "
                                  "them in --only or drop the dependent.")
        specs = [s for s in specs if s.id in keep]

    manifest_dir = tasks_file.expanduser().resolve().parent
    base_cfg = _resolve_configs(specs, manifest.defaults, manifest_dir)
    trace.configure(base_cfg.trace if base_cfg else None)
    _resolve_goals(specs, provider or manifest.defaults.provider, manifest_dir)

    waves = plan_waves(specs)
    console.print(Panel.fit(
        f"[bold]{len(specs)}[/] task(s) · jobs {jobs} · waves {max(waves.values())}"
        f" · manifest {tasks_file}", title="loopkit batch"))
    console.print(_schedule_table(specs, waves))
    _print_overlap_warnings(specs)
    if dry_run:
        console.print("[dim]dry run — nothing executed[/]")
        raise typer.Exit(0)

    runner = make_batch_runner(base_config=base_cfg, open_pr=open_pr)
    result: BatchResult = run_batch(specs, runner, jobs=jobs, defaults=manifest.defaults,
                                    timeout=timeout)
    console.print(_result_table(result))
    console.print(f"done [green]{len(result.done)}[/] · failed [yellow]{len(result.failed)}[/]"
                  f" · skipped [dim]{len(result.skipped)}[/] of {len(result.rows)}")
    if out:
        payload = [{"task": row.spec.id, **asdict(row.outcome)} for row in result.rows]
        out.write_text(json.dumps(payload, indent=2) + "\n")
        console.print(f"[dim]wrote[/] {out}")
    raise typer.Exit(0 if result.rows and not result.failed else 2)


@app.command("overlap")
def overlap(tasks_file: Path = typer.Option(..., "--tasks", "-t",
                                            help="A batch manifest (batch.toml) or mold plan "
                                                 "(plan.toml) — mold-only keys are ignored.")) -> None:
    """Predict which tasks will collide — advisory similarity analysis, never a gate.

    Reads each task's predicted-touch set (the explicit `touches` field, else repo-relative path
    tokens in its goal text), intersects them pairwise, and suggests the scheduling levers the
    manifest doesn't already declare: a shared `group` per overlap cluster, plus a merge-order hint
    (tasks run in isolated clones, so overlapping PRs collide at merge time, not run time). Tasks
    with no touch data are listed as unanalyzed — never silently assumed conflict-free. Offline:
    issue-sourced goals are not fetched here; `batch` re-checks after fetching and warns there.
    """
    from ..extensions.batch import load_manifest
    from ..extensions.overlap import EXPLICIT, analyze
    try:
        manifest = load_manifest(tasks_file)
    except FileNotFoundError:
        fail("overlap", f"no manifest at {tasks_file}")
    except Exception as exc:                              # noqa: BLE001 — surface validation cleanly
        fail("overlap", f"invalid manifest: {escape(str(exc))}")
    specs = manifest.task
    report = analyze(specs)
    analyzed = len(specs) - len(report.unanalyzed)
    console.print(Panel.fit(
        f"[bold]{len(specs)}[/] task(s) · {analyzed} analyzed · "
        f"{len(report.collisions)} overlap(s) · manifest {tasks_file}", title="loopkit overlap"))

    if report.collisions:
        table = Table(title="predicted overlaps", header_style="bold")
        table.add_column("tasks")
        table.add_column("shared paths")
        table.add_column("declared?")
        for c in report.collisions:
            status = "[green]yes[/] (group/after)" if c.covered else "[yellow]no[/]"
            table.add_row(f"{c.a} ↔ {c.b}", "\n".join(c.paths), status)
        console.print(table)
    elif analyzed:
        console.print("[green]no predicted overlaps[/] among the analyzed tasks — "
                      "safe to run fully parallel")

    if report.suggestions:
        console.print("\n[bold]suggestions[/] (copy into the manifest — advisory, edit freely):")
        for tid, name in report.suggestions.items():
            # escape(): `[[task]]` is literal TOML here, not rich markup
            console.print(escape(f'  [[task]] id = "{tid}"  →  add: group = "{name}"'))
    for members in report.components:
        console.print(f"[dim]merge-order hint (manifest order):[/] {' → '.join(members)}")
    if report.unanalyzed:
        console.print(f"\n[dim]unanalyzed (no touches field, no paths in goal text):[/] "
                      f"{', '.join(report.unanalyzed)}")
    explicit = sum(1 for t in report.touches if t.source == EXPLICIT)
    console.print(f"[dim]touch data: {explicit} explicit · {analyzed - explicit} from goal text · "
                  f"{len(report.unanalyzed)} none — advisory only, nothing is blocked[/]")
    raise typer.Exit(0)


def _print_overlap_warnings(specs) -> None:
    """Advisory overlap warnings on the resolved batch (goals fetched, so issue text analyzes too).

    Prints only the *undeclared* collisions — pairs the manifest doesn't already cover with a
    shared group or an `after` path. Never blocks: a missed conflict costs one rebase at merge
    time; a false serialization would tax every batch.
    """
    from ..extensions.overlap import analyze
    for c in analyze(specs).uncovered:
        console.print(f"[yellow]predicted overlap:[/] {c.a} ↔ {c.b} share "
                      f"{', '.join(c.paths)} — no shared group/after "
                      f"[dim](advisory; see `loopkit overlap`)[/]")


def _resolve_configs(specs, defaults, manifest_dir: Path) -> Config | None:
    """Resolve + validate every config up front (fail fast, not mid-batch); return the loaded default.

    Per-task `config` paths (and the [defaults] one) resolve relative to the manifest's directory,
    so a manifest and its config files travel together. Each task's path is rewritten to the
    resolved absolute form the runner will load in its own thread.
    """
    base_cfg: Config | None = None
    if defaults.config:
        base_cfg = _load_checked(_resolve_path(defaults.config, manifest_dir), "defaults")
    for spec in specs:
        if spec.config:
            resolved = _resolve_path(spec.config, manifest_dir)
            _load_checked(resolved, spec.id)              # validate now; the runner reloads it
            spec.config = str(resolved)
        elif base_cfg is None:
            fail("batch", f"task '{spec.id}' has no config and the manifest sets no "
                          "[defaults] config.")
    return base_cfg


def _resolve_path(value: str, manifest_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (manifest_dir / path).resolve()


def _load_checked(path: Path, owner: str) -> Config:
    if not path.exists():
        fail("batch", f"config for '{owner}' not found: {path}")
    try:
        return load_config(path)
    except Exception as exc:                              # noqa: BLE001 — pydantic detail, cleanly
        fail("batch", f"invalid config for '{owner}' ({path}): {escape(str(exc))}")


def _resolve_goals(specs, provider: str, manifest_dir: Path) -> None:
    """Fetch every issue-sourced goal now, via gh/glab — the `run --from-issue` path, batched.

    The issue is fetched from the task's own config `repo` (that checkout's remote is the forge the
    issue lives on); a fetch failure fails the whole batch before any worker starts. Issue-sourced
    tasks inherit the issue title for their PR title unless the manifest set one.
    """
    from ..extensions import issues
    for spec in specs:
        if spec.goal or spec.issue is None:
            continue
        cfg = load_config(spec.config) if spec.config else None
        repo = cfg.repo_path() if cfg else manifest_dir
        issue = issues.fetch_issue(repo, spec.issue, provider=provider)
        if issue is None:
            fail("batch", f"task '{spec.id}': could not fetch issue #{spec.issue} "
                          f"(provider {provider}) — is gh/glab installed + authenticated?")
        task = issues.issue_to_task(issue)
        spec.goal = task["goal"]
        spec.title = spec.title or task["title"]


def _schedule_table(specs, waves: dict[str, int]) -> Table:
    table = Table(title="schedule", header_style="bold")
    table.add_column("wave", justify="right")
    table.add_column("task")
    table.add_column("goal source")
    table.add_column("group")
    table.add_column("after")
    for spec in sorted(specs, key=lambda s: (waves[s.id], s.id)):
        source = f"issue #{spec.issue}" if spec.issue is not None else "goal"
        table.add_row(str(waves[spec.id]), spec.id, source, spec.group or "-",
                      ", ".join(spec.after) or "-")
    return table


def _result_table(result) -> Table:
    table = Table(title="batch result", header_style="bold")
    table.add_column("task")
    table.add_column("branch")
    table.add_column("terminal")
    table.add_column("iters", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("PR")
    for row in result.rows:
        reason = row.outcome.reason
        color = _REASON_COLORS.get(reason, "yellow")
        table.add_row(row.spec.id, row.outcome.branch, f"[{color}]{reason}[/]",
                      str(row.outcome.iterations), f"${row.outcome.cost_usd:.2f}",
                      row.outcome.pr_url or "-")
    return table
