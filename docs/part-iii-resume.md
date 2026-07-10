# loopkit — Part III resume (cloud productionization)

**Read this first when picking up Part III.** It is the single source of truth for the current
phase: state, locked decisions, sharp edges, and the next step. For *how the system is
built/designed*, read the architecture wiki: [`architecture/`](architecture/README.md). The
auto-memory `project_loopkit` only points here. **History is in `git log`** (and the resume-doc
changelog was retired 2026-06-29 — this doc holds current state, not a diary).

## Current state (2026-06-29)

**Every coded phase of Part III (0–6) is built and tested — `324` tests green.** The env-grab
is replaced by an identity→Secret resolver, the key is withheld from the agent, the trigger paths
bind a run to the issue author, the single loop runs forge-CI-natively with no cluster, the cloud
worker's untrusted tool surface runs in a **keyless, isolated executor container** (a kernel
boundary, not a timed shred), and the cross-run flywheel has a durable git-repo home
(`loopkit-skills`). On top of the phases, the **measurement layer** has its first bricks (`loopkit
measure` → `pass^k`, `cost_per_accepted`) and a **gate-determinism preflight** (`run --check-gate`).

**What is NOT done is live enablement — it needs external resources, not code:**

1. **A GitHub remote** → the `worker-image` workflow runs on Actions' amd64 runners + pushes to GHCR
   (Phase 1), and the CI drop-in + the real `loopkit-skills` repo go live.
2. **A DOKS cluster** → live-apply Phases 2–6 (Redis-durable-across-restart, one real end-to-end run,
   a live webhook/CronJob firing, the multi-tenant creds proof, and the Phase-6 ptrace-fails proof).

**Next code task (no cluster needed): Security E — Redis AUTH** (see *Next step*).

## Status at a glance

Each phase is built + unit-tested token-free; the live column is what still needs a remote/cluster.

| Phase | Delivers | Status |
|---|---|---|
| **0 — Adapters + budget teeth** | 2×2 adapter matrix, `pricing.py` cost, LangSmith tracing | 🟢 built + live |
| **1 — Image + registry** | `worker-image` Actions → GHCR (multi-arch amd64) + `imagePullSecret` | 🟢 built · ⏳ live push needs a remote |
| **2 — Cluster foundation** | context guard, `loopkit cloud`, `k8s/cloud/` (Redis SS+AOF+PVC, RBAC, default-deny NetworkPolicy) | 🟢 built · ⏳ apply needs DOKS |
| **3 — Run mechanics** | sentinel shutdown, `cloudrun.create_run` (per-run ns + Jobs), `cloud run/ls/status/logs/kill` | 🟢 built · ⏳ live run needs DOKS |
| **4 — Triggers** | webhook (GitHub HMAC / GitLab token) + CronJob → one `create_run`, in-cluster auth | 🟢 built · ⏳ live fire needs DOKS |
| **5a — Per-submitter creds** | identity→Secret resolver + worker `secrets.py` hygiene, red-teamed | 🟢 built · ⏳ multi-tenant proof needs DOKS |
| **5b — Skills repo** | `GitSkillRegistry`: clone-at-start + gated push-on-`DONE` (the flywheel across machines) | 🟢 built · ⏳ needs a real `loopkit-skills` remote |
| **5c — CI tier** | `run --from-event/--from-issue/--open-pr`, `init --ci`, `examples/ci/` (no cluster) | 🟢 built · live drop-in optional |
| **6 — Agent isolation** | keyless-executor sidecar (`executor.py`), two-container worker pod | 🟢 built · ⏳ ptrace-fails proof needs DOKS |
| **6 (rest) — observability / v2** | logs/metrics shipping, read-only dashboard; KEDA/ESO·Vault/GitHub-App; separate-pod executor | ⚪ planned |

## Recent work (newest first — priming only; full history in `git log`)

- **2026-07-10 — guide↔loopkit pairing pass (docs only, no code):** distilled real-use lessons into the
  *Agentic Loops* manual as **foundational chapter content** (not a bolted-on "lessons" section) and
  paired the two repos hand-in-hand. Manual side (tutor `loops/`): Ch 5 (the *rediscovery* tax +
  the bounded note channel), Ch 9 (oracle-authoring friction → **oracle synthesis**, the top adoption
  lever), Ch 13 (semantic thrash + a `NEEDS_HUMAN` terminal), Ch 14 (`cost_per_iteration` as a lever —
  model escalation + per-seam routing), Ch 21 (**the loop doesn't stop at the PR** — the revise-run
  lifecycle), plus a new `loops/graduating-to-loopkit.md` concept→capstone→loopkit map. loopkit side
  (threaded, no new doc): `part-iii-ecosystem.md` (two-way graduation-map pointer + revise-run mirror),
  `part-iii-prior-art.md` (surfaced oracle synthesis + tied the scratchpad item to `loops/05`). Cross-repo
  refs kept **textual** (the manual is a separate repo). Roadmap unchanged; **Next step still Security E**.
- **2026-07-07 — trace grouping fixed (fleet/evolve = ONE LangSmith trace):** diagnosed from live
  spacer-remediation traces splitting into per-tick/per-gate roots. Root cause 1: `trace._provider()`
  marked itself resolved **before** the slow first `import langsmith`, so concurrent loops
  (evolve/fleet threads all opening `loopkit run` at once) got a `None` provider and their spans
  silently no-oped — every later span in that thread became its own root trace. Now lock-serialized,
  resolved-flag set last. Root cause 2: pool threads start with empty contextvars, so worker trees
  couldn't parent under a supervisor span — `orchestrate._dispatch` now runs each worker in a
  `contextvars.copy_context()` snapshot under a new `loopkit fleet`/`loopkit evolve` umbrella span
  (+ per-candidate `slug`/`generation`/`candidate` metadata, `score`/`revalidate` spans, tick
  `continue` outputs). Bonus fix: auto-on (API key, no flag) now sets `LANGSMITH_TRACING=true` for
  the SDK — langsmith's own `trace` cm gates posting *and* parenting on that exact value, so key-only
  auto-on used to upload **nothing**; an explicit `LANGSMITH_TRACING=false` now wins over the key.
  Proved against real langsmith 0.9.8 (network stubbed) + thread-race and nesting-fake tests.
- **2026-07-07 — revise runs (the post-PR follow-through, CI tier):** a GitHub `pull_request_review`
  event with **changes requested** on a `loopkit/*` branch now dispatches a **revise run** — the
  review is the goal (`triggers.revise_goal`), the run **resumes the PR's head branch**
  (`--from-event` sets it; `durability.ensure_branch` now resumes remote-only branches from
  `origin/<branch>` so a fresh CI clone doesn't fork from main), and the push updates the same PR
  (no new PR, no `Closes #N`). Idempotency **inverts** for this lane: dedupe key `repo#prN@rID` =
  one run *per review round*, vs one-ever-per-issue. Containment = the `loopkit/` branch prefix (the
  loop only revises its own PRs); identity on the webhook path = the **reviewer's** key (C3). The
  cloud webhook parses revise but **defers 204** (RunSpec has no branch — tracked follow-up);
  CI-failure auto-revise is **deliberately excluded** (unbounded self-trigger). GitHub templates
  gained the `pull_request_review` lane (drift-guard kept); demo 20 gained the revise beat. Closes
  the "loop stops at PR-opened" gap surfaced by the loop-taxonomy gap analysis.
- **2026-06-29 — gate-aware `doctor`** (UX, branch `feat/gate-aware-doctor`): `loopkit doctor` now
  **runs the iteration gate once** on the current tree and reports what the verdict means for a run —
  *already passes* (the loop may instantly/ falsely DONE — a too-weak gate), *fails* (the healthy
  start), or *broken command* (a misconfig, flagged not mistaken for a test failure) — and warns when
  `gate.acceptance == gate.iteration` (defeats the held-out check, Ch 9). `--no-gate` skips it; the
  verdict is advisory (doctor's exit still tracks the safety preflight). First slice of the
  "idiot-proof local + CI" UX push. +5 tests (`test_doctor.py`), 333 → 338.
- **2026-06-29 — `cli.py` refactor** (branch `refactor/cli-package`, behavior-identical, surface-test
  guarded): split the 1443-line `cli.py` into a `cli/` package by deployment tier (`local`/`fleet`/
  `cloud`/`_support`); DRY'd the cross-cutting idioms (`fail`/`kc_str`/`confirm_or_abort`) and made the
  Ch 16 context guard **structural** via the `@guarded_command` decorator (a cloud command can't be
  registered without the refusal path); moved the typer-free run-creds decision policy into
  `extensions/creds.py` (`decide_run_creds`, now unit-testable). 326 → 333 tests.
- **2026-06-29 — docs + code structure reorg** (this session): extracted `cli.py`'s scaffolding
  templates to `loopkit/_templates.py`; refreshed the stale module maps + added the canonical
  file-ownership table in [`01-system-today.md`](architecture/01-system-today.md); added
  `examples/README.md` + `loopkit/scenarios/README.md`; trimmed the root README to handoffs + added a
  top funnel; archived the Part II docs to [`archive/`](archive/); rewrote this resume doc as a true
  resume (dropped the 412-line changelog → git).
- **2026-06-26 — operator UX + docs build-out:** a `run_loop` liveness heartbeat (`tick.progress`
  every 20 s so a healthy-but-silent run doesn't look hung); `OPERATING.md` / `BILLING.md` /
  `TROUBLESHOOTING.md`; a second gate flavor (`examples/gates/docs-gate.sh`) + the walkthrough.
- **2026-06-26 — claude-code billing safety:** `claude-code` defaults to the **subscription** (an
  ambient `ANTHROPIC_API_KEY` is withheld; opt in with `run --api-key`), and the cost parser now reads
  the CLI's top-level JSON array — the budget stop was silently blind on claude-code before.
- **2026-06-25 — reliability:** `gate_stability` preflight (`run --check-gate N`, refuse a flaky
  gate) + `cost_per_accepted` on the measure report.
- **2026-06-22 — `pass^k` measurement layer** (`extensions/measure.py` + `loopkit measure` + demo 24);
  Security follow-ups **D + G** fixed (liveness bounds + bounded flywheel).
- **2026-06-21 — Security hardening A–C** (sidecar git-hook adjacency, non-dumpable key-holder,
  flywheel-poisoning guards) + the **prior-art pass** (ACI edit-validation, the two-oracle gate).

## Locked decisions

Every load-bearing fork is decided. Detail + rationale live in the architecture wiki (linked).

| Area | Decision | Where |
|---|---|---|
| **Topology** (Built 🟢 Phase 3) | Ephemeral **per-run Jobs** (coordinator + worker), the work-queue Job pattern, **sentinel shutdown** | [`02`](architecture/02-cloud-architecture.md#run-lifecycle) |
| **Tenancy** (Built 🟢 Phase 3) | **Namespace per run**; `ResourceQuota`/`LimitRange` loose to start (separation now, tighten later) | [`02`](architecture/02-cloud-architecture.md#tenancy--namespace-per-run) |
| **Queue/state** (manifest Built 🟢 Phase 2) | In-cluster **Redis StatefulSet + PVC + AOF**; per-run keyspace | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Worker storage** | **`emptyDir`** (durability is via git push); shared-PVC ruled out by DO RWO | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Control plane** (CLI Built 🟢 P2, triggers Built 🟢 P4) | **CLI + CronJobs + webhook listener** → one `create_run()`; operator/CRD = v2 | [`02`](architecture/02-cloud-architecture.md#triggers-the-ch-12-trigger-idea-as-infrastructure) |
| **CLI ↔ k8s** (Built 🟢 Phase 2) | Python **kubernetes client** (`loopkit[cloud]`); cloud-agnostic; runs laptop **or** in-cluster; **context-safety guard** pins the DOKS context | [`02`](architecture/02-cloud-architecture.md#control-plane--one-path-three-entry-points) |
| **Worker scaling** | **Fixed `--workers N`** for v1; KEDA `ScaledJob` later | [`02`](architecture/02-cloud-architecture.md#scaling) |
| **Registry/image** (Built 🟢 Phase 1) | **GHCR**, **multi-arch amd64** built via GitHub Actions (not `kind load`); `imagePullSecret` recipe | [`02`](architecture/02-cloud-architecture.md#image--registry-pipeline) |
| **Adapters** | Full 2×2: `claude-code` / `claude-api` / `codex` / `openai-api` behind the `Agent` protocol | [`03`](architecture/03-adapters-and-auth.md#the-agent-protocol--the-22-adapter-matrix) |
| **Agent auth** (Built 🟢 Phase 5a) | **Per-submitter** key resolved by `(env, submitter)`, adapter selects/projects the var; registered set = fail-closed allowlist; Vault = a later resolver swap | [`03`](architecture/03-adapters-and-auth.md#the-pluggable-credential-model) |
| **Billing** | Dedicated **API key for prod** (subscription subsidy ended 2026-06-15); subscription token for dev | [`03`](architecture/03-adapters-and-auth.md#billing--cost-control) |
| **Skills home** (Built 🟢 Phase 5b) | Dedicated **`loopkit-skills` git repo** (cross-run learned state); `GitSkillRegistry` clones at start + gated push on `DONE`, rebase-retry for concurrent pods, loopkit-core's git token | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) · [`5b`](part-iii-skills-repo.md) |
| **Security** (Built 🟢 P2–P6) | Ch 16 envelope extended: default-deny + per-run **Cilium FQDN** egress, least-priv SAs (no write verbs on the listener SA), **credential withheld from the agent** (load-shred + scrub + redact + pre-push scan), branch-only/draft PRs, context guard, **+ P6 agent isolation: the untrusted tool surface runs in a keyless, different-uid/PID-ns executor sidecar (kernel boundary, closes the same-uid in-pod key-read residual)** | [`04`](architecture/04-security.md) |
| **Observability** (Built 🟢) | Two layers: payload-free logs (`log.py`) **+** full-tree **LangSmith traces** (`trace.py`, optional `[trace]`, auto-on, `None`-safe); per-span cost via `pricing.py` | [`01`](architecture/01-system-today.md#observability--two-layers-logs--traces) |

## Sharp edges to carry (paid for or foreseen)

- **arm64 → amd64.** Local Colima/Apple Silicon is arm64; DO nodes are amd64. Prod images must be
  amd64/multi-arch, pushed to GHCR — not `kind load` (which the Tiltfile uses for the Docker-29
  containerd workaround).
- **DO block storage is ReadWriteOnce** — no shared-across-nodes PVC. `emptyDir` for workers sidesteps
  it entirely.
- **Subscription billing changed 2026-06-15** — headless Claude Code now draws a capped agent-credit
  pool at API rates; the subscription is **not** a cheap way to run a fleet. Use a dedicated API key
  for prod. (And `claude-code` now defaults to the subscription + withholds an ambient API key — opt
  into billing with `run --api-key`.)
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

## Open / deferred decisions + hardening

Forward-looking; none blocks the live steps. Decisions:

- GitHub **App** vs PAT/deploy-keys for clone/push/PR at scale (App is the eventual answer for
  multi-repo + PR creation).
- **KEDA** timing (when a single run needs to fan very wide); **ESO/Vault** for secrets (a resolver
  swap, not a redesign); observability stack choice (DO managed logs vs Loki/Grafana); an operator +
  **`LoopRun` CRD** as the v2 declarative control plane.

Hardening (the 🟡 backlog, all post-v1):

- Node pools (system + autoscaling worker) via the DO cluster autoscaler.
- Lifecycle GC: `ttlSecondsAfterFinished`, namespace GC, Redis keyspace cleanup, orphaned-branch
  cleanup.
- Mid-task pod death: `backoffLimit` retries + task re-pop (commit-every-tick is *local* durability
  until the DONE-push; a push-every-tick WIP branch would make runs resumable).
- Observability stack; the deferred dashboard; a run-history store (GitHub PRs + `kubectl get jobs`
  suffice for v1).

## Next step

**Code-only tracks remaining (no cluster/remote needed):**

1. **Security E — Redis AUTH (build next, per the plan).** Shared Redis has no `requirepass`/ACL and
   the per-run NetworkPolicy allows `:6379` from all pods → a prompt-injected agent can read/write
   other runs' keyspaces. Add a per-run Redis password (in the run Secret) or an ACL per keyspace
   ([`part-iii-security-review.md`](part-iii-security-review.md), Finding E).
2. **Measurement layer (rest)** — `pass^k` + `cost_per_accepted` are built; the open thread is a
   persisted corpus of harness-stamped reports + the full pass^k-vs-cost / convergence axes (fed by an
   offline-re-gradeable trajectory log). loopkit's strategic contribution candidate.
3. **Phase 6 (rest) — observability + the v2 layer** (logs/metrics, the read-only dashboard;
   KEDA/ESO/Vault/GitHub App; a separate-pod executor split = the real fix for same-pod 443
   content-exfil, Finding F).
4. **Cloud-tier revise** — plumb `branch` through `RunSpec → worker_command` so the webhook listener
   can dispatch revise runs instead of deferring them 204 (the parse/dedupe/policy layers are done);
   GitLab revise stays out until GitLab grows a changes-requested-equivalent MR primitive.

**Then live enablement (needs external resources):**

1. **Push to a GitHub remote** → the `worker-image` workflow builds amd64 on Actions and pushes to
   GHCR. (The corp Zscaler proxy blocks the *emulated* amd64 cross-build locally — a dev-only TLS
   edge; Actions has no such proxy. Native build + in-container smoke already pass here.)
2. **Provision a DOKS cluster** (`make cloud-provision` prints the `doctl` recipe; `make
   cloud-kubeconfig` writes the **repo-local** `.kube/loopkit-cloud.yaml`), then `make cloud-doctor`
   → `make cloud-bootstrap` → `loopkit cloud run …`. This live-verifies what unit tests can't:
   **Redis durable across a pod restart** (P2), **one real end-to-end run** (P3 — Jobs produce a
   branch + draft PR, `evolve` reseeds, the namespace is GC'd), a **live webhook/CronJob firing**
   (P4), the **multi-tenant creds proof** (P5a), and the **Phase-6 ptrace-fails proof** (P6). The
   guards, kubeconfig isolation, sentinel mechanic, and run topology are already proven locally.

Carry the invariants in [`../CLAUDE.md`](../CLAUDE.md): extend at the seams, `None`-safe, thin stack,
test-as-you-go, log-as-you-go, **trace-as-you-go**, **credentials never reach the agent's reach**, and
**every mutating cloud command goes through the context guard**. **Update this doc and the
architecture wiki as each phase lands** (the documentation contract).
