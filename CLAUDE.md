# loopkit — working instructions

`loopkit` is a self-governed coding loop you can point at any repository: give it a goal and two
gates, and it drives a coding agent toward the goal tick by tick with guardrails (external
verification gate, held-out acceptance gate, three hard stops, durable git state, a blast-radius
safety envelope). It runs as a single loop, an in-process fleet, or a queue-driven fleet on
Kubernetes.

## Read these first

| When | Read |
|---|---|
| Picking up the **current phase** (Part III — cloud productionization) | [`docs/part-iii-resume.md`](docs/part-iii-resume.md) — the single next-session source of truth |
| Understanding **how the system is built / designed** | [`docs/architecture/`](docs/architecture/README.md) — the living architecture wiki |
| The **previous phase** (Part II library + dev fleet, done) | [`docs/part-ii-resume.md`](docs/part-ii-resume.md) |
| Using loopkit on a real repo / the steering files | [`docs/USING-ON-YOUR-REPO.md`](docs/USING-ON-YOUR-REPO.md), [`docs/CONTROL-FILES.md`](docs/CONTROL-FILES.md) |
| **Teaching** Part III — GitHub/GitLab ecosystem, the three tiers, the runnable labs (`loopkit demo 18/19`) | [`docs/part-iii-ecosystem.md`](docs/part-iii-ecosystem.md) |

## ⛓️ Documentation contract (binding)

The architecture wiki and the resume doc are **load-bearing infrastructure, not afterthoughts**.
They are the canonical reference for both humans and AI working on this project.

- **`docs/architecture/`** is the long-running architecture reference. **Update it on any meaningful
  or relevant change** — a new subsystem or module, a changed contract/interface, a new
  architectural decision (or the reversal of one), a new failure mode/sharp edge, a new control-flow
  or data-flow path. Keep the page that *owns* that area current; keep the master diagram in
  `docs/architecture/README.md` in sync with the topology.
- **`docs/part-iii-resume.md`** (or the current phase's resume doc) holds **current state +
  load-bearing context + sharp edges + next step** — not chronological history. Update it whenever
  state moves: a phase starts/completes, a decision is locked, a gotcha is paid for, the "next step"
  changes.
- **History belongs in git + the resume-doc changelog, not in the architecture wiki.** The wiki
  describes how things *are/will be*; the resume doc describes *where we are and what's next*.
- A change is not "done" until the docs that describe it are updated in the same change.

This contract exists because loopkit is itself a tool for autonomous, long-horizon work: the docs
are the interface a future session (human or agent) uses to resume without re-deriving context.

## Invariants to preserve (don't regress)

- **Reuse the contracts**, don't fork them: `Agent` / `Gate` / `Store`, the three `StopPolicy`
  stops, the **held-out acceptance gate**, the `[loopkit][component]` + run-id logging, and
  safe-by-default (never `main`, clean tree, protected paths, budget ceiling).
- **Extend at the seams.** The core (`loopkit/`) keeps **no runtime dependency** on
  `loopkit/extensions/`. New core attach points follow the established shape: keyword-only,
  typing-only import, duck-called, `None`-safe (None = exact prior behavior).
- **Stack stays thin:** `typer + rich + pydantic`, stdlib-first elsewhere. Optional wires behind
  extras (`[claude]`/`[openai]` = the API-adapter SDKs, `[trace]` = langsmith, `[fleet]` = redis,
  `[cloud]` = kubernetes client; `truststore` is **dev-only**, never prod). `pip install loopkit`
  never pulls any of them.
- **Two-layer observability (don't collapse it):** always-on **payload-free logs** (`log.py`,
  `[loopkit][component]` + run id, ids/lengths/counts only) **plus** optional **full-tree LangSmith
  traces** (`trace.py`, `[trace]`, auto-on, `None`-safe, nests via contextvars). Traces are the one
  place the human-readable I/O + tool calls + per-span cost/usage belong; logs still never carry
  payloads. Cost is exact via `pricing.py` (per-model table) — that's what makes the budget stop bite.
- **Test-as-you-go with `MockAgent` (zero tokens); log-as-you-go; trace-as-you-go.** Add a scenario
  (`scenarios/chNN_*.py`) for each new concept so `demo`/`learn` keep pace. New traced steps land
  with a fake-provider test (assert the span tree + cost; no real key, no network).

## Cloud / kubectl safety (Part III)

Part III deploys to a **managed cloud cluster (DigitalOcean DOKS)** — a production-sensitive
context under the global kubectl-safety rule. The `loopkit cloud` CLI and any kubectl/helm use
**must pin the expected cluster context and refuse/confirm before mutating any other** — the same
`allow_k8s_contexts('kind-loopkit')` + `fail()` guard the dev `Tiltfile` uses, extended to the real
cloud context. Never run a mutating command against a cloud context without explicit confirmation.
The repo-local `KUBECONFIG` isolation pattern in the `Makefile` is the model.
