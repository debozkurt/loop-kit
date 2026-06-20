# loopkit Architecture

The living architecture reference for loopkit — written for principal-level engineering use:
decisions with their rationale and trade-offs, not just description. This is the canonical map of
how the system is built and where it is going, for humans and for AI agents working on the project.

> **Companion docs.** This wiki describes *how things are/will be*. For *where we are and what's
> next*, read [`../part-iii-resume.md`](../part-iii-resume.md) (current phase) first. The project
> instructions and the documentation contract live in [`../../CLAUDE.md`](../../CLAUDE.md).

## Maintenance contract

These docs are load-bearing. **Update them in the same change that alters the system** — a new
module/subsystem, a changed contract, a locked (or reversed) decision, a new failure mode, a new
control/data-flow path. Keep the page that *owns* the area current and keep the master diagram below
in sync with the topology. History goes in git and the resume-doc changelog, not here. See
[`CLAUDE.md`](../../CLAUDE.md) → *Documentation contract* for the full rule.

## Page map

| Page | Owns |
|---|---|
| **README** (this page) | The map, the master diagram, the glossary, the status legend |
| [`01-system-today.md`](01-system-today.md) | **Built:** the single-loop core, its contracts, the **2×2 adapter matrix** + cost (`pricing.py`), the **two-layer observability** (logs + LangSmith traces), the extension seams, the in-process + dev-cluster fleet |
| [`02-cloud-architecture.md`](02-cloud-architecture.md) | **Designed:** the Part III Kubernetes/DOKS target — topology, run lifecycle, control plane, storage, scaling, triggers |
| [`03-adapters-and-auth.md`](03-adapters-and-auth.md) | The agent-adapter matrix (CLI/API × Claude/OpenAI), the pluggable credential model, per-submitter keys, billing |
| [`04-security.md`](04-security.md) | The Ch 16 safety envelope at cloud scale — threat model and defense-in-depth |

## Status legend

Every claim in these docs carries one of these, so a reader always knows what exists vs. what is
targeted:

- **🟢 Built** — implemented, tested, in `main`. (Core, Part II extensions, dev kind/Tilt fleet.)
- **🟡 Designed** — decided and specified here, not yet built. (The Part III cloud system.)
- **⚪ Planned** — a known future direction, not yet specified. (Operator/CRD, KEDA, Vault, dashboard.)

## Master diagram — the Part III target topology 🟡

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#1b1b1b','primaryColor':'#2b2b2b','primaryTextColor':'#e6e6e6','primaryBorderColor':'#5a5a5a','lineColor':'#8a8a8a','secondaryColor':'#333333','tertiaryColor':'#242424','fontSize':'13px'}}}%%
flowchart LR
  CLI["loopkit cloud<br/>CLI (laptop / CI)"]
  CRON["CronJob<br/>scheduled"]
  HOOK["Webhook listener<br/>push / PR / issue"]
  CREATE["create_run()<br/>namespace +<br/>Secrets + Jobs"]
  REDIS["Redis StatefulSet<br/>queue + results<br/>per-run keyspace"]
  COORD["Coordinator Job<br/>enqueue · collect ·<br/>select · sentinel"]
  WORK["Worker Job<br/>parallelism N ·<br/>clone · run_loop · push"]
  GH["GitHub<br/>clone / branch / PR ·<br/>issues · skills repo"]
  LLM["Agent API<br/>Claude / OpenAI"]

  CLI --> CREATE
  CRON --> CREATE
  HOOK --> CREATE
  CREATE --> COORD
  CREATE --> WORK
  COORD <--> REDIS
  WORK <--> REDIS
  WORK --> GH
  WORK --> LLM
```

> Render with `/render-mermaid --hd` to verify (dark-greyscale, flat by house style). **Namespaces:**
> `ns/loopkit-system` holds the long-lived Redis StatefulSet + webhook listener; each run gets an
> ephemeral `ns/run-<id>` holding its coordinator Job, worker Job, and Secrets, GC'd on completion.
> The three submit paths (CLI, CronJob, webhook) **converge on one `create_run()`** — the single
> code path that materializes a run. Full detail in [`02-cloud-architecture.md`](02-cloud-architecture.md).

## Glossary

| Term | Meaning |
|---|---|
| **Tick** | One iteration of the core loop: `prompt → agent → guard → commit → review → gates → stops`. |
| **Gate** | An external command whose exit status verifies work. Two kinds: the **iteration gate** (the agent can see/run it) and the **held-out acceptance gate** (the agent cannot — the anti-overfit check). |
| **Hard stop** | A terminal condition independent of the goal: budget, no-progress, iteration cap. Precedence: `DONE > SAFETY > BUDGET > NO_PROGRESS > CAP`. |
| **Run** | One logical unit of work: a target repo + a goal (or a set of tasks) + a budget, producing branches/PRs. The cloud siloing/accounting unit (one `ns/run-<id>`). |
| **Task** | One item within a run (one goal, one branch). Workers pull tasks off the queue. |
| **Fleet** | Many workers draining a queue in parallel. In-process (`Supervisor`), or queue-driven across containers (`Coordinator`/`Worker` over Redis). |
| **Coordinator** | Transport-only driver: enqueues tasks, collects outcomes, runs evolutionary selection. Holds no agent and no gate. |
| **Worker** | The executor: `pop → clone → run_loop → push → put outcome`. Physical isolation = its own pod filesystem + branch. |
| **Adapter** | A concrete `Agent` implementation. Four real ones (the 2×2 matrix): `claude-code` / `codex` (CLI) and `claude-api` / `openai-api` (SDK), plus the token-free `mock`. See [`03`](03-adapters-and-auth.md). |
| **Cost / pricing** | The per-tick dollar cost an adapter reports, computed by `pricing.py` from native token usage (per-model table, cache tiers). Feeds the budget stop and every trace span; unpriced model → 0.0 (the budget can't bite — `doctor` warns). |
| **Trace** | An optional full LangSmith run tree (`run → tick → agent → llm/tool → gate`) carrying human-readable I/O, all tool calls, and cost/usage metadata. The always-on, payload-free `[loopkit][component]` logs are its complement — see [`01`](01-system-today.md#observability--two-layers-logs--traces). |
| **Held-out / selection-inflation guard** | A candidate may only "win" (reseed a generation, be declared done) after passing a gate it never competed on — the Ch 9 defense against overfitting/reward-hacking. |
| **Control files** | Per-repo steering that travels with the clone: `loopkit.toml`, `PROMPT.md`, `CLAUDE.md`/`AGENTS.md`, the gate commands, skills `.md`. See [`../CONTROL-FILES.md`](../CONTROL-FILES.md). |
