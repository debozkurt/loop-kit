# Part IV — molding loopkit to a repo — resume

The single next-session pointer for the **molding kit** work. Current state + load-bearing context +
next step; history lives in `git log`. Design of record: [`part-iv-molding-kit.md`](part-iv-molding-kit.md).

## The thesis (one paragraph)

loopkit is autonomous at *execution*, not *configuration*. Setting it up for a repo is a judgment task a
Claude/Codex **copilot** already does well — so we do **not** build an auto-configurator monolith (that
would duplicate the copilot and bloat loopkit). Instead loopkit ships a **playbook + verified building
blocks** the copilot molds *with*: deterministic detection, fail-first oracle verification, the per-issue
packaging. The same kit serves the no-copilot CI/fleet tiers, where the molder is the triggering agent.
The spacer remediation harness is the existence proof (a human hand-built exactly this kit for 60+
findings). **The molder is the copilot; loopkit supplies the determinism, verification, and provenance
the judgment can't self-supply.**

## Current state (2026-07-10)

- **Layer 1 BUILT** (uncommitted at time of writing unless the next line says otherwise): `examples/molding/`
  — the `loopkit-mold` Claude Code skill (`SKILL.md`), `coverage-tiers.md` (the `ledger2issues.py`
  typed-DoD brain, generalized), and `templates/` (per-issue config · acceptance dispatcher · oracle
  skeleton · judge rubric · worktree recipe). It **references, not duplicates** `examples/gates|evolve|skills|ci`.
  Validated: shell `bash -n` clean, TOML/frontmatter parse, all cross-links resolve, drift guards green.
- Zero new code paths — Layer 1 is a skill + templates a copilot uses today. Install:
  `ln -s "$(pwd)/examples/molding" ~/.claude/skills/loopkit-mold`.

## Decisions locked (with the maintainer)

- **Command surface:** the molder is the copilot + the `loopkit-mold` skill; loopkit's own surface is the
  `detect` + `synth-gate` **primitives** (Layers 2–3). The earlier `onboard`/`plan` two-verb sketch is
  **retired**.
- **Trust model:** always human-review-gated (the molding proposal is reviewed before any run). Untrusted
  goals (CI) ⇒ fail-first verification mandatory + the standing guardrails.
- **Introspection:** hybrid — deterministic file-marker detection for the safety-critical config, LLM only
  for prose/gaps.
- **Skill home:** canonical in `examples/molding/`; global via a **symlink**, not a second copy; named
  `loopkit-mold` (distinct from loopkit's runtime skill *flywheel*).
- **`detect` output:** prints a proposed `loopkit.toml` by default (`--write` opt-in); it proposes, the
  molder decides.

## Next step

**Layer 2 — `loopkit synth-gate`** (fail-first oracle verification): take a proposed acceptance oracle →
run it against the current tree → assert it FAILS → only then bless it. This is roadmap #1 (oracle
synthesis) — the highest-value, least-duplicative primitive, and the #1 real-use friction (hand-authoring
a fail-first oracle). Build as an **extension** (`extensions/` + a lazy-imported CLI command, the `measure`
pattern), reusing the core gate-running machinery + the `--validate` seam; add a demo/learn lab and tests
(MockAgent + a real fail→pass fixture, no tokens). Then Layer 3 `loopkit detect`, Layer 4 reliability-gated
routing, Layer 5 the unattended molding step (generalize the spacer `sequencer.py`/`ledger2issues.py`).

## Sharp edges to carry

- **Don't duplicate the copilot.** Every layer must be a *verified primitive* or *packaging* the copilot
  can't self-supply — not "an LLM writes your config/checklist." If a layer is just an LLM call, it belongs
  in the skill, not in code.
- **Fail-first is the load-bearing half of oracle synthesis** — proposing a test is easy; proving it fails
  on the buggy tree is the value. Layer 2 is the *verification*, not the generation.
- **`examples/ci/` is drift-guarded** (`test_ci` byte-equality) — don't edit those templates casually.
- **Keep it killer, not bloated** (the roadmap invariant): resist growing a general config-generation
  framework. The kit is a skill + templates + two small extensions.

## Relationship to Part III

Part III (cloud) is not superseded — its open items (Security E/F, observability, KEDA, ESO/Vault, GitHub
App) still stand; see [`part-iii-resume.md`](part-iii-resume.md). Part IV is a parallel product direction.
Layer 5 (the unattended molding step) is where the two meet — it makes the CI/fleet tiers self-configure.
