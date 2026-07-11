"""Ch 26 — detect: reading a repo's mechanical config deterministically (Part IV, molding, Layer 3).

Molding loopkit to a repo is a copilot's judgment job — *except* the parts where a guess is dangerous.
A hallucinated test command wastes a run; a hallucinated protected path lets the loop weaken its own
held-out gate (Ch 9) or churn a migration it should never touch. Those aren't judgment calls — they're
readable off file markers, at zero tokens. `loopkit detect` reads them so neither a copilot nor an
unattended agent has to guess, and prints a **proposed** loopkit.toml with every fact backed by its
evidence.

The line it holds is the whole Part IV thesis: detect fills the mechanical, safety-critical scaffold and
deliberately leaves the two things no marker can tell it — the **goal** and the **held-out acceptance
oracle** (author it, then verify with synth-gate, Ch 25) — as annotated placeholders. The copilot keeps
the judgment; loopkit supplies the determinism the judgment can't self-supply.

This lab runs on the bundled demo-repo (pytest.ini + a tests/ dir + a CLAUDE.md) — first as it ships, so
detection is honest and minimal, then after dropping in a CI workflow, a migrations dir, and a lockfile,
to show detect propose exactly the paths that now exist (evidence, never a guess). Scripted-only: it's a
read-only introspection primitive, not an agent run, so --live doesn't apply. No tokens, no network.
"""
from __future__ import annotations

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import Scenario, Stage


def _detect_table(profile) -> Table:
    colors = {"high": "green", "medium": "yellow", "low": "yellow", "none": "red"}
    table = Table(title="detected", header_style="bold")
    table.add_column("fact")
    table.add_column("value", overflow="fold")
    table.add_column("confidence")
    table.add_column("evidence", overflow="fold")
    for d in profile.detections:
        color = colors.get(d.confidence, "dim")
        table.add_row(d.key, d.value or "—", f"[{color}]{d.confidence}[/]", d.evidence)
    return table


def run(stage: Stage) -> None:
    from ..extensions.detect import detect_repo

    repo = stage.fixture()                       # a fresh git demo-repo (pytest.ini, tests/, on main)

    stage.beat("Molding is mostly a copilot's judgment — but two things must never be [bold]guessed[/]: "
               "the [bold]test command[/] (a wrong one wastes a whole run) and the [bold]protected "
               "paths[/] (a wrong one lets the loop weaken its own held-out gate, or churn a migration "
               "it should never touch). Those are readable off file markers, at zero tokens.")
    stage.beat("[bold]loopkit detect[/] reads them — the test runner, the protected-path candidates, the "
               "default branch, the agent on PATH — and prints a [bold]proposed[/] loopkit.toml. Every "
               "fact carries its [italic]evidence[/], so the proposal is auditable, not opaque.")

    stage.rule("1 · read the repo as it ships")
    p1 = detect_repo(repo)
    stage.console.print(_detect_table(p1))
    stage.beat(f"On the bare demo-repo detect finds exactly what's there: [green]{p1.test_command}[/] "
               f"(from pytest.ini), the [bold]{p1.protected_paths[0]}[/] gate dir to protect, and the "
               f"default branch [bold]{p1.default_branch}[/]. It proposes only what [italic]exists[/] — "
               f"a protected path for a directory that isn't there would be noise, not safety.")

    stage.rule("2 · the proposed loopkit.toml — and what detect refuses to fake")
    # escape: the TOML's `[agent]`/`[gate]` section headers would otherwise read as Rich markup tags.
    stage.console.print(Panel(escape(p1.to_toml()), title="proposed loopkit.toml", border_style="dim"))
    stage.beat("The scaffold is filled — adapter, gate.iteration, protected_paths, forbid_branches, the "
               "safe [bold]loopkit/run[/] branch. But [bold]goal[/] and [bold]acceptance[/] are left as "
               "placeholders on purpose: no file marker can tell detect what \"done\" means, or what the "
               "held-out oracle is. It points you at [bold]synth-gate[/] (Ch 25) instead of guessing — "
               "the copilot keeps that judgment.")

    stage.rule("3 · evidence, not a guess — add real markers and they appear")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    (repo / "migrations").mkdir()
    (repo / "poetry.lock").write_text("# lock\n")
    p2 = detect_repo(repo)
    stage.console.print(_detect_table(p2))
    stage.beat(f"Now the repo has CI, migrations, and a lockfile — and detect proposes protecting all of "
               f"them: [bold]{', '.join(p2.protected_paths)}[/]. Nothing was assumed; each path is there "
               f"because the marker is on disk. This is why detect is [italic]code, not a prompt[/] — "
               f"deterministic, auditable, and testable at zero tokens.")

    stage.beat("Default is [bold]print-only[/] — detect proposes, the molder decides. [dim]`loopkit "
               "detect --write`[/] saves it (never clobbering an existing config without `--force`); "
               "[dim]`--out profile.json`[/] emits the audit record for the unattended tier. Next in "
               "the recipe: author the held-out oracle and [bold]synth-gate[/] it (Ch 25).")


SCENARIO = Scenario(chapter=26, slug="detect",
                    title="detect: reading a repo's mechanical config deterministically",
                    teaches="a copilot molds loopkit well — except where a guess is dangerous (the test "
                            "command, the protected paths). detect reads those off file markers, at zero "
                            "tokens, and prints a proposed loopkit.toml with every fact backed by "
                            "evidence — leaving the goal + held-out oracle for the molder's judgment "
                            "(Part IV molding, Layer 3).",
                    live_supported=False, run=run)
