# Steering files: the control surface

loopkit is steerable by files you can read, diff, and version — not hidden state. This is the full
set, in leverage order, with what each one controls and how to write it. The principle: **the loop's
behaviour should be legible.** If you can't point at the file that made it do something, you can't
trust it unattended.

| File | Controls | Loaded | Chapter |
|---|---|---|---|
| `loopkit.toml` | the entire run envelope | once, at start | Ch 18 |
| `PROMPT.md` (anchors) | the task spec, in prose | **fresh every tick** | Ch 4–5 |
| `CLAUDE.md` / `AGENTS.md` | standing rules for the agent | every tick (as anchors) | Ch 16–17 |
| `tests/seen/` + `tests/holdout/` | the two gates — what "done" means | every tick / once before DONE | Ch 6–7, 9 |
| `<skill>.md` (skills dir) | lessons from past solved runs | every tick (rendered into prompt) | Ch 17 |

---

## `loopkit.toml` — the master config

Not markdown, but the root of all steering: the whole run as one declarative object. Every section
maps to a chapter. The fields you touch most:

```toml
goal   = "One precise sentence: what 'done' means."     # the objective
repo   = "."                                            # which repo (or --repo)
branch = "loopkit/run"                                  # the loop's own branch — never main

[agent]
adapter      = "claude-code"     # mock | claude-code | codex | claude-api | openai-api
max_cost_usd = 5.0               # budget ceiling (the loop halts here regardless of progress)
use_api_key  = false             # claude-code: false = bill your SUBSCRIPTION (an ambient
                                 # ANTHROPIC_API_KEY is withheld); true (or `run --api-key`) = bill the
                                 # API key. `loopkit doctor` prints which path is active before a run.

[prompt]
anchors = ["PROMPT.md", "CLAUDE.md"]   # the files reloaded into a fresh context each tick

[plan]                                 # OPTIONAL — plan-driven backlog mode (one loop, many items)
file = "IMPLEMENTATION_PLAN.md"        # a `- [ ]` checklist; the run isn't DONE while any item is open.
                                       # Also list it under [prompt].anchors so the agent maintains it.
                                       # Scaffold the whole thing with `loopkit init --plan`.

[gate]
iteration  = "pytest tests/seen -q"        # fast, in-sample — optimized every tick
acceptance = "pytest tests/holdout -q"     # held-out — the honest "done" check (Ch 9)
regression = "pytest tests/regression -q"  # optional second oracle — previously-passing stays green

[stops]
max_iter          = 20           # hard cap
no_progress_after = 3            # halt if N ticks change nothing
plan_stall_after  = 6            # plan mode only: halt if N ticks complete no checklist item
                                 # (no_progress watches the git tree, which a churning plan-mode
                                 # agent keeps changing; this watches the done-count). Keep < max_iter.

[safety]
protected_paths = ["tests/"]     # the loop may not touch these (so it can't game its gates)
require_clean_tree = true
allow_branches = ["loopkit/*"]
gate_stability_runs = 0          # >=2 → run the iteration gate N× on the initial tree and refuse
                                 #        to start unless every run agrees (a flaky gate corrupts the
                                 #        stop oracle, Ch 9). 0/1 = skip. `run --check-gate N` overrides.

[remote]                         # the outward edge — all off by default
enabled = false                  # nothing pushes unless true
open_pr = false
```

**Why a single file:** a typo becomes a clear validation error up front (pydantic), not a confusing
failure twenty minutes into a run. The toml *is* the run — reproducible, reviewable, diffable.

---

## `PROMPT.md` — the task spec (the highest-leverage file)

This is what the agent reads **every tick**, in a fresh context. It is the goal in prose, and it's
where most steering actually happens. The discipline (Ch 5): the loop never accumulates a long,
drifting conversation — each tick reloads `PROMPT.md` (plus feedback from the last gate), so the
spec stays the single source of truth and the agent can't wander.

Write it as a precise spec, not a chat message:

```markdown
# Task

Implement `export_csv(rows)` in `exporter.py` to this spec:

- Quote any field containing a comma, quote, or newline (RFC 4180).
- A literal quote inside a field is doubled (`"` → `""`).
- Rows are CRLF-terminated.

The visible tests are an incomplete check — passing them is necessary but not sufficient.
Make the behaviour correct. Do not weaken, delete, or skip any test.
```

**Rules of thumb:**
- State the spec, list the edge cases, name the file(s) to change.
- Tell it the visible tests are incomplete (this primes it against overfitting — Ch 9).
- Keep it stable: this file is reloaded every tick, so churn here churns the whole run.

You can anchor more than one file (`prompt.anchors`) — e.g. a `SPEC.md` alongside `PROMPT.md`.

---

## `CLAUDE.md` / `AGENTS.md` — standing rules

Conventions that apply to **every** task in this repo, not just one — anchored each tick alongside
`PROMPT.md`. This is where guardrails live:

```markdown
# Conventions

- Pure functions; type hints; round money to 2 decimals.
- Keep changes minimal and focused on the goal.
- Never edit files under `tests/` — they are the specification, not yours to change.
```

`AGENTS.md` is the cross-vendor standard format (the Ch 19 portability bet — the same file works
across harnesses). Use it for rules that should travel with the repo regardless of which agent runs.
`protected_paths` in the toml *enforces* "don't touch tests"; `CLAUDE.md` *explains* it — belt and
suspenders.

---

## The gates — `tests/seen/` vs `tests/holdout/`

The two gates are the loop's grader, and they're the real definition of "done." They're code, not
prose, because the whole point is an *objective* check the agent can't argue with.

- **`tests/seen/`** (the iteration gate) — fast, runs every tick, what the loop optimizes toward.
- **`tests/holdout/`** (the acceptance gate) — the loop **never optimizes against these**; they run
  once, on a candidate that already passed `seen`, to confirm the green is real not overfit (Ch 9).
- **`tests/regression/`** (the optional acceptance regression gate, `gate.regression`) — a *second
  held-out oracle*: `acceptance` proves the target behavior now works; `regression` proves
  previously-passing behavior was preserved. A fix that passes its target by breaking something else
  fails. Leave it unset and the acceptance gate alone certifies (exact prior behavior).

The split is the lesson demo-repo teaches: seen tests pass even with a boundary bug; only the
held-out tests catch it. **Put your edge cases and boundary conditions in `holdout/`.** Keep all of
them under `safety.protected_paths` so the loop can't make itself pass by editing the spec — this is
also the tamper defense (the agent can't touch the verifier).

You don't have to use pytest or this layout — the gates are just shell commands (`gate.iteration`,
`gate.acceptance`). Any command that exits 0 on pass works: `npm test`, `go test ./...`, a linter, a
custom script. The held-out discipline is what matters, not the tool.

---

## `<skill>.md` — the write-back flywheel

When a run reaches DONE *and* clears the write-back gate, loopkit distils what worked into a named
skill — one markdown file per skill in the skills directory — and renders it back into future runs'
prompts (Ch 17). Over many runs the repo accumulates a library of hard-won lessons that compound.

```markdown
---
name: csv-rfc4180-quoting
description: How to quote CSV fields correctly (commas, quotes, newlines)
---

Quote a field iff it contains a comma, a double-quote, or a newline. Double embedded quotes.
Terminate rows with CRLF. The boundary the held-out tests check is the empty-field-with-quote case.
```

**The one rule (Ch 9 at the meta level):** write-back is **gated**, never automatic. Reaching DONE
makes a run *acceptable*, not automatically *worth learning from* — a thin win can distil a skill
that poisons every later prompt. Only a run that clears the write-back gate mints one. Curate the
skills dir like you'd curate docs: a bad skill is worse than no skill.

---

## Putting it together — the steering hierarchy

```
loopkit.toml        →  the rules of the game (gates, stops, safety, remote)
PROMPT.md           →  THIS task          (changes per goal)
CLAUDE.md/AGENTS.md →  EVERY task's rules  (stable per repo)
tests/holdout/      →  what "done" means   (the honest grader)
skills/*.md         →  what past runs learned (accumulates)
```

Most days you edit two things: **`PROMPT.md`** (what to do) and the **held-out gate** (how you'll
know it's done). Those are the goal and the grader — get them right and the loop does the rest.
