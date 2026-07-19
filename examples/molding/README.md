# molding/ — mold loopkit to a repo (the kit + the skill)

This folder is **both** a Claude Code skill and the templates it uses. The idea (Part IV —
[`../../docs/part-iv-molding-kit.md`](../../docs/part-iv-molding-kit.md)): setting up loopkit for a repo
is a judgment task a coding-agent copilot already does well, so we don't build a rigid auto-configurator
— we give the copilot a **playbook + verified building blocks** so it molds loopkit *consistently* and
*safely*. Point a copilot at your repo, ask it to "mold loopkit here," and it walks the recipe in
[`SKILL.md`](SKILL.md).

## What's here

| Path | Role |
|---|---|
| [`SKILL.md`](SKILL.md) | the `loopkit-mold` skill — the molding recipe (detect → DoD → oracle → gates → features → safety → review) |
| [`coverage-tiers.md`](coverage-tiers.md) | classify work → what its test must assert (the `ledger2issues.py` typed-DoD brain, generalized) |
| [`templates/issue.loopkit.toml`](templates/issue.loopkit.toml) | a per-issue molded config skeleton |
| [`templates/acceptance-dispatcher.sh`](templates/acceptance-dispatcher.sh) | the acceptance gate, chained (has-tests → held-out oracle) |
| [`templates/acceptance-oracle.sh`](templates/acceptance-oracle.sh) | a held-out oracle runner skeleton (copy-in → run → remove) |
| [`templates/acceptance-probe.sh`](templates/acceptance-probe.sh) | env-liveness probe skeleton — proves the runner is alive before fail-first is trusted |
| [`templates/proposer.sh`](templates/proposer.sh) | reference `ShellProposer` for unattended `mold-batch` — fills the oracle + probe via a headless agent (copy + edit the one per-repo gate block) |
| [`templates/judge-rubric.md`](templates/judge-rubric.md) | per-issue finding-specific REJECT criteria for the review hook |
| [`templates/worktree.sh`](templates/worktree.sh) | isolate one issue's run on a fresh base (the batch/queue shape) |

The skill **references, never duplicates**, the rest of `examples/` — the actual gate scripts live in
[`../gates/`](../gates/), best-of-N in [`../evolve/`](../evolve/), calibration + flywheel in
[`../skills/`](../skills/), CI in [`../ci/`](../ci/). This folder is the connective playbook + the
per-issue packaging that the others don't cover.

## Install it as a skill (one source of truth)

The canonical copy is here in the repo (versioned with the code it molds). To make it available in any
session, **symlink** it into your Claude Code skills dir — don't copy, so there's no drift:

```bash
ln -s "$(pwd)/examples/molding" ~/.claude/skills/loopkit-mold
```

Then in any repo: *"use loopkit-mold to set up loopkit for this issue."*

## Not the flywheel

`loopkit-mold` (this Claude Code skill) configures loopkit *before* a run. It is **unrelated** to
loopkit's *runtime* skill **flywheel** (`loopkit/extensions/skills.py` + [`../skills/`](../skills/)),
which distils lessons *across* runs. Same word "skill," different mechanism — don't conflate them.

## Status

Part IV **Layer 1** (the skill + templates — usable today, zero new code paths). Later layers add code
primitives the copilot calls: `loopkit synth-gate` (fail-first oracle verification) and `loopkit detect`
(deterministic config proposal). See [`../../docs/part-iv-molding-kit.md`](../../docs/part-iv-molding-kit.md).
