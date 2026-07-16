"""Ch 28 — batch: the parallel shape as one command, conflict-aware (Part III/IV bridge).

The three shapes of pointing loopkit at work: one task (`run`), a sequential backlog (`--plan`),
and a **parallel batch** — which until now meant the Redis fleet or the Python API. `loopkit batch`
is the promised no-infra middle: a TOML manifest of tasks → the same fleet machinery in-process →
one isolated clone + branch (+ draft PR) per task.

The lesson is the scheduling, because real batches are rarely fully independent: `group` serializes
tasks predicted to collide (same files, a shared test DB) while `after` encodes true dependencies
(expand-then-contract chains) — and a task whose dependency fails is **skipped**, never run against
a base it assumed. This lab drives the scheduler with a scripted runner (no repos, no tokens) so
every rule is visible in one screen: waves, serialization, and the cascading skip.
Scripted-only: it's the scheduler being taught, not an agent run, so --live doesn't apply.
"""
from __future__ import annotations

from rich.table import Table

from . import Scenario, Stage


def _result_table(result) -> Table:
    colors = {"done": "green", "skipped": "dim"}
    table = Table(title="batch result", header_style="bold")
    table.add_column("task")
    table.add_column("terminal")
    table.add_column("why")
    for row in result.rows:
        reason = row.outcome.reason
        table.add_row(row.spec.id, f"[{colors.get(reason, 'yellow')}]{reason}[/]",
                      row.outcome.error or "-")
    return table


def run(stage: Stage) -> None:
    from ..extensions.batch import TaskSpec, plan_waves, run_batch
    from ..extensions.fleet import WorkerOutcome

    stage.beat("[bold]Three shapes[/] of pointing loopkit at work: one task ([bold]run[/]), a "
               "sequential backlog ([bold]--plan[/]), and a [bold]parallel batch[/]. The batch shape "
               "used to need Redis + separately-started workers; [bold]loopkit batch[/] runs the same "
               "fleet machinery in-process — a TOML manifest in, one loop per task, draft PRs out.")
    stage.beat("Real batches aren't fully independent, so the manifest declares how tasks collide: "
               "[bold]group[/] = serialize in manifest order (they'd touch the same files, or share a "
               "test DB) — mutual exclusion, not a dependency. [bold]after[/] = a true dependency "
               "(the provider's change must land before the consumer's is attempted) — and a task "
               "whose dependency fails is [bold]skipped[/], never run against a base it assumed.")

    specs = [TaskSpec(id="clamp-limit", goal="clamp the page/limit params"),
             TaskSpec(id="fix-total", goal="return the real total", group="handlers"),
             TaskSpec(id="fix-sort", goal="stable sort ties", group="handlers"),
             TaskSpec(id="forward-key", goal="provider forwards the key"),
             TaskSpec(id="require-key", goal="consumer requires the key", after=["forward-key"]),
             TaskSpec(id="restrict-ingress", goal="close the catch-all", after=["require-key"])]

    stage.rule("1 · the dry-run schedule — waves are earliest starts, not barriers")
    waves = plan_waves(specs)
    table = Table(title="schedule", header_style="bold")
    table.add_column("wave", justify="right")
    table.add_column("task")
    table.add_column("group")
    table.add_column("after")
    for spec in sorted(specs, key=lambda s: (waves[s.id], s.id)):
        table.add_row(str(waves[spec.id]), spec.id, spec.group or "-", ", ".join(spec.after) or "-")
    stage.console.print(table)
    stage.beat("[bold]clamp-limit[/] and the first of each chain start immediately; the [bold]handlers[/] "
               "group serializes its two members; the key chain is strictly ordered. The live scheduler "
               "is event-driven — a task starts the [italic]moment[/] its predecessors finish, so waves "
               "are a lower bound on overlap, not an execution barrier.")

    stage.rule("2 · run it — with the middle of the key chain failing")
    def runner(task: dict) -> WorkerOutcome:
        reason = "no_progress" if task["id"] == "require-key" else "done"
        return WorkerOutcome(task_id=task["id"], branch=task["branch"], reason=reason, iterations=1)

    result = run_batch(specs, runner, jobs=3)
    stage.console.print(_result_table(result))
    stage.beat("[bold]require-key[/] failed — so [bold]restrict-ingress[/] was [dim]skipped[/], not run: "
               "closing the ingress while the consumer doesn't require the key yet would ship exactly the "
               "half-migrated state the [bold]after[/] edge exists to prevent. The skip [italic]cascades[/] "
               "down the chain, and every task still ends with an outcome — nothing silently dropped.")
    stage.beat("Each real task runs the full single-loop discipline in its own scratch clone: its own "
               "config (gates, budget, protected paths), the optional [bold]--review[/] judge and "
               "[bold]--validate[/] pre-check per task, and on DONE a push + [bold]draft PR[/] — a human "
               "is still the only merge authority, now over a whole batch at once.")


SCENARIO = Scenario(chapter=28, slug="batch",
                    title="batch: a manifest of tasks → parallel loops, conflict-aware",
                    teaches="the third shape — a no-infra parallel batch over the in-process fleet: "
                            "group serializes predicted collisions, after encodes real dependencies "
                            "(failures skip their dependents, cascading), and every task ends with an "
                            "outcome and its own draft PR for one human review pass.",
                    live_supported=False, run=run)
