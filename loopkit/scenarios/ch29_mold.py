"""Ch 29 — mold-batch: unattended batch molding (Part IV, Layer 5).

Layers 1-4 equip a *copilot* to mold loopkit onto a repo. The case they don't cover is the one the
kit was built for: **batch remediation** — dozens of heterogeneous tasks, no copilot session per
task. `mold-batch` is the connective tissue: detect → tier-typed oracle skeleton → (optional)
proposer → **mandatory isolated fail-first verification** → route → a per-task config, ending in a
ready `loopkit batch` manifest. It never runs a loop — the human checkpoint is the seam between the
two commands: mold-all → one review → run.

This lab molds one task twice on the bundled demo-repo. First with no proposer: mechanical code
refuses to fake judgment, so the task stops at an annotated skeleton (needs-oracle). Then the
"judgment arrives" (a real failing oracle lands, standing in for a proposer or a human) and the same
command verifies, blesses, and emits the runnable batch. No tokens: the oracle is a shell exit code.
Scripted-only: the molding pipeline is the lesson, not an agent run, so --live doesn't apply.
"""
from __future__ import annotations

from rich.markup import escape

from . import Scenario, Stage

_TS = "2026-07-16T00:00:00+00:00"       # fixed clock — molding takes the timestamp as an input


def run(stage: Stage) -> None:
    from ..extensions.mold import MoldDefaults, MoldManifest, MoldSpec, mold_batch

    repo = stage.fixture()                        # the demo-repo: pytest markers, tests/, on main
    out = repo.parent / "molded"
    manifest = MoldManifest(
        defaults=MoldDefaults(repo=str(repo)),
        task=[MoldSpec(id="bulk-discount", tier="correctness",
                       goal="line_total must apply the 10% bulk discount at quantity >= 10")])

    stage.beat("[bold]detect[/], [bold]synth-gate[/], and [bold]route[/] serve a copilot molding one "
               "task. Batch remediation has [italic]no copilot per task[/] — that's Layer 5: "
               "[bold]mold-batch[/] runs the whole kit per task and ends in a reviewable artifact, "
               "never a run. Two knobs: [bold]--level[/] (how DEEP: detect < oracle < route < full) "
               "and [bold]--limit[/] (how MANY per invocation; a state file resumes the rest).")

    stage.rule("1 · mechanical-only: the kit refuses to fake judgment")
    first = mold_batch(manifest, out, level="oracle", timestamp=_TS)
    row = first.rows[0]
    stage.console.print(f"  status: [yellow]{row.status}[/] — {row.note}")
    skeleton = (out / "bulk-discount" / "acceptance" / "run.sh").read_text()
    fills = sum(line.count("FILL") for line in skeleton.splitlines())
    stage.beat(f"With no [bold]--proposer[/], the task gets the coverage-tier skeleton ({fills} FILL "
               "markers, the tier's typed assertion inlined) and stops at [yellow]needs-oracle[/] — "
               "[italic]unverified, unblessed, excluded from any emitted batch[/]. The table carries the "
               "mechanical half of proposal; the judgment half is a [bold]seam[/] (a fresh-context "
               "headless agent, or a human), never a rule pretending to be judgment.")

    stage.rule("2 · judgment arrives — the same command verifies and blesses")
    acc = out / "bulk-discount" / "acceptance"
    (acc / "run.sh").write_text("#!/usr/bin/env bash\n"
                                "python - <<'EOF'\n"
                                "from pricing import line_total\n"
                                "assert line_total(10.0, 10) == 90.0, 'no bulk discount applied'\n"
                                "EOF\n")
    second = mold_batch(manifest, out, level="full", timestamp=_TS)
    row = second.rows[0]
    verdict = row.verdict
    checks = ", ".join(f"{c.name}={'ok' if c.ok else 'FAIL'}" for c in verdict.checks)
    stage.console.print(f"  status: [green]{row.status}[/] — {row.note}")
    stage.console.print(f"  verdict: blessed={verdict.blessed} ({checks}) sig={verdict.signature}")
    stage.beat("A failure at needs-oracle always [bold]retries[/] (successes skip via state.json) — the "
               "loop is [italic]fill the FILLs, re-run[/]. The filled oracle is goal-derived and therefore "
               "[bold]untrusted input[/]: verification is mandatory and runs in an [italic]isolated copy[/] "
               "(the security boundary). It FAILS on the buggy tree → [green]blessed[/], with the verdict "
               "stored beside it as provenance. An oracle that had passed would be "
               "[yellow]oracle-rejected[/] — it certifies nothing.")

    stage.rule("3 · the terminal artifact is a batch, not a run")
    emitted = (out / "batch.toml").read_text()
    head = escape("\n".join(emitted.splitlines()[:10]))   # raw TOML — [[task]] is not rich markup
    stage.console.print(f"[dim]{head}\n…[/]")
    stage.beat("At [bold]full[/], each blessed task gets its config (iteration gate from detect, "
               "acceptance wired to its own oracle, protected paths, budget) and lands in "
               "[bold]molded/batch.toml[/] — anything not ready is listed in a comment block, demoted "
               "visibly, never dropped. The checkpoint is the seam between commands: review the molded "
               "instances [bold]once[/], then [dim]loopkit batch --tasks molded/batch.toml[/] fans out "
               "the loops. Molding proposes; a human still launches and merges.")


SCENARIO = Scenario(chapter=29, slug="mold-batch",
                    title="mold-batch: many tasks, no copilot per task (Layer 5)",
                    teaches="unattended batch molding as connective tissue over detect/synth-gate/route: "
                            "the tier table carries the mechanical half of oracle proposal, a seam carries "
                            "the judgment half, verification is mandatory + isolated, and the output is a "
                            "reviewable batch manifest — mold-all, one human review, then loopkit batch.",
                    live_supported=False, run=run)
