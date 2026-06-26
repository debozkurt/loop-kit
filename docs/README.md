# loopkit docs — index

Start with the **operator guides**; drop into the **architecture wiki** for how it's built; the
**project/phase docs** are development history and design records.

## 🧭 Operator guides — using loopkit on your repo

| Doc | When |
|---|---|
| [`USING-ON-YOUR-REPO.md`](USING-ON-YOUR-REPO.md) | point loopkit at your own repo, end to end |
| [`../examples/walkthrough/`](../examples/walkthrough/) | **copy-this-run-it-see-DONE** on the bundled demo repo |
| [`CONTROL-FILES.md`](CONTROL-FILES.md) | every `loopkit.toml` / `PROMPT.md` knob, annotated |
| [`../examples/gates/`](../examples/gates/) | gates are *any shell command* — ready-to-copy two-oracle kits (test + docs + LLM-review) |
| [`BILLING.md`](BILLING.md) | **which credential pays** — subscription default, `--api-key`, budget-on-subscription |
| [`OPERATING.md`](OPERATING.md) | drive a run: silent-vs-hung, never edit a live tree, resume, output per tier |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | `cost: $0.00`, `rc=1`, `safety_halt`, dirty tree, flaky gate, `AF_UNIX too long` |

## 🏗️ Architecture — how it's built / designed

| Doc | Covers |
|---|---|
| [`architecture/`](architecture/README.md) | the living architecture wiki (system today, cloud, adapters/auth, security) |
| [`../examples/ci/`](../examples/ci/) | the CI deployment tier templates (claude-api + claude-code/OAuth) |

## 📋 Project / phase records (development history)

The current-phase source of truth + design records and resume docs:

| Doc | Covers |
|---|---|
| [`part-iii-resume.md`](part-iii-resume.md) | **current phase** — state, decisions, next step, changelog |
| [`part-ii-resume.md`](part-ii-resume.md) | the prior phase (library + dev fleet) |
| [`part-iii-ci-mode.md`](part-iii-ci-mode.md) · [`part-iii-ecosystem.md`](part-iii-ecosystem.md) | the CI tier + the three-tier ecosystem teaching module |
| [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md) · [`part-iii-skills-repo.md`](part-iii-skills-repo.md) | the keyless-executor split + the cross-run skills repo |
| [`part-iii-security-review.md`](part-iii-security-review.md) · [`part-iii-prior-art.md`](part-iii-prior-art.md) | the adversarial review + the prior-art survey |
| [`tilt-fleet-plan.md`](tilt-fleet-plan.md) | the dev kind/Tilt fleet plan |
