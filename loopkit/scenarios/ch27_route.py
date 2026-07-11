"""Ch 27 — route: turning a measured pass^k into a run strategy (Part IV, molding, Layer 4).

`measure` (Ch 24) answered *how reliably* the loop solves a goal — pass^k, which **falls** with k.
This chapter closes the loop: **so what do you DO about the number?** The molding rule that connects
`measure` to `evolve` is mechanical, which is exactly why it earns code rather than a paragraph in the
skill: **at or above a reliability bar, run once; below it, escalate to `evolve`** (best-of-N + held-out
re-validation), sizing the fan-out from how unreliable the task is. A `pass^1` of zero is flagged
honestly — escalation can't manufacture a capability the loop has never once shown.

This lab runs the decision rule over four representative measurements (no trials, no tokens — the
decision is a pure function of the counts) so every branch is visible: a reliable task → single, an
unreliable one → evolve at a sized population, a never-solved one → evolve-at-cap-but-flagged, and the
sharp one — a task that clears the single-run bar yet fails the "reliably pass three in a row" bar.
Scripted-only: it's a decision primitive, not an agent run, so --live doesn't apply.
"""
from __future__ import annotations

from rich.table import Table

from . import Scenario, Stage

_TS = "2026-07-11T00:00:00+00:00"       # fixed clock — the decision takes the timestamp as an input


def _row(stage, decision) -> None:
    strat = "[yellow]evolve[/]" if decision.escalated else "[green]single[/]"
    stage.console.print(f"  {decision.successes:>2}/{decision.trials} trials · pass^{decision.k} = "
                        f"[bold]{decision.pass_hat_k:.2f}[/]  →  {strat}   [dim]{decision.command}[/]")


def run(stage: Stage) -> None:
    from ..extensions.route import decide_route

    stage.beat("[bold]measure[/] (Ch 24) told us [italic]how reliably[/] the loop solves a goal — pass^k. "
               "This chapter answers the next question: [bold]so what do you DO about the number?[/] The "
               "rule that connects measure to [bold]evolve[/] is mechanical — reliable enough → run once; "
               "unreliable → escalate to best-of-N — which is why it's code, not a judgment call.")
    stage.beat("[bold]route[/] applies that rule to a measurement. The bar is [bold]pass^k ≥ threshold[/] "
               "(default k=1, the single-run success rate). Below it, escalate to evolve and [italic]size "
               "the population[/] from the base rate — a fan-out only as big as the task needs.")

    stage.rule("the rule over four measurements (threshold 0.90)")
    reliable = decide_route(trials=10, successes=9, timestamp=_TS)      # 0.9 → single
    flaky = decide_route(trials=10, successes=4, timestamp=_TS)         # 0.4 → evolve, sized
    never = decide_route(trials=8, successes=0, timestamp=_TS)          # 0.0 → evolve @cap, flagged
    for d in (reliable, flaky, never):
        _row(stage, d)

    stage.beat(f"[bold]9/10[/] clears the bar → [green]single run[/]: best-of-N would just spend tokens for "
               f"a result you'd already get. [bold]4/10[/] is unreliable → [yellow]evolve[/] at "
               f"[bold]p={flaky.population}[/] (sized so ~{flaky.pass_at_population:.0%} of runs find one "
               f"success); the held-out re-validation keeps the selection honest (Ch 9).")
    stage.beat(f"[bold]0/8[/] is the honest edge: the loop has [red]never[/] solved it single-shot, so a "
               f"bigger fan-out can't manufacture the capability. route escalates to the cap but says so — "
               f"[dim]{never.reason.split('—')[1].strip()[:90]}…[/] Fix the goal/gates/oracle or the model "
               f"first; routing can't.")

    stage.rule("the bar you set matters — reliability falls with k")
    single = decide_route(trials=10, successes=9, timestamp=_TS, k=1)
    strict = decide_route(trials=10, successes=9, timestamp=_TS, k=3)
    _row(stage, single)
    _row(stage, strict)
    stage.beat(f"Same [bold]9/10[/] task. At the single-run bar (k=1) it's fine. But demand it "
               f"[bold]reliably pass three independent runs[/] (k=3) and pass^3 = "
               f"[bold]{strict.pass_hat_k:.2f}[/] < 0.90 → [yellow]evolve[/]. This is the tau-bench point: "
               f"*can* ≠ *reliably does*, and the production bar is the second one.")

    stage.beat("route is [bold]advisory[/] — it prints the strategy + the exact command, never launching an "
               "(expensive) evolve itself; the molder or a human runs it, and the standing guardrails still "
               "bound anything that does. The decision carries the measurement's harness signature — "
               "[dim]`loopkit route --from-report report.json --out decision.json`[/] — so it's auditable, "
               "not a number floating free of its harness.")


SCENARIO = Scenario(chapter=27, slug="route",
                    title="route: a measured pass^k → a run strategy",
                    teaches="measure says how reliably the loop solves a goal; route turns that number "
                            "into a decision — run once if reliable, escalate to evolve (best-of-N) if "
                            "not, sizing the fan-out from the base rate and flagging a never-solved task "
                            "honestly. The mechanical feedback loop the molder routes through (Part IV "
                            "molding, Layer 4).",
                    live_supported=False, run=run)
