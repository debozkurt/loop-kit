"""Ch 24 — reliability: pass^k, the metric that falls.

Every earlier chapter asked *can the loop solve this?* `evolve` (Ch 10-11) leans all the way into
that: run a goal many ways and keep the winner — **discovery**, which the field calls `pass@k` and
which *rises* with k (more attempts, more chances one lands). Production asks the opposite question:
*when I'm not cherry-picking, how often does it actually work?* That is **reliability** — `pass^k`,
the chance that **all** of k independent attempts succeed — and it *falls* with k. tau-bench's
headline is the whole lesson: a model >60% at one shot can be <25% across eight.

This lab measures that gap on the bundled demo-repo. It runs the **same goal five times**, each a full
isolated `run_loop` graded by the **held-out acceptance gate** (a trial counts only when that gate
certifies DONE — not the agent's say-so). The scripted agent is deliberately *flaky* — it solves a
fixed three of the five — so the pass/fail mix is reproducible and you can watch the two curves
diverge: discovery climbing toward 1, reliability collapsing toward 0.

It closes on the measurement discipline: the report carries the loopkit **version + a harness
signature + a timestamp**, because a number without its harness isn't a measurement (SWE-bench
Verified was retired in 2026 over a 10-20pt swing across scaffolds). Scripted-only: the flakiness is
the teaching device, so `--live` doesn't apply.
"""
from __future__ import annotations

import sys

from rich.table import Table

from ..agent import MockAgent
from . import CORRECT_PRICING, Scenario, Stage, demo_src

# A fixed timestamp so the lab's output is reproducible (the report takes the clock as an input).
_TS = "2026-06-22T00:00:00+00:00"
# The scripted agent solves these trial indices; the rest write a no-discount version that never
# clears the seen gate, so they halt short of DONE. 3 of 5 → pass^1 = 0.6.
_SOLVES = {0, 2, 4}
_WRONG = '''\
"""Line-item pricing — no bulk discount (fails the seen gate)."""


def line_total(unit_price, quantity):
    return round(unit_price * quantity, 2)
'''


def _factory(task: dict):
    """A flaky agent keyed off the trial id `measure` assigns (`<id>-t<i>`): solve, or write wrong."""
    i = int(str(task["id"]).rsplit("-t", 1)[1])
    body = CORRECT_PRICING if i in _SOLVES else _WRONG
    return MockAgent(behaviors=[lambda ws, _b=body: (ws / "pricing.py").write_text(_b) and "wrote"
                                or "wrote"])


def run(stage: Stage) -> None:
    from ..extensions.fleet import make_repo_runner
    from ..extensions.measure import measure_reliability

    stage.beat("Every chapter so far asked [bold]can the loop solve this?[/] [bold]evolve[/] leans "
               "into that — run a goal many ways, keep the winner. That's [bold]discovery[/], the "
               "field's [italic]pass@k[/], and it [green]rises[/] with k: more tries, more chances "
               "one lands.")
    stage.beat("Production asks the opposite: [bold]when I'm not cherry-picking, how often does it "
               "actually work?[/] That's [bold]reliability[/] — [italic]pass^k[/], the chance that "
               "[bold]all[/] of k independent attempts succeed — and it [red]falls[/] with k. A "
               "loop that's 60% one-shot can be far worse run after run.")
    stage.beat("We'll run the [bold]same goal five times[/], each a full isolated loop graded by the "
               "[bold]held-out gate[/] — a trial passes only when that gate certifies DONE. Our "
               "scripted agent is flaky on purpose: it solves a fixed [bold]3 of the 5[/].")

    py = sys.executable
    runner = make_repo_runner(
        str(demo_src()), mode="copy", max_iter=4,
        gate_iteration=f"{py} -m pytest tests/seen -q",
        gate_acceptance=f"{py} -m pytest tests/holdout -q",
        protected_paths=("tests/",), agent_factory=_factory)

    stage.rule("running 5 independent trials")
    report = measure_reliability(
        runner, {"id": "pricing", "goal": "Apply a 10% bulk discount at quantity >= 10."},
        trials=5, timestamp=_TS, adapter="mock", model="(scripted)", target="demo-repo")

    marks = "  ".join(f"[green]✓[/]" if o.passed else "[red]✗[/]" for o in report.outcomes)
    stage.beat(f"trials: {marks}  →  [bold]{report.successes}/5[/] reached the held-out DONE "
               f"(pass^1 = {report.success_rate:.0%}).")
    stage.console.print(_curve_table(report))

    stage.beat(f"Read the two columns. [italic]pass@k[/] climbs toward 1 — *somewhere* in k tries it "
               f"gets solved (that's what evolve banks on). [italic]pass^k[/] collapses: "
               f"pass^3 = [bold]{report.pass_hat_k[3]:.2f}[/] — three independent attempts all "
               f"succeeding is far from a sure thing, even though one-shot looked shippable.")
    stage.beat(f"Last: the number is stamped with its [bold]harness[/] — loopkit "
               f"{report.harness.loopkit_version}, sig [bold]{report.harness.signature}[/], "
               f"{report.timestamp}. A pass^k is only comparable to another taken on the *same* "
               f"harness; a number without its harness isn't a measurement. "
               f"[dim]`loopkit measure --out report.json`[/] persists the whole report.")


def _curve_table(report) -> Table:
    table = Table(title=f"reliability — {report.trials} trials, {report.successes} passed",
                  header_style="bold")
    table.add_column("k", justify="right")
    table.add_column("pass^k  (reliability ↓)", justify="right")
    table.add_column("pass@k  (discovery ↑)", justify="right")
    for k in sorted(report.pass_hat_k):
        table.add_row(str(k), f"{report.pass_hat_k[k]:.3f}", f"{report.pass_at_k[k]:.3f}")
    return table


SCENARIO = Scenario(chapter=24, slug="reliability", title="Reliability: pass^k, the metric that falls",
                    teaches="evolve optimizes discovery (pass@k, rises with k); production needs "
                            "reliability (pass^k, falls with k). `loopkit measure` runs a goal N times "
                            "and reports both, harness-stamped.",
                    live_supported=False, run=run)
