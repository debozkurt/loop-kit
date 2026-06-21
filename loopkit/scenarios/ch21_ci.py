"""Ch 21 — the CI deployment tier: an issue becomes a draft PR, with no cluster.

Chapter 18 stood up loopkit's *own* webhook listener — the cloud tier hand-builds the trigger. This
chapter is the cheaper realization of the same idea: let the **forge's CI** be the trigger. A labelled
issue starts a CI job, the job runs **one `loopkit run`**, and the result is a draft PR that closes the
issue on merge. No Kubernetes, no listener, no secret resolver — because the forge already *is* the
trigger, the secret store, the run identity, and the per-job sandbox. That "use the platform's
primitives first" instinct is the whole tier.

This lab runs the real CI glue end to end, offline:

    canned GitHub `issues` payload  ──parse_event_payload──▶  goal = title+body
        ──▶ run_loop over a fresh demo-repo (scripted, or claude-code with --live)
            ──▶ DONE ──▶ the outward edge: push the branch + open a draft PR ("Closes #N")

Only the network edge is simulated (scenarios never reach a forge) — the parser, the loop, the gates,
and the safety envelope are the same ones `loopkit run --from-event … --open-pr` uses in a real Action.
"""
from __future__ import annotations

import json

from rich.panel import Panel
from rich.table import Table

from ..agent import MockAgent
from ..extensions import triggers
from . import CORRECT_PRICING, Scenario, Stage, demo_config, pytest_gates, writes

# The forge hands a CI job the issue event verbatim (Actions writes it to $GITHUB_EVENT_PATH). This is
# that payload — and its title+body is the demo-repo's pricing task, so the loop has something to solve.
ISSUE_NUMBER = 128
_EVENT = {
    "action": "labeled",                       # an Action `on: issues: [opened, labeled]` fires here
    "issue": {
        "number": ISSUE_NUMBER,
        "title": "Apply a 10% bulk discount in line_total",
        "body": ("When quantity >= 10, line_total should apply a 10% bulk discount before rounding. "
                 "See PROMPT.md for the exact spec. Don't weaken the tests."),
        "user": {"login": "ada"},
        "labels": [{"name": "loopkit"}],
    },
    "repository": {"full_name": "acme/pricing", "clone_url": "https://github.com/acme/pricing.git"},
}


def run(stage: Stage) -> None:
    stage.beat("loopkit runs at [bold]three deployment tiers[/], same loop core, only the "
               "trigger/secrets/isolation differ: [bold]local[/] (a human, your laptop), [bold]cloud "
               "fleet[/] (your own Kubernetes — Ch 12/20, the listener + workers), and the one in "
               "between, [bold]CI[/]: a forge job. The CI tier needs [italic]no cluster[/] because the "
               "forge already gives you the four things the cloud tier hand-builds.")
    stage.console.print(_tiers_table())
    stage.beat("[bold]Use the platform's primitives first.[/] In CI the forge is the [bold]trigger[/] "
               "(a labelled issue starts the job), the [bold]secret store[/] (masked CI variables), "
               "the [bold]identity[/] (the job's scoped token), and the [bold]sandbox[/] (a throwaway "
               "runner). loopkit doesn't rebuild any of them — it just runs the loop. That's the whole "
               "appeal, and it's why the CI path is [italic]additive[/]: no cloud code involved.")

    # 1. The forge hands the job the issue event. `parse_event_payload` auto-detects the forge by shape
    #    (GitHub vs GitLab) and extracts the goal — the exact code `loopkit run --from-event` runs.
    event = triggers.parse_event_payload(_EVENT)
    goal = f"{event.title}\n\n{event.body}".strip()
    stage.beat(f"The job calls [bold]loopkit run --from-event \"$GITHUB_EVENT_PATH\"[/]. "
               f"`parse_event_payload` reads the issue (#{event.issue_number}) and the goal becomes its "
               f"[bold]title + body[/], verbatim:\n\n   [italic]{event.title}[/]\n\nSame builder the "
               "webhook tier uses — one issue→goal mapping, every tier.")

    # 2. Drive the real loop over a fresh demo-repo. Scripted writes the fix; --live lets claude-code
    #    solve the issue for real. Same gates, same protected-path guard, same budget stop as any tier.
    repo = stage.fixture()
    iteration, acceptance = pytest_gates()
    scripted = MockAgent(behaviors=[writes("pricing.py", CORRECT_PRICING)])
    cfg = demo_config(repo, goal=goal, max_iter=6)
    stage.beat("Now the loop runs — the [bold]same[/] `run_loop`, gates, and safety envelope as the "
               "local and cloud tiers. In CI the runner [italic]is[/] the Ch 16 sandbox, so loopkit "
               "doesn't build one; its own controls (branch-only, protected paths, the held-out gate, "
               "the budget stop) still hold. " +
               ("[bold]--live[/]: claude-code solves the issue." if stage.live
                else "Scripted: the agent writes the fix; pass [bold]--live[/] to watch claude-code "
                     "solve it for real."))
    result = stage.run(cfg, stage.agent(scripted), iteration_gate=iteration, acceptance_gate=acceptance)

    # 3. The outward edge: --open-pr flips [remote] on for this invocation → push + a DRAFT PR that
    #    closes the issue on merge. Simulated here (no forge in a scenario); real via remote.sync_done.
    if result.reason.value == "done":
        stage.console.print(_draft_pr_panel(cfg.branch, event))
        stage.beat(f"DONE → the [bold]--open-pr[/] outward edge: push [bold]{cfg.branch}[/] and open a "
                   f"[bold]draft[/] PR whose body carries [bold]Closes #{event.issue_number}[/], so "
                   "merging it closes the issue. Draft, because a human reviews and merges — the loop "
                   "proposes, it doesn't ship. (Simulated here; real runs call `remote.sync_done`.)")
    else:
        stage.beat(f"The run halted at [yellow]{result.reason.value}[/] — no PR. In CI that's a failed "
                   "job and a red check on the issue; nothing leaves a branch until the loop is done.")

    stage.beat("That's the tier you'll reach for first on a real repo: [bold]loopkit init --ci "
               "github|gitlab[/] scaffolds the workflow, you label an issue, and a draft PR shows up — "
               "zero infrastructure. One honest caveat: CI secrets are [bold]repo-scoped[/], so a run "
               "spends the [italic]repo's[/] key, attributed to the run, not to the engineer who filed "
               "the issue. Per-submitter keys + cost caps are the cloud tier's job. See "
               "[bold]docs/part-iii-ecosystem.md[/].")


def _tiers_table() -> Table:
    table = Table(title="the three deployment tiers", header_style="bold")
    table.add_column("tier")
    table.add_column("trigger")
    table.add_column("secrets")
    table.add_column("isolation")
    table.add_row("local", "a human", "local env", "the laptop")
    table.add_row("[bold]CI (this chapter)[/]", "forge issue / cron", "[bold]CI-native[/]", "[bold]the runner[/]")
    table.add_row("cloud fleet", "CLI / cron / webhook", "per-submitter resolver", "namespace + container")
    return table


def _draft_pr_panel(branch: str, event) -> Panel:
    body = (f"Automated by loopkit on branch `{branch}`.\n"
            f"Goal: {event.title}\n\nCloses #{event.issue_number}")
    return Panel.fit(
        f"[green]draft PR[/]  {event.repo}  ·  [bold]{branch}[/] → main\n"
        f"title: loopkit: {event.title[:60]}\n[dim]{body}[/]",
        title="outward edge (--open-pr)")


SCENARIO = Scenario(chapter=21, slug="ci-tier", title="The CI deployment tier",
                    teaches="Run the loop from forge CI with no cluster: an issue becomes a draft PR; "
                            "the forge is the trigger, secrets, identity, and sandbox.",
                    live_supported=True, run=run)
