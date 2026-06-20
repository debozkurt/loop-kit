# loopkit — Part III resume (cloud productionization)

**Read this first when picking up Part III.** It is the single source of truth for the current
phase: state, locked decisions, the build sequence, sharp edges, and the next step. For *how the
system is built/designed*, read the architecture wiki: [`architecture/`](architecture/README.md).
The auto-memory `project_loopkit` only points here.

> **Current state (2026-06-20):** **Phases 0–2 built; the two live steps (a GitHub remote, a real
> DOKS cluster) are the only things outstanding.** Part II (the extension library) and the dev
> kind/Tilt fleet are done and verified live (see [`part-ii-resume.md`](part-ii-resume.md)).
> **Phase 0** (branch `phase-0-adapters-tracing`) landed the pure-library foundation: the full **2×2
> adapter matrix**, **real per-adapter cost parsing** (`pricing.py`), and a **full-tree LangSmith
> tracing layer** (`trace.py`, auto-on, `None`-safe, verified live). **Phase 1** added the
> **`worker-image` GitHub Actions workflow** (buildx multi-arch `amd64` → GHCR + an in-CI amd64 smoke
> test) + the `imagePullSecret` recipe, **verified locally** in-container. **Phase 2** (this session)
> landed the **cloud control-plane foundation**: the non-negotiable **context-safety guard**
> (`extensions/cloud.py`, fail-closed, deferred-import behind `[cloud]`), the **`loopkit cloud`**
> sub-app (`context`/`doctor`/`bootstrap`), the **`k8s/cloud/` system manifests** (`loopkit-system`
> namespace, **Redis StatefulSet with AOF + PVC**, `loopkit-control` RBAC, default-deny
> **NetworkPolicy** + egress allowlist), and the repo-local-`KUBECONFIG` Makefile targets. **98 tests
> green** (was 81; +17 cloud). **Gating items:** (1) **no git remote** → the worker-image workflow
> hasn't run on Actions / nothing in GHCR; (2) **no DOKS cluster yet** → `bootstrap` is unit-verified
> (guard refuses a wrong context; manifests assert AOF+PVC durability) but not yet live-applied. The
> architecture is decided in [`architecture/`](architecture/README.md); the next build step is
> **Phase 3** below.

## Locked decisions

Every load-bearing fork is decided. Detail + rationale live in the architecture wiki (linked).

| Area | Decision | Where |
|---|---|---|
| **Topology** | Ephemeral **per-run Jobs** (coordinator + worker), the work-queue Job pattern, **sentinel shutdown** | [`02`](architecture/02-cloud-architecture.md#run-lifecycle) |
| **Tenancy** | **Namespace per run**; `ResourceQuota`/`LimitRange` loose to start (separation now, tighten later) | [`02`](architecture/02-cloud-architecture.md#tenancy--namespace-per-run) |
| **Queue/state** (manifest Built 🟢 Phase 2) | In-cluster **Redis StatefulSet + PVC + AOF**; per-run keyspace | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Worker storage** | **`emptyDir`** (durability is via git push); shared-PVC ruled out by DO RWO | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Control plane** (CLI + bootstrap Built 🟢 Phase 2) | **CLI + CronJobs + webhook listener** → one `create_run()`; operator/CRD = v2 | [`02`](architecture/02-cloud-architecture.md#control-plane--one-path-three-entry-points) |
| **CLI ↔ k8s** (Built 🟢 Phase 2) | Python **kubernetes client** (`loopkit[cloud]`); cloud-agnostic; runs laptop **or** in-cluster; **context-safety guard** pins the DOKS context | [`02`](architecture/02-cloud-architecture.md#control-plane--one-path-three-entry-points) |
| **Worker scaling** | **Fixed `--workers N`** for v1; KEDA `ScaledJob` later | [`02`](architecture/02-cloud-architecture.md#scaling) |
| **Registry/image** (Built 🟢 Phase 1) | **GHCR**, **multi-arch amd64** built via GitHub Actions (not `kind load`); `imagePullSecret` recipe | [`02`](architecture/02-cloud-architecture.md#image--registry-pipeline) |
| **Adapters** | Full 2×2: `claude-code` / `claude-api` / `codex` / `openai-api` behind the `Agent` protocol | [`03`](architecture/03-adapters-and-auth.md#the-agent-protocol--the-22-adapter-matrix) |
| **Agent auth** | **Pluggable** per `(env, adapter, submitter)`: OAuth token **or** API key; per-submitter keys (Option 1 hardened, Vault later) | [`03`](architecture/03-adapters-and-auth.md#the-pluggable-credential-model) |
| **Billing** | Dedicated **API key for prod** (subscription subsidy ended 2026-06-15); subscription token for dev | [`03`](architecture/03-adapters-and-auth.md#billing--cost-control) |
| **Skills home** | Dedicated **`loopkit-skills` git repo** (cross-run learned state) | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Security** (guard + NetworkPolicy + RBAC Built 🟢 Phase 2) | Ch 16 envelope extended: default-deny NetworkPolicy, least-priv SAs, per-run Secrets, branch-only/draft PRs, context guard | [`04`](architecture/04-security.md) |
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
- **Phase 1 — Image + registry ✅ BUILT (live push pending a remote).** The `worker-image` GitHub
  Actions workflow builds the root `Dockerfile` via buildx → `linux/amd64` (+arm64), smoke-tests the
  amd64 image on the runner (`fleet worker --help`, `demo 12`, `demo 14`), then pushes multi-arch to
  **GHCR**; the `imagePullSecret` recipe is documented in
  [`02`](architecture/02-cloud-architecture.md). *Acceptance:* the in-CI smoke step runs `fleet
  worker` on an amd64 node — **proven locally** (native build + in-container run of the smoke commands);
  the real amd64 build + GHCR push runs once the repo has a GitHub remote.
- **Phase 2 — Cluster foundation ✅ BUILT (live apply pending a DOKS cluster).** Landed the
  control-plane *code + manifests + tests*: the **context-safety guard** (`extensions/cloud.py` —
  pure `check_context`, fail-closed when unpinned, deferred-import behind `[cloud]`), the **`loopkit
  cloud`** sub-app (`context`/`doctor`/`bootstrap`, guard runs before any mutation), the
  **`k8s/cloud/`** manifests (`ns/loopkit-system`; **Redis StatefulSet with AOF + PVC**;
  `loopkit-control` SA + ClusterRole/Binding and a no-API worker SA; default-deny **NetworkPolicy** +
  DNS/intra-ns/HTTPS-egress allowlist with the metadata endpoint blocked), and `cloud-*` Makefile
  targets that keep the cloud kubeconfig **repo-local** (host `~/.kube/config` untouched). Node-pool
  + cluster provisioning is the `make cloud-provision` `doctl` recipe (needs DO creds). *Acceptance:*
  **CLI refuses a wrong context** — proven (unit + CLI tests, 98 green); **host kubeconfig untouched**
  — the repo-local-`KUBECONFIG` pattern; **Redis durable across pod restart** — asserted by the
  manifest (AOF + PVC) + test, **live-verified once a real DOKS cluster exists** (`make
  cloud-bootstrap`).
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
- ✅ **Multi-arch amd64 build pipeline** — *built (Phase 1)*: the `worker-image` Actions workflow →
  GHCR (the dev `Tiltfile`'s arm64 + `kind load` stays dev-only). Goes live once the repo has a remote.
- ✅ Redis **AOF** durability — *built (Phase 2)*: `k8s/cloud/10-redis.yaml` is a StatefulSet with
  `appendonly yes` + a PVC volumeClaimTemplate (dev Redis stays ephemeral by design). Live-verify on
  a real cluster.
- ✅ **NetworkPolicy** default-deny + egress allowlist; **least-privilege SAs** — *built (Phase 2)*:
  `k8s/cloud/30-networkpolicy.yaml` (default-deny + DNS/intra-ns/HTTPS, metadata blocked) and
  `20-rbac.yaml` (`loopkit-control` is the only SA that may create ns/Jobs/Secrets; workers get a
  no-API SA with token automount off). Per-run-namespace policies are stamped in Phase 3.
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

**Two live steps are outstanding (both need external resources, not code):**

1. **Make Phase 1 live — push to a GitHub remote** so the `worker-image` workflow runs on Actions'
   amd64 runners and pushes to GHCR. (The corp Zscaler proxy blocks the *emulated* amd64 cross-build
   locally — `x509: certificate signed by unknown authority` when buildx's `docker-container` driver
   pulls the base image; a **dev-only** TLS edge, Actions has no such proxy. Native build + in-container
   smoke already pass here.)
2. **Make Phase 2 live — provision a DOKS cluster** (`make cloud-provision` prints the `doctl` recipe
   with the system + autoscaling worker node pools), `make cloud-kubeconfig` (writes the **repo-local**
   `.kube/loopkit-cloud.yaml`), then `make cloud-doctor` → `make cloud-bootstrap`. That live-verifies
   the one Phase-2 acceptance unit tests can't reach: **Redis durable across a pod restart** (kill
   `redis-0`, confirm the queue/results survive via the AOF + PVC). The guard ("refuses a wrong
   context") and the kubeconfig isolation are already proven locally.

**Then start Phase 3 — Run mechanics (the core integration):** per-run namespace + **coordinator Job +
worker Job** (work-queue pattern, `emptyDir`, **sentinel shutdown**, per-run Redis keyspace); per-run
Secrets; `loopkit cloud run/ls/status/logs/kill` via the kubernetes client — all attaching to the
Phase-2 `extensions/cloud.py` seam (the guard wraps every new mutating command). *Acceptance:* one real
end-to-end run on DOKS produces a branch + draft PR; `evolve` reseeds across generations; namespace
GC'd on completion. Carry the invariants in [`../CLAUDE.md`](../CLAUDE.md): extend at the seams,
`None`-safe, thin stack, test-as-you-go, log-as-you-go, **trace-as-you-go**, and **every mutating cloud
command goes through the context guard**. **Update this doc and the architecture wiki as each phase
lands** (the documentation contract).

## Changelog

- **2026-06-20 — Phase 2 built (cluster foundation: guard + manifests).** Added
  `loopkit/extensions/cloud.py` — the **context-safety guard** (pure `check_context`/`resolve_expected`,
  fail-closed when nothing is pinned, deferred `kubernetes` import behind the new **`[cloud]`** extra)
  + `bootstrap()` (idempotent `create_from_yaml`, guard runs first). Wired the **`loopkit cloud`**
  Typer sub-app (`context`/`doctor`/`bootstrap`) into `cli.py`. Wrote **`k8s/cloud/`**:
  `00-namespace` (`loopkit-system`), `10-redis` (StatefulSet **AOF + PVC** + headless Service +
  ConfigMap), `20-rbac` (`loopkit-control` SA + ClusterRole/Binding; no-API worker SA),
  `30-networkpolicy` (default-deny + DNS/intra-ns/HTTPS-egress, metadata blocked). Added `cloud-*`
  **Makefile** targets that keep the cloud kubeconfig **repo-local** (`.kube/loopkit-cloud.yaml`,
  host `~/.kube/config` untouched) + a `cloud-provision` `doctl` recipe (system + autoscaling worker
  node pools). New **`tests/test_cloud.py`** (17 tests): guard logic, kubeconfig reading, CLI
  refuse/allow, manifest durability/least-priv sanity — **81 → 98 green**. Deferred-import invariant
  verified (core CLI loads with no `kubernetes`). **Gating item:** no DOKS cluster yet, so `bootstrap`
  is unit-verified but not live-applied. Updated [`02`](architecture/02-cloud-architecture.md) (control
  plane / Redis / tenancy now Built 🟢) and [`04`](architecture/04-security.md) (guard / NetworkPolicy
  / RBAC now Built 🟢).
- **2026-06-19 — Phase 1 built (image + registry).** Added `.github/workflows/worker-image.yml`:
  buildx multi-arch (`linux/amd64` +arm64) → GHCR, with an in-CI amd64 smoke test (`fleet worker
  --help`, `demo 12`, `demo 14`) that *is* the Phase-1 acceptance, plus the `imagePullSecret` recipe in
  the wiki ([`02`](architecture/02-cloud-architecture.md)). The root `Dockerfile` is reused unchanged —
  its multi-arch base means the Tiltfile's arm64 pin never leaks. Image build + smoke **verified
  locally** (native build; corp proxy blocks the emulated amd64 cross-build — dev-only TLS edge, Actions
  unaffected). **Gating item:** no git remote yet, so the workflow hasn't run on Actions / nothing is in
  GHCR. Phase 0 committed on branch `phase-0-adapters-tracing`. Also landed a docs convention —
  command-listing code blocks now read like real shell (`#` comments, single-line); applied to the
  CLI-surface block in `02`.
- **2026-06-19 — Phase 0 done + tracing.** Built the 2×2 adapter matrix + `pricing.py` (budget stop
  now bites); added `trace.py` (full-tree LangSmith tracing, auto-on, `None`-safe, verified live —
  user-requested as a global AI-app convention, now in global `CLAUDE.md`). New `ch14_economics`
  scenario; `doctor` agent/budget/tracing rows; extras `[claude]`/`[openai]`/`[trace]` (truststore is
  **dev-only**, never prod). 60 → 81 tests. Architecture wiki diagrams converted ASCII → mermaid
  (per the user's mermaid-only-for-loopkit-docs rule).
