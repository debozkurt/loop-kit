# loopkit docs — index

**New here? Start with the [README Quickstart](../README.md#quickstart--from-zero-to-a-merged-fix)** —
five steps from install to a merged fix. This index is everything past that: the **operator guides**
for going deeper on your repo, the **architecture wiki** for how it's built, and the **phase
references** (design records + current-phase resume doc).

> These are loopkit's **operator + design** docs. The *concepts* live in the paired
> [*Agentic Loops* manual](https://github.com/debozkurt/loop-guide) — loopkit is its reference
> implementation, and the chapter numbers across these docs map to it.

## 🧭 Operator guides — using loopkit on your repo

| Doc | When |
|---|---|
| [`USING-ON-YOUR-REPO.md`](USING-ON-YOUR-REPO.md) | point loopkit at your own repo, end to end |
| [`../examples/`](../examples/) | the four runnable example dirs, mapped (walkthrough · demo-repo · gates · CI) |
| [`../examples/walkthrough/`](../examples/walkthrough/) | **copy-this-run-it-see-DONE** on the bundled demo repo |
| [`CONTROL-FILES.md`](CONTROL-FILES.md) | every `loopkit.toml` / `PROMPT.md` knob, annotated |
| [`../examples/gates/`](../examples/gates/) | gates are *any shell command* — ready-to-copy two-oracle kits (test + docs + LLM-review) |
| [`BILLING.md`](BILLING.md) | **which credential pays** — subscription default, `--api-key`, budget-on-subscription |
| [`OPERATING.md`](OPERATING.md) | drive a run: silent-vs-hung, never edit a live tree, resume, output per tier |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | `cost: $0.00`, `rc=1`, `safety_halt`, dirty tree, flaky gate, `AF_UNIX too long` |

## 🏗️ Architecture — how it's built / designed

| Doc | Covers |
|---|---|
| [`architecture/`](architecture/README.md) | the living architecture wiki (system today + the module-ownership map, cloud, adapters/auth, security) — start with its README |
| [`../examples/ci/`](../examples/ci/) | the CI deployment tier templates (claude-api + claude-code/OAuth) |

## 📋 Phase references & design records

These are **living references**, not archived history — the architecture wiki delegates the deep
detail to them. Read in this order:

- **Start here (current phase):** [`part-iii-resume.md`](part-iii-resume.md) — state, locked
  decisions, sharp edges, next step.
- **The three-tier teaching view:** [`part-iii-ecosystem.md`](part-iii-ecosystem.md) — local · CI ·
  cloud, as a lesson.
- **Design records (the canonical detail behind the wiki):**
  [`part-iii-ci-mode.md`](part-iii-ci-mode.md) (the CI tier) ·
  [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md) (the keyless-executor split) ·
  [`part-iii-skills-repo.md`](part-iii-skills-repo.md) (the cross-run skills repo).
- **Cross-cutting:** [`part-iii-security-review.md`](part-iii-security-review.md) (the adversarial
  full-flow review — canonical) · [`part-iii-prior-art.md`](part-iii-prior-art.md) (canonical
  harnesses mapped to loopkit's design).

## 🗄️ Closed phases (archived)

Completed-phase records, kept for reference in [`archive/`](archive/):

- [`archive/part-ii-resume.md`](archive/part-ii-resume.md) — Part II (library + dev fleet), done.
- [`archive/part-ii-tilt-fleet-plan.md`](archive/part-ii-tilt-fleet-plan.md) — the dev kind/Tilt
  fleet build plan, built + run live.
