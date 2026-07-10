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

- **Layer 1 BUILT** (committed `b571d8f`): `examples/molding/` — the `loopkit-mold` Claude Code skill
  (`SKILL.md`), `coverage-tiers.md` (the `ledger2issues.py` typed-DoD brain, generalized), and
  `templates/` (per-issue config · acceptance dispatcher · oracle skeleton · judge rubric · worktree
  recipe). It **references, not duplicates** `examples/gates|evolve|skills|ci`. Zero new code paths — a
  skill + templates a copilot uses today. Install: `ln -s "$(pwd)/examples/molding" ~/.claude/skills/loopkit-mold`.
- **Layer 2 BUILT** (uncommitted at time of writing): **`loopkit synth-gate`** — fail-first oracle
  verification, the load-bearing half of oracle synthesis (roadmap #1). New `extensions/synth_gate.py`
  (`verify_oracle` → `OracleVerdict`/`OracleCheck`, self-contained: stdlib + the core
  `executor.run_gate`, **no fleet coupling** — the `measure.py` shape) + the `synth-gate` CLI command in
  `cli/local.py` (lazy import; ORACLE positional defaults to the config's `gate.acceptance`;
  `--fix`/`--isolate`/`--mode`/`--repo`/`--out`; exit **0 = blessed, 3 = not real**) + `demo 25`
  (`scenarios/ch25_synth_gate.py`) + `tests/test_synth_gate.py` (16 tests, no tokens: fake-runner logic
  + a real fail→pass over the demo-repo + the CLI exit-code contract) + the surface-snapshot entry.
  - **What it checks:** *fail-first* (mandatory) — the oracle must FAIL on the current (buggy) tree, so
    passing it later is real evidence; and, given a reference `--fix`, *pass-on-fix* (gold) — apply the
    fix to an isolated copy and the oracle must PASS (SWE-bench FAIL_TO_PASS validation), proving it
    *discriminates* buggy-from-fixed rather than failing for some unrelated reason. Blessed iff every
    check holds; the verdict is an auditable provenance record (oracle + fix + signature + version + ts).
  - **Two failure modes it catches:** an already-green oracle (reproduces nothing, certifies DONE on
    tick zero) and an unsatisfiable one (fails forever, burns the budget). Fail-first alone can't tell
    "fails for the right reason" from "fails because broken" — that's why the CLI always surfaces the
    fail-first output, and why `--fix` is the real answer when a reference fix exists.
  - Generalizes the `run --validate` pre-loop seam; `examples/gates/validate.sh` still does the raw
    preflight, `synth-gate` supersedes it for verifying an oracle *at molding time*. SKILL.md step 3 +
    the oracle template now cite the command.

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

**Layer 3 — `loopkit detect`** (deterministic repo introspection): file-marker heuristics decide the
*mechanical, safety-critical* config a copilot must not guess — test runner (`pyproject.toml`/`tox.ini` →
pytest, `package.json` `scripts.test`, `go.mod` → `go test`, a `Makefile` target), protected-path
candidates (CI/chart/migration/lockfile paths + the gate files themselves), the default branch, and which
adapter is on `PATH` — and **prints** a proposed `loopkit.toml` (`--write` opt-in, never overwriting an
existing config without `--force`; it proposes, the molder decides). Build as an **extension** + a
lazy-imported CLI command (the same `synth-gate`/`measure` shape); deterministic core ⇒ testable at zero
tokens. Then Layer 4 reliability-gated routing (wire `measure` → `evolve` escalation into the playbook +
a thin helper), Layer 5 the unattended molding step (generalize the spacer `sequencer.py`/`ledger2issues.py`).

## Sharp edges to carry

- **Don't duplicate the copilot.** Every layer must be a *verified primitive* or *packaging* the copilot
  can't self-supply — not "an LLM writes your config/checklist." If a layer is just an LLM call, it belongs
  in the skill, not in code.
- **Fail-first is the load-bearing half of oracle synthesis** — proving the oracle fails on the buggy
  tree is the value; proposing the test is easy. Layer 2 (`synth-gate`) is the *verification*, not the
  generation — it never writes a test. `--fix` completes it: fail→pass proves the oracle *discriminates*,
  which fail-first alone can't (a broken oracle fails for free). Future layers keep this line: a layer
  that's just "an LLM writes your X" belongs in the skill, not in code.
- **`examples/ci/` is drift-guarded** (`test_ci` byte-equality) — don't edit those templates casually.
- **Keep it killer, not bloated** (the roadmap invariant): resist growing a general config-generation
  framework. The kit is a skill + templates + two small extensions.

## Relationship to Part III

Part III (cloud) is not superseded — its open items (Security E/F, observability, KEDA, ESO/Vault, GitHub
App) still stand; see [`part-iii-resume.md`](part-iii-resume.md). Part IV is a parallel product direction.
Layer 5 (the unattended molding step) is where the two meet — it makes the CI/fleet tiers self-configure.
