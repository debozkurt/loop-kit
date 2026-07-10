"""Ch 25 — synth-gate: proving a held-out oracle is real (Part IV, molding, Layer 2).

Chapter 9 taught the held-out acceptance gate: the loop can't game a test it never sees. But a
held-out gate is only worth having if it actually *discriminates* a buggy tree from a fixed one — and
a copilot that proposes an oracle almost never checks that. Two ways a proposed oracle silently lies:

  - it **already passes** on the buggy tree (the goal doesn't reproduce, or the "test" never asserts
    the target) — it would certify DONE on tick zero, measuring nothing;
  - it **can never pass** (a broken import, a wrong path, an impossible assertion) — it fails forever
    and the loop burns its whole budget against a mirage.

`loopkit synth-gate` is the primitive that catches both, by running the oracle across the fail→pass
transition (the same thing SWE-bench validates its FAIL_TO_PASS tests with): the oracle must FAIL on
the buggy tree (**fail-first** — it reproduces the goal) and, given a reference fix, must PASS on the
fixed tree (**pass-on-fix** — it's satisfiable and truly discriminates). Only an oracle that flips is
*blessed*.

This lab runs entirely on the bundled demo-repo, whose seeded `quantity > 10` bug is the perfect
fixture. It blesses the real held-out oracle, then shows the Chapter-9 trap — the *seen* suite passes
on the buggy tree, so it certifies nothing held-out and synth-gate refuses it — and finally proves the
real oracle flips fail→pass once the bug is fixed. Scripted-only: it's a verification primitive, not an
agent run, so `--live` doesn't apply. No tokens, no network.
"""
from __future__ import annotations

import sys

from rich.table import Table

from . import CORRECT_PRICING, Scenario, Stage

# A fixed timestamp so the lab's output is reproducible (the verdict takes the clock as an input).
_TS = "2026-07-10T00:00:00+00:00"


def _verdict_table(verdict) -> Table:
    table = Table(title=f"oracle verification — {'blessed' if verdict.blessed else 'NOT blessed'}",
                  header_style="bold")
    table.add_column("check", justify="left")
    table.add_column("a real oracle…", justify="left")
    table.add_column("this oracle", justify="left")
    for check in verdict.checks:
        got = "[green]met[/]" if check.ok else "[red]did not[/]"
        table.add_row(check.name, f"must {check.expected}", got)
    return table


def run(stage: Stage) -> None:
    from ..extensions.synth_gate import verify_oracle

    py = sys.executable
    repo = stage.fixture()                       # a fresh git demo-repo with the seeded > 10 bug
    holdout = f"{py} -m pytest tests/holdout -q"  # the real held-out acceptance oracle
    seen = f"{py} -m pytest tests/seen -q"        # the visible suite — passes even on the buggy tree

    stage.beat("Chapter 9 gave us the [bold]held-out[/] acceptance gate — the loop can't game a test "
               "it never sees. But a held-out gate only earns its keep if it actually tells a "
               "[bold]buggy[/] tree from a [bold]fixed[/] one. A proposed oracle can lie two ways: it "
               "[bold]already passes[/] (reproduces nothing, certifies DONE on tick zero) or it "
               "[bold]can never pass[/] (a mirage the loop burns its budget on).")
    stage.beat("[bold]synth-gate[/] proves an oracle real the way SWE-bench validates a FAIL_TO_PASS "
               "test: run it across the fail→pass transition. [bold]fail-first[/] — it must fail on "
               "the buggy tree. [bold]pass-on-fix[/] — given a reference fix, it must pass on the "
               "fixed one. Only an oracle that [italic]flips[/] gets blessed.")

    stage.rule("1 · the real held-out oracle — fail-first")
    v1 = verify_oracle(holdout, repo, timestamp=_TS)
    stage.console.print(_verdict_table(v1))
    stage.beat(f"The held-out oracle [green]FAILS[/] on the buggy tree — the boundary assertion "
               f"`line_total(2.00, 10) == 18.00` doesn't hold while the code says `> 10`. That's real: "
               f"passing it *later* is now evidence, not a freebie. "
               f"[bold]{'blessed' if v1.blessed else 'refused'}[/].")

    stage.rule("2 · the Chapter-9 trap — an oracle that's already green")
    v2 = verify_oracle(seen, repo, timestamp=_TS)
    stage.console.print(_verdict_table(v2))
    stage.beat("Someone proposes the [bold]seen[/] suite as the acceptance gate. It looks like a test "
               "— but it [red]already passes[/] on the buggy tree (it misses the boundary, the whole "
               "Chapter-9 point). synth-gate [bold]refuses[/] it: a gate that's green before the fix "
               "certifies nothing. This is the check a copilot skips.")

    stage.rule("3 · the gold check — fail→pass with a reference fix")
    fix = (f"{py} -c \"import pathlib; p = pathlib.Path('pricing.py'); "
           f"p.write_text(p.read_text().replace('> 10', '>= 10'))\"")
    v3 = verify_oracle(holdout, repo, timestamp=_TS, fix=fix)
    stage.console.print(_verdict_table(v3))
    stage.beat("Now give synth-gate a [bold]reference fix[/]. In an isolated copy it applies the fix, "
               "re-runs the oracle, and the oracle [green]flips to pass[/]. That proves the oracle is "
               "[italic]satisfiable[/] and genuinely discriminates buggy-from-fixed — a gate that "
               "never flips is as useless as one that's always green.")

    stage.beat(f"Last: the verdict is a [bold]provenance record[/] — oracle + fix + sig "
               f"[bold]{v3.signature}[/], loopkit {v3.loopkit_version}, {v3.timestamp}. A blessing is "
               f"auditable and reproducible, not 'looked fine to me'. [dim]`loopkit synth-gate <oracle> "
               f"--fix <cmd> --out verdict.json`[/] — the load-bearing half of oracle synthesis the "
               f"molder can't self-supply.")


SCENARIO = Scenario(chapter=25, slug="synth-gate",
                    title="synth-gate: proving a held-out oracle is real",
                    teaches="proposing a test is easy; proving it certifies anything is the job. "
                            "synth-gate runs an oracle across the fail→pass transition — fail-first on "
                            "the buggy tree, pass-on-fix on the fixed one — and blesses only a gate "
                            "that flips (Part IV molding, Layer 2).",
                    live_supported=False, run=run)
