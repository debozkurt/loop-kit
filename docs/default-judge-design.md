# Built-in default judge — design & build plan

**Status:** planned. The observability groundwork shipped in the `feat/review-clarity` branch
(PR #18): `ReviewConfig.decide()` returns a `ReviewDecision(command, reason)`, and there is a
`TODO(default-judge)` branch in `decide()` where this feature plugs in. Read this doc cold and build it.

## Why

Review should be **on by default, off only when expressly disabled** — so a quality gate is never
silently skipped (the failure mode that let review fire in zero of 28 batch runs). "On by default"
only means something if there is *something to run* when no `[review] command` is configured. That
"something" is the **built-in default judge**: loopkit ships a generic adversarial reviewer that runs
out of the box, is enrichable, and is fully overridable.

## Locked design decisions

| Fork | Decision |
|---|---|
| **Backend** | Default to the session's `[agent]` backend (`claude-code`→`claude`, `codex`→`codex`); overridable via `[review] backend` (and/or a CLI flag). Lets you loop with Claude but judge with Codex for cross-model blind-spot diversity. |
| **Model** | Match the coding agent's model (`[agent].model`) by default; `[review] model` overrides. |
| **Verdict** | **Real defects only** — REJECT for correctness bugs, security, incomplete fix (sibling sites), gaming/weakened tests, trivially-passing tests, contract breaks. Style/nits are advisory, never block. |
| **Mold** | **Layered** — bundled generic criteria + optional project criteria + mold's per-task rubric, composed. Plain `run` still gets the generic judge. |

Independence-by-default comes from a **fresh, clean-context, adversarial, read-only** pass (no shared
memory with the coder), *not* from a different model — so it works with one backend, and true model
diversity is one override away.

## Architecture

New module `loopkit/extensions/judge.py`:

```
DEFAULT_REVIEW_CRITERIA: str          # the bundled generic adversarial checklist (real-defects-only)
JudgeVerdict(passed: bool, reason: str, raw: str)

build_judge_prompt(diff, extra_criteria: list[str]) -> str
run_judge(workspace, *, backend, model, base, extra_criteria=(), runner=None) -> JudgeVerdict
    # 1. diff = git diff base...HEAD (fall back to HEAD~1..HEAD)   — empty diff ⇒ APPROVE by vacuity
    # 2. prompt = DEFAULT_REVIEW_CRITERIA + extra_criteria + diff + verdict instruction
    # 3. out = (runner or _run_backend)(prompt, backend, model, workspace)   — runner injectable for tests
    # 4. return _parse_verdict(out)   — last `VERDICT: APPROVE|REJECT — …`; NO verdict ⇒ fail-closed REJECT

class DefaultReviewHook(ReviewHook):  # implements review(workspace, commit_message) -> GateResult
    # built from (ReviewConfig, AgentConfig): derives backend/model, reads [review] criteria/prompt files
```

**Backend dispatch** (`_run_backend`): reuse the adapter binaries. `claude-code`/`claude` → `claude -p
<prompt> [--model M]`; `codex` → `codex -p <prompt> [--model M]` (matches `_CLIAdapter._command`).
Scrub env with `secrets.current().child_env(add=<backend cred keys>)` — claude = subscription keys,
codex = `OPENAI_API_KEY` (see `secrets.ADAPTER_KEYS` / `CLAUDE_CODE_SUBSCRIPTION_KEYS`). The judge is
**read-only**: the diff is embedded in the prompt, so it needs no tools and touches no files.

`DEFAULT_REVIEW_CRITERIA` — product-agnostic, real-defects-only. BLOCK on: correctness bug on a
reachable input; security (authz gap, injection, committed secret, sensitive data logged, forgeable
trust boundary); incomplete fix (a sibling instance of the same bug left unfixed — name it); gaming
(deleted/weakened/skipped test, loosened assertion, gate/CI edit, test-input special-casing);
trivially-passing test (passes against the OLD buggy code); contract break (renamed/removed field,
changed status/signature the goal didn't ask for). Do NOT block on formatting/naming/structure — note
as advisory. End with exactly `VERDICT: APPROVE` or `VERDICT: REJECT — <reason citing file:line>`.

## Config surface (`ReviewConfig`)

```
enabled: bool = True          # master switch (already present)
command: str | None = None    # custom judge; None ⇒ built-in default judge (already present)
backend: str | None = None    # NEW: override; default derives from [agent].adapter
model:   str | None = None    # NEW: override; default = [agent].model
criteria: list[str] = []      # NEW: project criteria file(s) appended to the bundled prompt
```

`decide()` — replace the `TODO(default-judge)` branch. `ReviewDecision` grows a `kind`
(`"off" | "command" | "default"`), because the default judge is not a shell command:

```
if disabled:            off,     "--no-review"
if override is not None: command, override            # explicit --review / manifest review=
if not enabled:         off,     "disabled (enabled=false)"
if command is not None: command, "[review] command"
else:                   default, "built-in judge (<backend>/<model>)"
```
(Keep `--no-review` and override ahead of the `enabled` gate — the current, non-breaking precedence.)

## Call-site wiring

`cli/local.py` (run) and `extensions/batch.py` (batch) already call `decide()` + log the reason.
Change the hook construction:

```
if decision.kind == "command":  hook = ShellReviewHook(decision.command)
elif decision.kind == "default": hook = DefaultReviewHook(cfg.review, cfg.agent)   # both have cfg
else:                            hook = None
```

Both sites have the full `cfg` (so `cfg.agent` for backend/model derivation). Update the run-line /
batch-log reason to name the backend+model when kind == default.

`cli/local.py` doctor: the `review` row already renders on/off; when kind == default, show
`on — built-in judge (<backend>/<model>)`.

## Mold layering (Phase 2)

Mold already carries a per-task `review =` command through to the emitted `batch.toml` (see
`extensions/mold.py`, `part-iv-molding-kit.md`). For the layered default:
- Mold generates a per-task **rubric** file (like it generates the oracle) — the task-specific REJECT
  criteria co-derived from the goal.
- Mold wires it as `[review] criteria = ["<task>/rubric.md"]` in the emitted per-task config (or passes
  `--criteria` if the built-in judge is exposed as a `loopkit review` subcommand).
- `DefaultReviewHook` reads `cfg.review.criteria` and appends each file to the bundled prompt.
- Result: bundled generic criteria + project criteria + per-task rubric, composed. Plain `run` gets
  just the generic judge; batch/mold users get task-specific teeth.

Optional: expose the judge as a `loopkit review [--backend B --model M --criteria F...]` subcommand so
it's usable as a plain `[review] command` too, and mold can wire an explicit command. Keeps one impl:
the subcommand and `DefaultReviewHook` both call `run_judge`.

## Testing (same commit)

- **No real CLI calls** — inject `runner=` into `run_judge` returning a canned judge transcript.
- `_parse_verdict`: APPROVE / REJECT / no-verdict (⇒ fail-closed) / multiple verdicts (last wins).
- `build_judge_prompt`: diff embedded, extra_criteria appended in order, empty diff ⇒ APPROVE-by-vacuity.
- `decide()`: the three kinds (off/command/default) + precedence unchanged.
- Backend/model derivation from `AgentConfig` (claude-code→claude, codex→codex; model passthrough).
- `DefaultReviewHook.review` returns a clean `GateResult` (pass → no feedback; fail → reason).
- `doctor` review row shows `built-in judge (...)` when kind == default.
- `test_cli_surface` EXPECTED update if a `loopkit review` subcommand is added.

## Open questions / risks to decide while building

1. **Base ref for the diff.** Cumulative (`base...HEAD`, catches cross-tick interactions — what
   review-judge.sh did) vs. last commit (`HEAD~1..HEAD`). Lean cumulative vs the branch fork point,
   with a robust fallback to `HEAD~1..HEAD` when the base is unknown.
2. **Cost visibility.** The default judge is a model call per advancing tick. Surface per-review cost
   (parse usage like the adapters do, or at least log a count) so on-by-default isn't a silent spend.
   `doctor` should note "review runs a model call per tick."
3. **Read-only hardening.** The diff-in-prompt design needs no tools, but consider passing
   tool-restricting flags to the backend defensively so a future prompt change can't let the judge edit.
4. **Availability.** If the derived backend binary isn't on PATH / not authed, the judge fails closed
   (blocks DONE). `doctor` should probe the backend binary and warn early, rather than failing mid-run.
5. **Verdict parsing robustness across backends.** Claude and Codex format output differently; parse the
   `VERDICT:` line from the tail; fail-closed on absence. Keep it backend-agnostic.

## Files to touch

`loopkit/extensions/judge.py` (new) · `loopkit/config.py` (`ReviewConfig` fields + `decide()` kind) ·
`loopkit/cli/local.py` (run hook build + doctor row) · `loopkit/extensions/batch.py` (hook build) ·
`loopkit/extensions/mold.py` (Phase 2 rubric) · `tests/test_review.py` + a new `tests/test_judge.py` ·
`README.md` + this doc.
