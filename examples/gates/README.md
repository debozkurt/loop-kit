# Gates & config are fully customizable — a starter catalog

The two questions every newcomer asks: *what can a gate be?* and *how much of this do I configure?*
The answer to both is **everything**. This folder is the generic, framework-agnostic starting point —
copy [`loopkit.example.toml`](loopkit.example.toml), keep what fits, point it at any repo.

## A gate is *any shell command*

That is the entire contract — there is no gate API, no plugin, no DSL:

| The command's… | …means |
|---|---|
| **exit code 0** | the gate **passed** |
| **exit code non-zero** | the gate **failed** |
| **stdout / stderr** | the **feedback** loopkit feeds back into the next tick's prompt |

So a "gate" is whatever proves *your* notion of correct. The loop optimizes toward the **iteration**
gate every tick (fast, in-sample, deterministic) and certifies DONE with the **acceptance** gate
(held-out, run once — the two-oracle pattern, Ch 9). Mix and match freely:

```toml
[gate]
# 1. a test suite (the classic)
iteration  = "python -m pytest -q"
acceptance = "python -m pytest tests/holdout -q"

# 2. a linter / type-checker / formatter-check
# iteration = "ruff check . && mypy src"

# 3. a build (compiles cleanly = plausibly right)
# iteration = "go build ./... && go vet ./..."

# 4. a structural / content check — pure shell, no framework
# iteration = "test -f CHANGELOG.md && grep -q '## Unreleased' CHANGELOG.md"

# 5. a link / doc checker for a prose or docs repo
# iteration = "markdownlint '**/*.md' && lychee --no-progress ."

# 6. an LLM-as-judge — the held-out reviewer is itself a script that shells out to a model
# acceptance = "bash gate/review.sh"   # exits 0 on ACCEPT; rubric lives in a protected path

# 7. a Makefile target that wraps any of the above
# iteration = "make verify"
```

> **Determinism matters for the *iteration* gate.** It runs every tick, so a flaky verdict corrupts
> every stop decision. Keep LLM-judged / nondeterministic checks as the **acceptance** oracle (run
> once), and probe stability before trusting a gate: `loopkit run --check-gate 5`.

## A ready-to-copy two-oracle kit

Three runnable starters in this folder — copy them into a `gate/` dir, edit, and point your config at
them. Together they are the two-oracle pattern (Ch 9) in the smallest possible form:

| File | Role | Wire it as |
|---|---|---|
| [`mechanical.sh`](mechanical.sh) | deterministic **verification** — replace the skeleton checks with your tests / lint / build / structural asserts | `gate.iteration` |
| [`review.sh`](review.sh) | **peer LLM review** — a second model scores the change's *diff* against a rubric it can't edit; `VERDICT: ACCEPT` → exit 0 | `gate.acceptance` |
| [`rubric.md`](rubric.md) | the grading criteria `review.sh` applies — make it task-specific | (read by `review.sh`) |

```toml
[gate]
iteration  = "bash gate/mechanical.sh"     # every tick — fast, deterministic
acceptance = "bash gate/review.sh"          # once before DONE — held-out peer review

[safety]
protected_paths = ["gate/"]                # so a run can't weaken review.sh OR rubric.md
```

`review.sh` is the *generic* shape of the held-out reviewer: it diffs the run's change against the base
branch and asks a fresh `claude` to judge it by the rubric — the same "a second model you don't control
certifies the work" idea, with nothing task-specific baked in. (Note: in a keyless CI/cloud context the
gate's `claude` call may not have a credential — there the **draft PR + a human reviewer** is the clean
held-out oracle; see [`../ci/`](../ci/).)

## …and the config is the whole loop, declaratively

Every other knob is in the same file — the goal, the fixed-context anchors, the budget + adapter,
the three hard stops, and the **safety envelope** (protected paths, branch allow/deny, clean-tree).
[`loopkit.example.toml`](loopkit.example.toml) annotates all of them. Start there, or scaffold a
minimal one with `loopkit init`, then tighten gate-by-gate.

**Protect your verifier.** Put the gate's files (`tests/`, `gate/`, an `evals.py`) under
`safety.protected_paths` so a run can't "pass" by weakening its own grader (Ch 9 — verifier hacking).
That single line is what makes an autonomous gate trustworthy.
