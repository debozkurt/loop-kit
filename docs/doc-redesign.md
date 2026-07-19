# Documentation redesign — design + pickup

Status: **designed 2026-07-19; Phases 1–2 shipped, Phase 3 queued for a new session.** This is the
canonical plan for reshaping loopkit's docs + examples so they (a) index every feature/command,
(b) onboard a new project with copy-paste-easy, jr-friendly steps, and (c) double as a primer an AI
can read to understand the whole platform.

## Why (the problem)

Every current pain traces to **audience-mixing inside single files**. loopkit has three distinct
readers and no doc commits to one:

| Reader | Wants | Reads badly today because… |
|---|---|---|
| Jr operator onboarding a project | linear steps, real commands, "did it work?" | had to pick among 4 competing front doors, then wade through architecture |
| Sr engineer evaluating/extending | feature index, command surface, design *why*, the seams | the index is scattered across README + USING + molding |
| AI analyzing the platform | dense structured map, contracts, invariants, no narrative | *nothing serves it* — it must reverse-engineer structure from prose |

Secondary issues: the README is 525 lines mixing onboarding + catalog + rationale + roadmap; the
manual's `Ch N`/`Part N` cross-refs tax every page; commands/config are documented in several places
that drift; design docs are named by dev *phase* (`part-iii-ci-mode`) not *topic* (`ci-mode`); and
working-state resume docs sit among reference docs.

## The approach: split by intent (Diátaxis) + one AI primer

[Diátaxis](https://diataxis.fr) (tutorial / how-to / reference / explanation) maps cleanly onto the
three readers; add a fifth artifact for the AI reader that Diátaxis lacks.

### Target shape

```
README.md                 ~150 lines: what-it-is · install · the 5-step Quickstart · nav table · "why two gates". Nothing else.
docs/
  tutorial/               LEARNING (jr): the Quickstart expanded + the annotated walkthrough.
  how-to/                 TASKS (operator): one recipe each — sync-to-forge · from-issues · batch · ci · plan-mode
                          (today's USING-ON-YOUR-REPO.md, split into task-sized pages).
  reference/              INDEX (sr + AI): ONE command reference (every cmd+flag) · ONE config reference · the feature matrix.
  explanation/            WHY (sr + AI): two-gates · safety model · extension seams · design records (renamed by topic).
  AI-PRIMER.md + llms.txt  THE PRIMER (AI): one dense, structured map of the whole platform (spec below).
  project/                STATE (maintainers): resume docs, phase records — out of the shipped onboarding set.
examples/                 unchanged catalog, but every entry links the tutorial/how-to step that uses it.
```

### Streamline / merge / kill

- **Kill competing front doors** — one Quickstart owns onboarding; every index links to it. *(Phase 1: done.)*
- **Split the README 3:1** — front page keeps only first-contact material; command reference, config
  reference, fleet internals, observability, roadmap move to `reference/` and `explanation/`. Target ~150 lines.
- **Confine the manual to one crosswalk** — a single `explanation/manual-crosswalk.md` (chapter ↔ module
  ↔ concept) frees every other page to read standalone; that table also serves the AI reader.
- **Rename design docs by topic, not phase** — `part-iii-ci-mode.md → ci-mode.md`,
  `part-iv-molding-kit.md → molding.md`, `part-iii-agent-isolation.md → agent-isolation.md`, etc.
- **One command reference, ideally generated from Typer `--help`** so it can't drift from the code.
- **One config reference** — make the annotated `examples/gates/loopkit.example.toml` the single source;
  `CONTROL-FILES.md` references it (or CI checks them against each other).
- **Separate state from reference** — move `part-iii-resume.md` / `part-iv-resume.md` to `project/`.

### The AI primer (the novel piece — build FIRST in Phase 2)

An AI analyzing loopkit needs the opposite of good prose: **deterministic structure, canonical facts,
zero narrative.** A single `docs/AI-PRIMER.md` (+ root `llms.txt` pointing to it):

- What loopkit is, in 3 sentences (no manual refs).
- The **module map as a table**: `file → responsibility → key contract` (lift the README's module
  table, drop the chapter column, add each module's contract).
- The **invariants**, stated once — a public subset of `CLAUDE.md`'s "Invariants to preserve"
  (the `Agent`/`Gate`/`Store` contracts, the None-safe seam rule, safe-by-default, two-layer observability).
- **Command + config surface as parseable tables** (same source as `reference/`).
- **Extension seams**: where/how to plug in (`ShellProposer`/`ReviewHook` contracts, the executor seam).
- **Pointers, not prose**: "for X, read `explanation/two-gates.md`."

Why it doubles up: the qualities that make a doc good for an AI (structure, canonical facts, explicit
contracts, no dangling cross-refs) are identical to what a sr engineer wants when ramping fast. It is
also the densest truth-map for humans in a hurry — not a robots-only artifact.

## Phased plan

1. **Phase 1 — tactical consolidation. ✅ DONE (this session).** One Quickstart spine in the README
   (5 numbered steps, real commands + expected output, mold-first with `init` fallback, chapter-free);
   removed the triplicated quickstart; reframed `USING-ON-YOUR-REPO.md` as the deep reference that
   builds on the Quickstart; de-duped the `## Documentation` nav; re-pointed all four "start here"s
   (README, `docs/README.md`, `examples/README.md`) at the one spine. ~60% of the onboarding benefit.
2. **Phase 2 — `AI-PRIMER.md` + `llms.txt`. ✅ DONE (2026-07-19).** `docs/AI-PRIMER.md` — module →
   contract map (core + extensions), invariants, tick lifecycle + all 8 `StopReason` terminals,
   review semantics, the FULL command surface (core/batch/fleet/cloud incl. `review`, `measure`,
   `route`, `synth-gate`, `executor` — commands the README never listed), the config table with
   **code defaults from `config.py`** (not the README's example values), and the seam table with the
   `ShellProposer` env contract. Root `llms.txt` (llmstxt.org shape) points at it. Linked from the
   README "Where to go" nav + `docs/README.md` architecture table.
3. **Phase 3 — the Diátaxis reorg.** Larger: split the README, topic-rename the design docs, generate
   the command reference, unify the config reference, move state to `project/`. Do it when doc drift
   actually hurts, not preemptively.

## Pickup (new session)

- Read this doc first, then skim `docs/AI-PRIMER.md` (the Phase-2 result — its tables are the seed
  material for Phase 3's `reference/` layer).
- **Next: scope Phase 3 as its own deliberate change** (it touches many files — land it per the
  CLAUDE.md doc contract: docs updated in the same change as what they describe). Suggested order:
  (a) topic-rename the design docs + move resume docs to `project/` (pure moves, low risk);
  (b) split the README into `reference/` + `explanation/` (the primer's command/config tables are
  the starting point for the generated references); (c) split `USING-ON-YOUR-REPO.md` into
  `how-to/` recipes.
- Phase-2 gotcha worth keeping: the README/example-toml show *illustrative* values
  (`max_cost_usd = 5.0`, `max_iter = 12`) that differ from `config.py` defaults (10.0, 30) — any
  generated config reference must source `config.py`, and `examples/gates/loopkit.example.toml`'s
  `[review]` comment still predates the built-in default judge (fix in Phase 3's config unification).
- Convention reminder: loopkit design/state lives in this repo (not spacer memory); spacer keeps only a pointer.
