"""`loopkit mold-batch` — unattended batch molding (Part IV, Layer 5): tasks in, a reviewable
molded batch out.

For each task: detect the repo (mechanical config) → materialise the tier-typed oracle skeleton →
let the optional proposer fill it (the judgment seam) → verify fail-first in isolation (mandatory —
goal-derived oracles are untrusted) → route on real pass^k when a report exists → emit the per-task
config. The command never runs a loop: its terminal artifact is `<out>/batch.toml` plus per-task
provenance, and the human checkpoint is the seam between this command and `loopkit batch`.

The two knobs: `--level` (how DEEP each task is molded: detect < oracle < route < full) and
`--limit` (how MANY tasks this invocation processes; the state file resumes the rest).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .. import secrets
from ._support import app, console, fail

# Status → color for the tables: green = the level's success, yellow = needs a human, dim = partial.
_STATUS_COLORS = {"ready": "green", "verified": "green", "routed": "green", "detected": "green",
                  "needs-oracle": "yellow", "oracle-rejected": "yellow", "needs-config": "yellow"}


@app.command("mold-batch")
def mold_batch_cmd(
        tasks_file: Path = typer.Option(..., "--tasks", "-t",
                                        help="TOML molding manifest: a defaults table (repo, "
                                             "proposer, ...) + task rows with id, goal|issue, tier."),
        out_dir: Path = typer.Option(Path("molded"), "--out", "-o",
                                     help="Output directory: per-task molded instances + "
                                          "provenance + the emitted batch manifest."),
        level: str = typer.Option("full", "--level",
                                  help="How deep to mold: detect | oracle | route | full. "
                                       "Below-full emits partial instances for review."),
        limit: int | None = typer.Option(None, "--limit", min=1,
                                         help="Mold at most N unmolded tasks this invocation; "
                                              "re-invoke to resume (state.json tracks progress)."),
        force: bool = typer.Option(False, "--force",
                                   help="Re-mold tasks that already succeeded at this level."),
        proposer: str | None = typer.Option(None, "--proposer",
                                            help="Oracle-proposer command (ShellProposer) — "
                                                 "overrides the manifest's default. See the "
                                                 "MOLD_* env contract in extensions/mold.py."),
        provider: str | None = typer.Option(None, "--provider",
                                            help="Forge for issue-sourced goals: auto | github | "
                                                 "gitlab (default: the manifest's)."),
        dry_run: bool = typer.Option(False, "--dry-run",
                                     help="Parse + resolve and print the molding plan; write "
                                          "nothing.")) -> None:
    """Mold a batch of tasks into runnable loopkit instances — reviewable, never auto-run.

    Each task gets its own directory under --out: the detect profile, the tier-typed oracle (a
    skeleton until the proposer or a human fills it), the synth-gate verdict, the route decision,
    and (at full) a wired per-task config. The emitted batch.toml lists only blessed, ready tasks;
    everything else is surfaced for attention — review the instances, then run `loopkit batch`.
    """
    # FIRST: load creds into memory + scrub them out of os.environ, before any subprocess (the
    # forge CLI, the proposer, the oracle runs) can inherit them.
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from ..extensions.mold import LEVELS, ShellProposer, load_mold_manifest, mold_batch
    if level not in LEVELS:
        fail("mold-batch", f"unknown --level '{level}' (one of: {', '.join(LEVELS)})")
    try:
        manifest = load_mold_manifest(tasks_file)
    except FileNotFoundError:
        fail("mold-batch", f"no manifest at {tasks_file}")
    except Exception as exc:                              # noqa: BLE001 — surface validation cleanly
        fail("mold-batch", f"invalid manifest: {escape(str(exc))}")

    manifest_dir = tasks_file.expanduser().resolve().parent
    _resolve_repos(manifest, manifest_dir)
    _resolve_goals(manifest, provider or manifest.defaults.provider)

    proposer_cmd = proposer or manifest.defaults.proposer
    console.print(Panel.fit(
        f"[bold]{len(manifest.task)}[/] task(s) · level {level}"
        + (f" · limit {limit}" if limit else "")
        + f" · proposer {'set' if proposer_cmd else 'none (skeletons stop at needs-oracle)'}"
        + f"\nout {out_dir}", title="loopkit mold-batch"))
    if dry_run:
        console.print(_plan_table(manifest))
        console.print("[dim]dry run — nothing written[/]")
        raise typer.Exit(0)

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = mold_batch(manifest, out_dir, level=level, timestamp=timestamp, limit=limit,
                        force=force,
                        proposer=ShellProposer(proposer_cmd) if proposer_cmd else None)
    console.print(_result_table(result))
    if result.skipped:
        console.print(f"[dim]skipped {len(result.skipped)} already-molded task(s) "
                      f"(--force to re-mold)[/]")
    if level == "full":
        console.print(f"next: review [bold]{out_dir}[/], then "
                      f"[bold]loopkit batch --tasks {Path(out_dir) / 'batch.toml'}[/]")
    if result.attention:
        console.print(f"[yellow]{len(result.attention)} task(s) need attention[/] — fill the "
                      "oracle FILLs (or wire --proposer) and re-run; failures always retry.")
    raise typer.Exit(2 if result.attention else 0)


def _resolve_repos(manifest, manifest_dir: Path) -> None:
    """Resolve repo paths (relative to the manifest, so the pair travels together) and require
    each to exist — molding introspects a real checkout."""
    def resolve(value: str, owner: str) -> str:
        path = Path(value).expanduser()
        path = path if path.is_absolute() else (manifest_dir / path).resolve()
        if not path.is_dir():
            fail("mold-batch", f"repo for '{owner}' is not a directory: {path}")
        return str(path)

    if manifest.defaults.repo:
        manifest.defaults.repo = resolve(manifest.defaults.repo, "defaults")
    for spec in manifest.task:
        if spec.repo:
            spec.repo = resolve(spec.repo, spec.id)


def _resolve_goals(manifest, provider: str) -> None:
    """Fetch every issue-sourced goal now via gh/glab (fail fast) — the `run --from-issue` path."""
    from ..extensions import issues
    for spec in manifest.task:
        if spec.goal or spec.issue is None:
            continue
        repo = Path(spec.repo or manifest.defaults.repo)
        issue = issues.fetch_issue(repo, spec.issue, provider=provider)
        if issue is None:
            fail("mold-batch", f"task '{spec.id}': could not fetch issue #{spec.issue} "
                               f"(provider {provider}) — is gh/glab installed + authenticated?")
        task = issues.issue_to_task(issue)
        spec.goal = task["goal"]
        spec.title = spec.title or task["title"]


def _plan_table(manifest) -> Table:
    table = Table(title="molding plan", header_style="bold")
    table.add_column("task")
    table.add_column("tier")
    table.add_column("goal source")
    table.add_column("repo")
    for spec in manifest.task:
        source = f"issue #{spec.issue}" if spec.issue is not None else "goal"
        table.add_row(spec.id, spec.tier, source, spec.repo or manifest.defaults.repo or "-")
    return table


def _result_table(result) -> Table:
    table = Table(title="molding result", header_style="bold")
    table.add_column("task")
    table.add_column("tier")
    table.add_column("status")
    table.add_column("note")
    for row in result.rows:
        color = _STATUS_COLORS.get(row.status, "yellow")
        table.add_row(row.spec.id, row.spec.tier, f"[{color}]{row.status}[/]", row.note)
    return table
