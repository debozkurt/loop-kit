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

## Current state (2026-07-16)

- **Layer 5 BUILT** (branch `feat/batch-cli`): **`loopkit mold-batch`** (`extensions/mold.py` + CLI +
  `demo 29` + `tests/test_mold.py`, 18 tests no tokens) — unattended batch molding per the design's
  crux resolution: the coverage-tier→typed-DoD table carries the *mechanical* half of oracle proposal
  in code (`TIER_ASSERTIONS`), a **`ShellProposer` seam** carries the *judgment* half (mechanical-only
  stops at an annotated skeleton, `needs-oracle`), `synth-gate` verification is **mandatory +
  isolated**, and the terminal artifact is a reviewable `batch.toml` — never a run. Knobs: `--level`
  (detect < oracle < route < full) × `--limit` (+ state.json resume; successes skip, failures retry).
- **Companion runner BUILT** (same branch): **`loopkit batch`** (`extensions/batch.py` + CLI +
  `demo 28` + `tests/test_batch.py`, 21 tests no tokens) — the roadmap's no-infra parallel batch:
  a TOML manifest over the in-process fleet (`InMemoryQueue` + `run_workers`), one isolated clone +
  branch (+ draft PR) per task, conflict-aware scheduling (`group` serializes, `after` gates +
  cascading skips), per-task configs + the `--review`/`--validate` seams. `WorkerOutcome` gained an
  additive `pr_url`; `to_run_result` now tolerates non-terminal reasons.
- **The pytest CI lane exists** (`.github/workflows/tests.yml`) — the suite is no longer local-only;
  it runs on every push/PR with the `dev` extra alone (3.11 + 3.12).


- **Layer 1 BUILT** (committed `b571d8f`): `examples/molding/` — the `loopkit-mold` Claude Code skill
  (`SKILL.md`), `coverage-tiers.md` (the `ledger2issues.py` typed-DoD brain, generalized), and
  `templates/` (per-issue config · acceptance dispatcher · oracle skeleton · judge rubric · worktree
  recipe). It **references, not duplicates** `examples/gates|evolve|skills|ci`. Zero new code paths — a
  skill + templates a copilot uses today. Install: `ln -s "$(pwd)/examples/molding" ~/.claude/skills/loopkit-mold`.
- **Layer 2 BUILT** (uncommitted): **`loopkit synth-gate`** — fail-first oracle
  verification, the load-bearing half of oracle synthesis (roadmap #1). New `extensions/synth_gate.py`
  (`verify_oracle` → `OracleVerdict`/`OracleCheck`, self-contained: stdlib + the core
  `executor.run_gate`, **no fleet coupling** — the `measure.py` shape) + the `synth-gate` CLI command in
  `cli/local.py` (lazy import; ORACLE positional defaults to the config's `gate.acceptance`;
  `--fix`/`--isolate`/`--mode`/`--repo`/`--out`; exit **0 = blessed, 3 = not real**) + `demo 25`
  (`scenarios/ch25_synth_gate.py`) + `tests/test_synth_gate.py` (16 tests, no tokens: fake-runner logic
  + a real fail→pass over the demo-repo + the CLI exit-code contract) + the surface-snapshot entry.
- **Layer 3 BUILT** (uncommitted): **`loopkit detect`** — deterministic repo introspection → a
  **proposed** `loopkit.toml`. New `extensions/detect.py` (`detect_repo` → `RepoProfile`/`Detection`,
  the most standalone primitive: **stdlib-only, no core/executor/fleet coupling** — pure filesystem+git
  introspection) + the `detect` CLI command in `cli/local.py` (lazy import; positional REPO defaults to
  `.`; `--write`/`--force`/`--out`; always exit 0 — it's introspection, the confidence table tells the
  story) + `demo 26` (`scenarios/ch26_detect.py`) + `tests/test_detect.py` (30 tests, no tokens: each
  marker heuristic over synthetic trees with `which` injected + the TOML round-trips into a real
  `Config` + the CLI contract) + the surface-snapshot entry.
  - **What it reads (off file markers, at zero tokens):** the **test runner** →
    `[gate].iteration` (`pyproject.toml`/`pytest.ini`/`tox.ini` → pytest; a real `package.json`
    `scripts.test` → `<pm> test` [pnpm/yarn/npm by lockfile]; `go.mod` → `go test ./...`; `Cargo.toml` →
    `cargo test`; a `Makefile` `test:` target → `make test`; first present in that fixed priority wins,
    the rest recorded as `test-runner-alt`); the **protected-path candidates** → `[safety]` (the first
    test dir that exists — `tests`/`test`/`spec` — so the loop can't weaken its own gate, plus CI, chart,
    migration, lockfile paths, *existing only*); the **default branch** → augments `forbid_branches`
    (`origin/HEAD` → local `main`/`master` → HEAD); the **adapter** (`claude`→claude-code / `codex`→codex
    on PATH, else claude-code LOW). Every fact is a `Detection(key, value, evidence, confidence)`.
  - **The boundary it holds (the Part IV line):** it fills only the mechanical scaffold and leaves the
    two judgment fields no marker can read — `goal` and `gate.acceptance` (the held-out oracle) — as
    annotated placeholders that point at `synth-gate`. detect **proposes, it does not decide** (print by
    default; `--write` never clobbers an existing config without `--force`). A layer that's just "an LLM
    writes your X" belongs in the skill, not in code — detect writes no prose and fakes no oracle.
  - Generalizes step 1 of the `loopkit-mold` recipe (SKILL.md now runs `detect` first instead of
    hand-inspecting the markers).
- **Layer 4 BUILT** (committed): **`loopkit route`** — reliability-gated routing (`measure` pass^k → a
  single-run-vs-`evolve` decision). New `extensions/route.py` (`decide_route` → `RouteDecision` +
  `size_population` + `route_from_report`; **stdlib, reuses `measure`'s pass^k/pass@k estimators**, no
  core/fleet coupling) + the lazy-imported `route` CLI (`--from-report` = the free no-run path, else
  calibrate inline via `measure`; `--threshold`/`--k`/measure-parity flags; `--out` provenance; always
  exit 0) + `demo 27` (`ch27_route.py`) + `tests/test_route.py` (21 tests, no tokens) + surface entry.
  - **The rule:** `pass^k ≥ threshold` ⇒ **single** run; below ⇒ **evolve**, population sized so
    `1−(1−p)^N` (discovery at base rate p) clears a target, capped at 8. **Default k=1** (the base rate
    `c/n`, graded) — NOT `k=trials`, which is degenerate (pass^k at k=n is 1.0 iff every trial passed).
    `pass^1 == 0` ⇒ evolve-at-cap but **flagged honestly** (escalation can't manufacture a capability the
    loop never once showed — the sharp edge: fix the goal/gates/oracle or the model instead).
  - **Advisory** (prints the strategy + the turnkey `loopkit fleet evolve -g N -p N -k N` command, never
    launches an evolve) — matching the kit's proposes-not-decides posture. The `RouteDecision` carries
    the measurement's harness signature + a decision signature, so a routing choice is tied to the exact
    numbers it came from. SKILL.md step 5 (feature-routing table) now cites the command.
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

**All five layers are built** (L1 skill · L2 synth-gate · L3 detect · L4 route · L5 mold-batch +
the `loopkit batch` runner), and the pytest CI lane closed the local-only-gate gap. What remains is
exactly the dogfood the design demanded:

1. **Run the integrated flow on a real external repo** — a genuine batch (`mold-batch` at each level
   → review the molded instances → `loopkit batch`) against a repo that isn't the bundled demo. The
   unit tests prove each stage; only a real batch proves the *proposal* quality (how often does a
   proposer-filled oracle survive fail-first? how often does the tier table misclassify?). Feed what
   breaks back into `TIER_ASSERTIONS`, the skeleton, and the `ShellProposer` contract.
2. **Calibrate before trusting a batch class** — `measure --out` one representative task per class,
   feed the reports to `mold-batch` via each task's `report` field so `route` decides on real pass^k
   instead of defaulting to single-uncalibrated.
3. **Then the deferred niceties** as need proves them: per-item draft PRs for plan mode, a `--proposer`
   reference implementation in `examples/` (headless-agent oracle author), tree-level evolve reseed.

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
  framework. The kit is a skill + templates + three small extensions (`synth_gate`, `detect`, `route`).
- **Layer 5 stays a *batch* feature — don't generalize it.** It was built because batch remediation
  became a real target; for everyday issue→PR the CI tier still suffices. Its boundaries are the
  design's: mechanical-only never fakes an oracle (skeletons stop at `needs-oracle`), the proposer is
  a seam (never bundled judgment), verification is mandatory + isolated, and `mold-batch` never runs
  a loop — the human checkpoint is the seam into `loopkit batch`. Resist collapsing the two commands
  into one "mold-and-run": that seam IS the trust surface.

## Relationship to Part III

Part III (cloud) is not superseded — its open items (Security E/F, observability, KEDA, ESO/Vault, GitHub
App) still stand; see [`part-iii-resume.md`](part-iii-resume.md). Part IV is a parallel product direction.
Layer 5 (the unattended molding step) is where the two meet — it makes the CI/fleet tiers self-configure.
