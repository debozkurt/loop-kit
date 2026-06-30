# Examples — four folders, four different questions

These aren't four versions of the same thing. Each answers a distinct question, so pick by what you
want *right now* rather than reading all four:

| Folder | The question it answers | Runnable? | Remote (push/PR) |
|---|---|---|---|
| [`walkthrough/`](walkthrough/) | *Just show me the loop reach `DONE`.* Paste-these-commands narration over the demo fixture. | ✓ (a script you run) | off |
| [`demo-repo/`](demo-repo/) | *What does a real task look like?* The **fixture** — a tiny Python repo with a planted bug and a held-out test split (`tests/seen` vs `tests/holdout`), the two-oracle gate in its simplest form (Ch 6–9). | ✓ `loopkit doctor && loopkit run` | off |
| [`gates/`](gates/) | *What can a gate be, and what are all the knobs?* The annotated **reference catalog** — [`loopkit.example.toml`](gates/loopkit.example.toml) (every field, commented) plus a set of example gate scripts. | ✗ (read + copy, not run as-is) | documented (commented, off) |
| [`ci/`](ci/) | *How do I deploy it to my forge?* The CI workflow templates (GitHub/GitLab × API-key/claude-code) — a labelled issue becomes a draft PR. The middle of loopkit's three tiers (*local · **CI** · cloud fleet*). | ✓ (in CI) | **on** — `--open-pr` opens a real draft PR |

**Start here:** new to loopkit → [`walkthrough/`](walkthrough/). Setting up your own repo →
copy [`gates/loopkit.example.toml`](gates/loopkit.example.toml) and keep what fits (or run
`loopkit init`). Wiring it into CI → [`ci/`](ci/).

## On the two config files (a common point of confusion)

[`gates/loopkit.example.toml`](gates/loopkit.example.toml) and
[`demo-repo/loopkit.toml`](demo-repo/loopkit.toml) are **different artifact types, not duplicates**:

- the **gates** one is an annotated *reference* — every knob, heavily commented, points at a generic
  `tests/`. You read it and copy the lines you need; it is not meant to run as-is.
- the **demo-repo** one is the *runnable fixture* — deliberately terse (no comment walls) because you
  **run** it. It points at the demo's real `pricing.py` + `tests/seen`/`tests/holdout`.

## Where remote (push + PR) is exemplified

Remote sync is **environment-specific** — it needs a real remote URL plus `gh`/`glab` authenticated,
which no copy-paste fixture can supply. So it shows up in two honest places, and is deliberately
**off** in the runnable fixtures (a cloner has no `origin`/auth, so enabling it would just fail at the
outward edge):

- the static `[remote]` block is **documented** (commented out) in
  [`gates/loopkit.example.toml`](gates/loopkit.example.toml);
- it **actually fires** in [`ci/`](ci/), whose workflows pass `--open-pr` to open a real draft PR.

For a one-off without a `[remote]` block, `loopkit run --open-pr` flips the same switches for a single
run — see the root [`README.md`](../README.md) "Sync the result" section.
