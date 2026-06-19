"""Ch 12 — the deployable fleet: the loop behind a queue, run by many workers.

Chapter 10 fanned the loop out across git worktrees *in one process* — isolation was logical.
Chapter 12 deploys it: each worker is its own container, so isolation is physical, and the
in-memory handoff becomes a queue. The queue is also the trigger seam — a worker is indifferent
to what woke it, so anything that can push a task drives the fleet.

This scenario stands the fleet up in-process: an `InMemoryQueue` and three worker *threads* play
the part of three pods, each running the real demo-repo runner (`run_loop` over a fresh clone, mock
agent, no tokens). The wire contract is identical to the deployed fleet — `tilt up` swaps the
threads for pods on a kind cluster and the queue for Redis, nothing else.
"""
from __future__ import annotations

from rich.table import Table

from ..extensions.fleet import (
    PRICING_GOAL,
    Coordinator,
    InMemoryQueue,
    make_demo_runner,
    run_workers,
)
from . import Scenario, Stage

# Three independent shards of the same kind of work — the make-or-break property of fan-out is
# that no shard needs anything another produced, so N workers finish in ~the time of the slowest.
SHARDS = 3


def run(stage: Stage) -> None:
    stage.beat("Chapter 10 fanned the loop out across git worktrees in [bold]one process[/] — "
               "isolation was logical (one repo, many working dirs). Deploying it flips two "
               "things: each worker becomes its own [bold]container[/] (isolation goes physical — "
               "its own filesystem, its own clone, its own branch), and the in-memory handoff "
               "becomes a [bold]queue[/]. Same loop body; stronger boundary.")

    queue = InMemoryQueue()
    runner = make_demo_runner(adapter="mock", max_iter=4)        # the real pod runner, no tokens
    workers, threads = run_workers(queue, runner, count=SHARDS, run_id="demo", poll_timeout=0.05)

    stage.beat(f"Three worker 'pods' are up, blocked on [bold]BRPOP[/] — waiting for work. The "
               f"coordinator now [bold]LPUSH[/]es {SHARDS} independent tasks onto the queue and "
               "walks away; it never holds a worker. Whichever pod is free grabs the next task — "
               "the queue is a work-stealing dispatcher for free.")

    try:
        tasks = [{"slug": f"shard-{i}", "branch": f"loopkit/run-shard-{i}", "goal": PRICING_GOAL}
                 for i in range(SHARDS)]
        fleet = Coordinator(queue, collect_timeout=60).run_fleet(tasks)
    finally:
        for worker in workers:
            worker.stop()
        for thread in threads:
            thread.join(timeout=5)

    stage.console.print(_fleet_table(fleet))
    stage.beat(f"{len(fleet.done)}/{len(fleet.workers)} pods reached done, each on its own "
               "[bold]loopkit/run-shard-*[/] branch — and crucially, in its own filesystem. There "
               "is no shared tree to leak into: container isolation makes the collision problem "
               "[italic]structurally[/] impossible, where worktrees made it merely avoidable.")

    stage.beat("The queue is the [bold]trigger[/] seam (Ch 12): the worker doesn't care that a "
               "coordinator pushed these tasks — a cron, a webhook, or a human pushing one task "
               "would wake the same `run_loop`. That's what 'the loop is indifferent to what woke "
               "it' buys you once the loop is trustworthy.")

    stage.beat("Evolution rides the same rails: [bold]loopkit fleet evolve[/] enqueues a population "
               "at one goal, keeps top-k, and — the load-bearing Ch 9 guard — only reseeds a winner "
               "the worker confirmed on a [bold]held-out[/] gate it never competed on. v1 reseeds "
               "[italic]prompt-level[/] (the winner's note rides the next goal); [italic]tree-"
               "level[/] reseed (branch off the winner's code) needs a shared volume the next "
               "generation clones from — that's v2.")


def _fleet_table(fleet) -> Table:
    table = Table(title="fleet result", header_style="bold")
    table.add_column("shard")
    table.add_column("branch")
    table.add_column("terminal")
    table.add_column("iters", justify="right")
    for worker in fleet.workers:
        reason = worker.result.reason.value if worker.result else (worker.error or "error")
        color = "green" if worker.done else "yellow"
        iters = str(worker.result.iterations) if worker.result else "-"
        table.add_row(str(worker.task["slug"]), worker.branch, f"[{color}]{reason}[/]", iters)
    return table


SCENARIO = Scenario(chapter=12, slug="fleet", title="The deployable fleet",
                    teaches="Deploy the loop behind a queue: workers as containers (physical "
                            "isolation), the queue as the trigger, fan-out and evolution over Redis.",
                    live_supported=False, run=run)
