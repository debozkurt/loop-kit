# loopkit — Part III resume (cloud productionization)

**Read this first when picking up Part III.** It is the single source of truth for the current
phase: state, locked decisions, the build sequence, sharp edges, and the next step. For *how the
system is built/designed*, read the architecture wiki: [`architecture/`](architecture/README.md).
The auto-memory `project_loopkit` only points here.

> **Current state (2026-06-19):** **Phase 0 is DONE; the rest of Part III is scoped, not yet built.**
> Part II (the extension library) and the dev kind/Tilt fleet are done and verified live (see
> [`part-ii-resume.md`](part-ii-resume.md)). **Phase 0 landed the pure-library foundation:** the full
> **2×2 adapter matrix** (`claude-api`/`openai-api` SDK adapters + `codex` alongside `claude-code`),
> **real per-adapter cost parsing** (`pricing.py`) so the budget stop bites, and — added this phase —
> a **full-tree LangSmith tracing layer** (`trace.py`, auto-on, `None`-safe, verified live against the
> real backend). **81 tests green** (was 60); `demo 14` (the new Ch 14 economics scenario) clean. The
> remaining phases turn loopkit into a **cloud-deployable system on DigitalOcean DOKS that runs many
> jobs in production**. The architecture is decided in [`architecture/`](architecture/README.md); the
> next step is **Phase 1** below.

## Locked decisions

Every load-bearing fork is decided. Detail + rationale live in the architecture wiki (linked).

| Area | Decision | Where |
|---|---|---|
| **Topology** | Ephemeral **per-run Jobs** (coordinator + worker), the work-queue Job pattern, **sentinel shutdown** | [`02`](architecture/02-cloud-architecture.md#run-lifecycle) |
| **Tenancy** | **Namespace per run**; `ResourceQuota`/`LimitRange` loose to start (separation now, tighten later) | [`02`](architecture/02-cloud-architecture.md#tenancy--namespace-per-run) |
| **Queue/state** | In-cluster **Redis StatefulSet + PVC + AOF**; per-run keyspace | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Worker storage** | **`emptyDir`** (durability is via git push); shared-PVC ruled out by DO RWO | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Control plane** | **CLI + CronJobs + webhook listener** → one `create_run()`; operator/CRD = v2 | [`02`](architecture/02-cloud-architecture.md#control-plane--one-path-three-entry-points) |
| **CLI ↔ k8s** | Python **kubernetes client** (`loopkit[cloud]`); cloud-agnostic; runs laptop **or** in-cluster | [`02`](architecture/02-cloud-architecture.md#control-plane--one-path-three-entry-points) |
| **Worker scaling** | **Fixed `--workers N`** for v1; KEDA `ScaledJob` later | [`02`](architecture/02-cloud-architecture.md#scaling) |
| **Registry/image** | **GHCR**, **multi-arch amd64** built via GitHub Actions (not `kind load`) | [`02`](architecture/02-cloud-architecture.md#image--registry-pipeline) |
| **Adapters** | Full 2×2: `claude-code` / `claude-api` / `codex` / `openai-api` behind the `Agent` protocol | [`03`](architecture/03-adapters-and-auth.md#the-agent-protocol--the-22-adapter-matrix) |
| **Agent auth** | **Pluggable** per `(env, adapter, submitter)`: OAuth token **or** API key; per-submitter keys (Option 1 hardened, Vault later) | [`03`](architecture/03-adapters-and-auth.md#the-pluggable-credential-model) |
| **Billing** | Dedicated **API key for prod** (subscription subsidy ended 2026-06-15); subscription token for dev | [`03`](architecture/03-adapters-and-auth.md#billing--cost-control) |
| **Skills home** | Dedicated **`loopkit-skills` git repo** (cross-run learned state) | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Security** | Ch 16 envelope extended: default-deny NetworkPolicy, least-priv SAs, per-run Secrets, branch-only/draft PRs, context guard | [`04`](architecture/04-security.md) |
| **Observability** (Built 🟢) | Two layers: payload-free logs (`log.py`) **+** full-tree **LangSmith traces** (`trace.py`, optional `[trace]`, auto-on, `None`-safe); per-span cost via `pricing.py` | [`01`](architecture/01-system-today.md#observability--two-layers-logs--traces) |

## Build sequence

Ordered so each phase is independently testable and the risky integration (a real cloud run) is
proven before the trigger surface is built on top of it.

- **Phase 0 — Adapters + budget teeth ✅ DONE** (pure library, no k8s). Built the 2×2 adapter matrix
  (`claude-api`, `codex`, `openai-api` alongside `claude-code`) with an injectable backend seam for
  token-free tests; `pricing.py` per-model cost table → API adapters sum native usage, `claude-code`
  parses `total_cost_usd`, `codex` derives from token usage; `doctor` gained agent/budget readouts;
  `AgentConfig.max_tool_calls` bounds the per-tick API loop. **Bonus (this phase): a full LangSmith
  tracing layer** (`trace.py`) wired whole-system (run→tick→agent→llm/tool→gates, cost on every span),
  auto-on + `None`-safe, verified live. *Acceptance met:* 81 tests green (token-free); `demo 14`
  shows a real costed run reaching DONE and the budget stop biting; traces confirmed in LangSmith.
- **Phase 1 — Image + registry.** Multi-arch **amd64** worker image (agent CLIs + target toolchain) →
  **GHCR via GitHub Actions**; `imagePullSecret`. *Acceptance:* image pulls + runs `fleet worker` on
  an amd64 node.
- **Phase 2 — Cluster foundation.** DOKS cluster; `ns/loopkit-system`; **Redis StatefulSet (AOF +
  PVC)**; RBAC (`loopkit-control` SA); default-deny **NetworkPolicy** + egress allowlist; node pools;
  the **context-safety guard** in the CLI. *Acceptance:* Redis durable across pod restart; host
  kubeconfig untouched; CLI refuses a wrong context.
- **Phase 3 — Run mechanics (the core integration).** Per-run namespace + **coordinator Job + worker
  Job** (work-queue pattern, `emptyDir`, **sentinel shutdown**, per-run Redis keyspace); per-run
  Secrets; `loopkit cloud run/ls/status/logs/kill` via the kubernetes client. *Acceptance:* one real
  end-to-end run on DOKS produces a branch + draft PR; `evolve` reseeds across generations; namespace
  GC'd on completion.
- **Phase 4 — Triggers.** **CronJob** (`schedule`) + **webhook listener** (HMAC + idempotency) on the
  shared `create_run()`; in-cluster `--from-issues`. *Acceptance:* a scheduled run fires; a signed
  webhook starts exactly one run per issue; a forged/duplicate delivery is rejected.
- **Phase 5 — Per-submitter creds + skills repo.** Identity→Secret resolver (Option 1 hardened);
  `loopkit-skills` repo wired into the worker (read + gated write-back). *Acceptance:* two engineers'
  runs use their own keys; a solved run writes a skill back that a later run reads.
- **Phase 6 — Observability + hardening.** Logs/metrics shipping, the read-only dashboard, then the v2
  layer (KEDA, ESO/Vault, GitHub App, tighter quotas) as demand dictates.

## Gap inventory

**🔴 v1-critical** (don't ship without these)
- Multi-arch **amd64** build pipeline (the dev `Tiltfile` pins arm64 + `kind load` — do not leak that
  into prod).
- Redis **AOF** durability (dev Redis is ephemeral by design).
- **NetworkPolicy** default-deny + egress allowlist; **least-privilege SAs** (workers get no
  cluster-API access).
- Webhook **HMAC + idempotency**.
- **GitHub auth** for clone/push/PR at scale (PAT to start; **GitHub App** is the right end state).

**🟡 Later / hardening**
- Node pools (system + autoscaling worker) via the DO cluster autoscaler.
- Lifecycle GC: `ttlSecondsAfterFinished`, namespace GC, Redis keyspace cleanup, orphaned-branch
  cleanup.
- Mid-task pod death: `backoffLimit` retries + task re-pop (commit-every-tick is *local* durability
  until the DONE-push; push-every-tick to a WIP branch would make runs resumable — an enhancement).
- Observability stack; the deferred dashboard; a run-history store (GitHub PRs + `kubectl get jobs`
  suffice for v1).

## Sharp edges to carry (paid for or foreseen)

- **arm64 → amd64.** Local Colima/Apple Silicon is arm64; DO nodes are amd64. Prod images must be
  amd64/multi-arch, pushed to GHCR — not `kind load` (which the Tiltfile uses for the Docker-29
  containerd workaround).
- **DO block storage is ReadWriteOnce** — no shared-across-nodes PVC. `emptyDir` for workers sidesteps
  it entirely.
- **Subscription billing changed 2026-06-15** — headless Claude Code now draws a capped agent-credit
  pool at API rates; the subscription is **not** a cheap way to run a fleet. Use a dedicated API key
  for prod.
- **`evolve` must use sentinel shutdown,** not exit-on-empty-queue — workers must survive the gaps
  between generations.
- **Redis port:** the dev host runs a local `redis-server` on 6379; the dev fleet forwards to
  **:16379**. In-cluster prod uses the in-namespace Service DNS, no such collision — don't carry the
  16379 default into prod config.
- **Context guard is non-negotiable** on a cloud control plane (see [`04`](architecture/04-security.md)).
- **Tracing is auto-on but must stay a clean no-op.** `trace.py` activates only when `langsmith` +
  a LangSmith key are present; with neither it's a cheap no-op, so core code calls `trace.span(...)`
  unconditionally. Don't make any module hard-depend on `langsmith` — it's behind `[trace]`.
- **Zscaler/corp-TLS is a *local-dev-only* concern.** Behind the corp proxy the LangSmith uploader
  (and the SDKs) fail cert verification ("unable to get local issuer certificate"); `trace.py`
  injects `truststore` (OS trust store) **only if importable**, and `truststore` ships in
  `[dev]`, **never `[trace]`**. Prod uses normal TLS with standard CAs — never deploy the workaround.

## Open / deferred decisions

- GitHub **App** vs PAT/deploy-keys (App is the eventual answer for multi-repo + PR creation).
- **KEDA** timing (when a single run needs to fan very wide).
- **ESO/Vault** migration for secrets (resolver swap, not redesign).
- Observability stack choice (DO managed logs vs Loki/Grafana).
- Operator + **`LoopRun` CRD** as the v2 declarative control plane.

## Next step

**Start Phase 1 — Image + registry.** Phase 0 (adapters + cost + tracing) is done, so the next
risk to retire is getting a real worker image onto a real node: a multi-arch **amd64** worker image
(agent CLIs + target toolchain) built and pushed to **GHCR via GitHub Actions** (not `kind load` —
the dev Tiltfile's arm64 pin must not leak to prod), plus an `imagePullSecret`. *Acceptance:* the
image pulls and runs `fleet worker` on an amd64 node. Carry the invariants in
[`../CLAUDE.md`](../CLAUDE.md): extend at the seams, `None`-safe, thin stack, test-as-you-go,
log-as-you-go, **trace-as-you-go**. **Update this doc and the architecture wiki as each phase lands**
(the documentation contract).

## Changelog

- **2026-06-19 — Phase 0 done + tracing.** Built the 2×2 adapter matrix + `pricing.py` (budget stop
  now bites); added `trace.py` (full-tree LangSmith tracing, auto-on, `None`-safe, verified live —
  user-requested as a global AI-app convention, now in global `CLAUDE.md`). New `ch14_economics`
  scenario; `doctor` agent/budget/tracing rows; extras `[claude]`/`[openai]`/`[trace]` (truststore is
  **dev-only**, never prod). 60 → 81 tests. Architecture wiki diagrams converted ASCII → mermaid
  (per the user's mermaid-only-for-loopkit-docs rule).
