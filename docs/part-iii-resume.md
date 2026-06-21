# loopkit — Part III resume (cloud productionization)

**Read this first when picking up Part III.** It is the single source of truth for the current
phase: state, locked decisions, the build sequence, sharp edges, and the next step. For *how the
system is built/designed*, read the architecture wiki: [`architecture/`](architecture/README.md).
The auto-memory `project_loopkit` only points here.

> **Current state (2026-06-21):** **Phases 0–5a built; the two live steps (a GitHub remote, a real
> DOKS cluster) are the only things outstanding.** Part II (the extension library) and the dev
> kind/Tilt fleet are done and verified live (see [`part-ii-resume.md`](part-ii-resume.md)).
> **Phase 5a** (this session) landed **per-submitter credentials, hardened end-to-end against the
> prompt-injection flow** — red-teamed by a multi-agent pass + a lifecycle trace before build. New
> core **`secrets.py`** (load-then-shred creds off the FS + `os.environ`, `child_env` scrubs every
> untrusted-driven subprocess, redaction registry); new **`extensions/creds.py`** (identity→Secret
> resolver, key-projection, fail-closed fallback, S4 injective check, `cloud creds set/ls/rm`); the
> per-run Secret is now delivered via an **init-container→memory-tmpfs→shred** path (not envFrom, not
> a co-located mount) with a hardened **securityContext**; the **webhook binds the run to the issue
> author** (default-deny allowlist, 403-no-run before the dedupe reserve, `release()` on failure);
> **CLI adapters are hard-refused** on triggers (default `claude-api`); a **pre-push secret scan**,
> **token-in-URL sanitize**, **per-run Cilium FQDN egress**, and **RBAC narrowed** (no write verbs on
> the listener SA, `list`→`get`). 219 tests green (was 164). **Honest residual:** a same-uid in-pod
> memory/ptrace read of the in-process key remains until the agent shell runs in a separate
> PID-namespace container (deferred) — 5a closes every env/file/argv/URL/log/trace/gate/repo/network
> path, and does not claim "the agent never sees it."
> **Phase 0** (branch `phase-0-adapters-tracing`) landed the pure-library foundation (2×2 adapter
> matrix, `pricing.py` cost, full-tree LangSmith tracing). **Phase 1** added the **`worker-image`
> GitHub Actions workflow** (buildx multi-arch `amd64` → GHCR + an in-CI amd64 smoke test) +
> `imagePullSecret`, verified locally. **Phase 2** landed the **cloud control-plane foundation**: the
> non-negotiable **context-safety guard** (`extensions/cloud.py`, fail-closed, deferred-import behind
> `[cloud]`), the `loopkit cloud` sub-app (`context`/`doctor`/`bootstrap`), the **`k8s/cloud/` system
> manifests** (`loopkit-system`, **Redis StatefulSet AOF+PVC**, `loopkit-control` RBAC, default-deny
> NetworkPolicy), and repo-local-`KUBECONFIG` Makefile targets. **Phase 3** (this session) landed the
> **per-run mechanics**: **sentinel shutdown** (`fleet.py` — coordinator drains N sentinels at true
> completion, ephemeral worker pods exit 0; survives the gaps between `evolve` generations), the
> per-run **keyspace** + `--drain-workers` on the fleet CLI, the **`extensions/cloudrun.py`** run
> topology (pure builders for `ns/run-<id>` + coordinator/worker Jobs — work-queue `parallelism N` /
> `completions` unset, `emptyDir` scratch, no-API worker SA, per-run default-deny NetworkPolicy,
> per-run Secret) + **`create_run`/`delete_run`/`list_runs`/`run_status`/`run_logs`** (guard-first,
> injectable seams), and the **`loopkit cloud run/ls/status/logs/kill`** CLI. **Phase 4** (this
> session) landed the **triggers**: the **in-cluster auth path** (`cloud.api_client(in_cluster=True)`
> / `current_context(in_cluster=True)`, proven-in-pod via `load_incluster_config`, guard pins a
> synthetic, un-spoofable `in-cluster` context; `--in-cluster` on `cloud run`), the
> **`extensions/triggers.py`** module — a **`WebhookProvider`** abstraction with **GitHub** (HMAC-SHA256
> body signature) **and GitLab** (static `X-Gitlab-Token`, `object_attributes` payload) front-ends,
> both fail-closed, feeding one `WebhookApp.dispatch` (verify → parse → dedupe → `create_run`);
> issue-identity idempotency (in-memory **or** Redis `SET NX EX`) under a stdlib `http.server` shell;
> the sweep path (`--from-issues`) gained a `--provider` so self-hosted GitLab works too. The
> **CronJob** builder (`build_cronjob` runs `loopkit
> cloud run --in-cluster` as `loopkit-control`) + `create/delete/list_schedule`, the **`loopkit cloud
> schedule/schedules/unschedule/webhook`** CLI, and the **opt-in** `k8s/cloud/webhook/` manifests
> (Deployment + paid LoadBalancer, kept out of the bootstrap glob). The worker image now installs
> `[fleet,cloud]` (the trigger pods need the k8s client). **164 tests green** (was 125; +39). **Gating
> items:** (1) **no git remote** → the worker-image workflow hasn't run on Actions / nothing in GHCR;
> (2) **no DOKS cluster yet** → `bootstrap` + `create_run` + the triggers are unit-verified (guard
> refuses a wrong context before any apply; the run/cron topology is asserted object-by-object; the
> webhook decision tree — forged 401, ping, ignored, one-run-per-issue dedupe — is asserted with an
> injected `create`) but not yet live-applied. The architecture is decided in
> [`architecture/`](architecture/README.md); the next build step is **Phase 5** below.

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
| **Skills home** | Dedicated **`loopkit-skills` git repo** (cross-run learned state) | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) |
| **Security** (Built 🟢 P2–P5a) | Ch 16 envelope extended: default-deny + per-run **Cilium FQDN** egress, least-priv SAs (no write verbs on the listener SA), **credential withheld from the agent** (load-shred + scrub + redact + pre-push scan), branch-only/draft PRs, context guard | [`04`](architecture/04-security.md) |
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
- **Phase 3 — Run mechanics ✅ BUILT (live end-to-end run pending a DOKS cluster).** Landed the core
  integration as code + tests. **Sentinel shutdown** (`fleet.py`): the coordinator enqueues N
  sentinels at *true* completion (after the final `evolve` generation), each ephemeral worker pod
  pops one and exits 0 — `Worker.run_forever` honours the sentinel, `Coordinator.drain(N)` +
  `run_fleet/evolve(drain_workers=N)` push them; the fleet CLI exposes `--redis-namespace` (per-run
  keyspace) + `--drain-workers`. **`extensions/cloudrun.py`**: `RunSpec` + pure builders for the
  per-run topology (`ns/run-<id>`, no-API worker SA, ResourceQuota/LimitRange, default-deny
  NetworkPolicy + egress allowlist, optional per-run Secret, **coordinator Job** running
  `fleet run|evolve --drain-workers N`, **worker Job** `parallelism N`/`completions` unset/`emptyDir`
  scratch), and `create_run/delete_run/list_runs/run_status/run_logs` (guard-first, injectable
  applier/deleter/lister for token-free tests). **`loopkit cloud run/ls/status/logs/kill`** CLI, all
  guarded. *Acceptance:* the run topology + sentinel mechanic + guard-before-apply are **proven by
  unit tests** (125 green: sentinel drain via InMemoryQueue+MockAgent; builder spec object-by-object;
  `create_run` refuses a wrong context before applying anything); the **one real end-to-end run on
  DOKS** (branch + draft PR, `evolve` reseed, namespace GC) is the part that **awaits a live cluster**.
- **Phase 4 — Triggers ✅ BUILT (live firing pending a DOKS cluster).** Landed the two event entry
  points as code + tests, both converging on the Phase-3 `create_run()`, for **both GitHub and
  GitLab** (a `WebhookProvider` isolates the per-forge auth scheme + payload shape). **In-cluster auth**
  (`cloud.py`): `--in-cluster` switches `cloud run` to `load_incluster_config()`; the guard is
  preserved (a synthetic `in-cluster` context, *proven* by the in-cluster config loading — impossible
  on a laptop — and still refused unless explicitly pinned). **`extensions/triggers.py`**: the
  **webhook** path (a `WebhookProvider` with GitHub `verify_signature` HMAC-SHA256 + GitLab
  `verify_token` static `X-Gitlab-Token`, both constant-time/fail-closed;
  `parse_event`/`parse_gitlab_event`; `should_trigger` label gate; `event_to_run_spec`;
  `InMemory`/`Redis` idempotency; a pure
  `WebhookApp.dispatch` — 401/200/204/202/200-dup/400/500 — under a stdlib `http.server` shell) and
  the **CronJob** path (`ScheduleSpec` + `cronjob_command` + `build_cronjob` running `loopkit cloud
  run --in-cluster` as `loopkit-control`; `create/delete/list_schedule`, guard-first). **CLI**
  `loopkit cloud schedule/schedules/unschedule/webhook` (`webhook --provider`) + `--in-cluster` on
  `run`; a `--provider` on the `--from-issues` sweep so self-hosted GitLab works. Opt-in
  `k8s/cloud/webhook/` manifests (Deployment + paid LoadBalancer, excluded from the bootstrap glob);
  Dockerfile now installs `[fleet,cloud]`. *Acceptance:* the webhook decision tree + CronJob topology
  + guard-before-apply are **proven by unit tests** (164 green: forged delivery/bad token → 401 and no run; a
  signed issue → exactly one run; a re-delivery/second matching event → dedup, still one run; the
  CronJob fires `cloud run --in-cluster` as `loopkit-control`). The **one real scheduled firing + a
  live signed delivery** await a DOKS cluster.
- **Phase 5a — Per-submitter creds ✅ BUILT (live multi-tenant proof pending a DOKS cluster).** The
  identity→Secret resolver (`extensions/creds.py`) + the worker-side credential hygiene (`secrets.py`)
  + the hardened delivery/identity/policy changes across `cloudrun`/`triggers`/`cli` + the
  RBAC/network/webhook manifests. Red-teamed before build (4 adversarial lenses + a lifecycle trace).
  *Acceptance:* **proven by 216 token-free tests** — projection drops the off-adapter key; the resolver
  is fail-closed without a registered key; `run_bash`/gate spawn credential-free; the pre-push scan
  refuses a leaking diff; the webhook binds to the issue author + refuses an unregistered submitter
  without burning the dedupe key. The **live** proof (two engineers' keys; a hijacked run's
  `printenv`/`cat /var/run/loopkit/creds` yields nothing; FQDN egress blocks an off-allowlist POST)
  **awaits a DOKS cluster**.
- **Phase 5c — CI deployment tier (BUILD NEXT).** Run the single loop from **GitHub Actions / GitLab
  CI** — the forge is the trigger/scheduler/secret-store/identity/sandbox, so it needs **no cluster**
  and almost no new code (glue over `parse_event`/`issues.fetch_issues`/`remote.sync_done`). Designed in
  **[`part-iii-ci-mode.md`](part-iii-ci-mode.md)**. This adds the middle of the **three deployment
  tiers** — *local* (`loopkit run`) · *CI* (forge-triggered single loop, no infra) · *cloud fleet*
  (concurrent/`evolve`/multi-tenant). Additive: touches no cloud code. *Acceptance:* a labeled issue →
  a draft PR that closes it, with `MockAgent` covering the `--from-event` path token-free.
- **Phase 5b — Skills repo.** `loopkit-skills` repo wired into the worker (read at start + gated
  write-back on `DONE`). *Acceptance:* a solved run writes a skill back that a later run reads.
- **Phase 6 — Agent isolation (the residual-closer) + observability.** The headline hardening is the
  **sidecar / keyless-executor split** that closes the same-uid in-pod memory-read residual 5a can't
  close in a single container — **designed in [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md)**
  (next-session plan; independent of 5b). Then logs/metrics shipping, the read-only dashboard, and the
  v2 layer (KEDA, ESO/Vault, GitHub App, tighter quotas) — see *the ecosystem-vs-hand-rolled map* in
  that doc's spirit: an operator + `LoopRun` CRD, Argo Events for the webhook, KEDA `ScaledJob` for the
  queue, a GitHub App for auth, all of which replace thin slices we hand-rolled for the v1.

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
- ✅ Webhook **HMAC + idempotency** — *built (Phase 4)*: GitHub `verify_signature` (HMAC) + GitLab
  `verify_token` (static token), both fail-closed, + issue-identity dedupe (in-memory / Redis `SET NX
  EX`) in `WebhookApp.dispatch`. Goes live with a cluster + the opt-in `k8s/cloud/webhook/` Deployment.
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
2. **Make Phases 2–3 live — provision a DOKS cluster** (`make cloud-provision` prints the `doctl`
   recipe with the system + autoscaling worker node pools), `make cloud-kubeconfig` (writes the
   **repo-local** `.kube/loopkit-cloud.yaml`), then `make cloud-doctor` → `make cloud-bootstrap` →
   `loopkit cloud run --target <repo> --goal … --image ghcr.io/<owner>/loopkit-worker:<tag>`. That
   live-verifies the acceptance unit tests can't reach: **Redis durable across a pod restart** (P2 —
   kill `redis-0`, confirm queue/results survive via AOF+PVC) and **one real end-to-end run** (P3 —
   coordinator+worker Jobs produce a branch + draft PR, `evolve` reseeds, the namespace is GC'd on
   completion / `loopkit cloud kill`). The guards, the kubeconfig isolation, the sentinel mechanic,
   and the run topology are already proven locally.

   When the cluster exists, **Phase 4 also goes live**: `make cloud-webhook` deploys the opt-in
   listener (after creating the `loopkit-webhook` Secret), then point a GitHub repo webhook at the
   LoadBalancer with the HMAC secret and confirm a signed `issues` delivery starts exactly one run
   (and a forged/duplicate one does not); `loopkit cloud schedule … --cron …` creates a CronJob and
   the next firing produces a run. The HMAC/idempotency/CronJob logic + the in-cluster guard are
   already proven locally; only the live firing needs the cluster.

**Phase 5a (per-submitter creds) is built and tested** — the env-grab is replaced by the
identity→Secret resolver, the key is withheld from the agent, and the trigger paths bind the run to
the issue author. **The next two sessions are scoped and designed:**

1. **Phase 5c — the CI deployment tier (BUILD FIRST), designed in
   [`part-iii-ci-mode.md`](part-iii-ci-mode.md).** Run the single loop from GitHub Actions / GitLab CI:
   `loopkit run` gains `--from-event`/`--from-issue`/`--open-pr` (glue over the existing
   `parse_event`/`issues.fetch_issues`/`remote.sync_done`), plus two shipped workflow templates. It
   needs **no cluster** and works today, so it's the cheapest accessibility win and the most teachable
   realization of Ch 12 + Ch 16. CI is the forge's job for secrets/identity/sandbox — **none** of the
   cloud creds machinery applies there (that stays the cloud tier's).
2. **Phase 6 — agent isolation (the residual-closer), designed in
   [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md).** The **sidecar / keyless-executor
   split**: run the untrusted tool surface as a different uid/PID-namespace container with **no key**,
   replacing the timing-dependent shred with a kernel boundary. Closes the same-uid in-pod memory-read
   residual for the API-adapter path. (Build after the CI tier — the residual is only live-exploitable
   once a DOKS cluster runs, which is still gated on provisioning.)

**Still queued:** Phase 5b (the `loopkit-skills` cross-run flywheel — `SkillRegistry` exists, needs a
durable git-repo home), the two live steps (a GitHub remote → GHCR; a DOKS cluster → live-apply 2–5a),
and the rest of Phase 6 (observability, KEDA/ESO/Vault/GitHub App). Carry the invariants in
[`../CLAUDE.md`](../CLAUDE.md): extend at the seams, `None`-safe, thin stack, test-as-you-go,
log-as-you-go, **trace-as-you-go**, **credentials never reach the agent's reach**, and **every mutating
cloud command goes through the context guard**. **Update this doc and the architecture wiki as each
phase lands** (the documentation contract).

## Changelog

- **2026-06-21 — Phase 5a built (per-submitter creds, hardened against the prompt-injection flow).**
  Red-teamed before build (a multi-agent pass — 4 adversarial attack lenses → per-finding verify →
  synthesis — plus an end-to-end lifecycle trace), which **reversed an earlier draft**: a plain
  file-mount is co-located with the agent's own uid (`cat` defeats it), and deriving *whose* key from
  attacker-controlled webhook JSON is a confused deputy. **New core `secrets.py`** (stdlib-only):
  `CredentialStore.load` reads creds off a memory-tmpfs into process heap then **`os.remove`s the
  files + deletes the vars from `os.environ`** (so `printenv`/`cat` find nothing once agent code
  runs), `child_env()` scrubs every untrusted-driven subprocess (run_bash/gate/review get **none**;
  the vendor CLI gets **only its model key**), a **redaction registry** (`trace._cap` + exception
  log sites), `setrlimit(RLIMIT_CORE, 0)`. **New `extensions/creds.py`**: `Identity`/`secret_name`
  ((env, submitter)), `SecretResolver` with **key-projection** (only the adapter key + git; coordinator
  = git-only), an **S4 injective check** (recorded canonical identity must match), `resolve_for_run`
  (**fail-closed** default-deny on triggers), guard-first `set/list/delete_credential`. **`cloudrun`**:
  delivery via **init-container→`emptyDir{medium:Memory}`→shred** (no envFrom, no agent-readable mount)
  + a hardened **securityContext** (non-root/drop-ALL/ro-rootfs); `RunSpec.submitter`; **two
  projections**; `create_run` **deletes the ns on apply failure**; a per-run **Cilium FQDN egress**
  policy (best-effort). **`triggers`**: submitter = **issue author** (drop `sender.login`); GitLab uses
  a **pinned listener identity** (forgeable token); **CLI adapters hard-refused** + default
  **`claude-api`**; resolve+authorize **before** the dedupe reserve with a `release()` on failure
  (G6); the CronJob carries **no static creds** (G14). **`cli`**: `cloud creds set/ls/rm` (env/stdin
  only — never a key in argv), `--as`/`--from-env`/`--allow-fleet-fallback` on run/schedule, the
  webhook refuse-then-zeroize path, a `cloud doctor` creds row, the worker G7 fail-closed key check.
  **`pre-push secret scan`** + **token-in-URL sanitize** in `remote.py`/`fleet.py`. **RBAC narrowed**
  (20-rbac): secrets `create,get,delete` — **no `list`, no `update`/`patch`** on the listener SA; the
  webhook Secret + CronJob drop their static keys. **164 → 219 tests green** (+`test_secrets`,
  `test_hygiene`, `test_creds`; +cloudrun/triggers/cloud). **Honest residual (documented in 04):** a
  same-uid `ptrace`/heap read of the in-process key, closed only by a separate-PID-namespace agent
  container (deferred); 443-exfil of *allowed-host* content; redact-by-value is a backstop, not a
  boundary. **Production-readiness pass (same session) fixed 5 runtime bugs the structural tests
  couldn't see:** (1) the Cilium FQDN policy is a **CRD** — `utils.create_from_dict` can't build a
  `CiliumIoV2Api` and would have `AttributeError`'d *every* `create_run`; now routed through
  `CustomObjectsApi`, best-effort. (2) The **coordinator** (`fleet run`/`evolve`) didn't load creds, so
  `--from-issues`'s `gh` had no token (now a file, not env) — added `secrets.install` to both. (3) The
  init `cp -Lr …/.` copied the k8s `..data` metadata dir, so a key survived a tmpfs **subdir** the shred
  missed — fixed to `cp -L …/*` + a recursive shred. (4) `api_key()` could hand an **OAuth token** to
  the Anthropic SDK (rejects it) — now the precise `_SDK_KEY` only. (5) Pinned the worker image **uid
  1000** so the pod `securityContext` lands on the right user. Updated
  [`03`](architecture/03-adapters-and-auth.md), [`04`](architecture/04-security.md),
  [`02`](architecture/02-cloud-architecture.md).

- **2026-06-20 — Phase 4 follow-on: GitLab webhook support.** Refactored the webhook front-end behind
  a **`WebhookProvider`** abstraction (the only per-forge bits are *how to authenticate* + *how to read
  the payload*; idempotency / `event_to_run_spec` / `create_run` stay provider-neutral). Added
  `verify_token` (GitLab static `X-Gitlab-Token`, constant-time/fail-closed — honest caveat: not bound
  to the body, unlike GitHub HMAC) + `parse_gitlab_event` (`object_kind:issue`, `object_attributes`,
  `iid`, `description`, `project.git_http_url`, top-level `labels[].title`; GitLab `open/reopen/update`
  normalized to the GitHub vocabulary). `WebhookApp.dispatch` now takes raw `headers` and delegates to
  `GitHubProvider`/`GitLabProvider` (`provider_for`); `cloud webhook --provider github|gitlab`
  (`LOOPKIT_WEBHOOK_PROVIDER`). Also threaded `--provider` through the `--from-issues` *sweep* path
  (`RunSpec.provider`/`ScheduleSpec.provider` → `fleet run --provider`) so self-hosted GitLab (whose
  URL `detect_provider` can't auto-detect) works on the CronJob path too. New GitLab tests + the
  GitHub dispatch tests moved to the headers API — **154 → 164 green**. Updated
  [`02`](architecture/02-cloud-architecture.md) + [`04`](architecture/04-security.md) (webhook now
  GitHub *and* GitLab; the token-vs-HMAC security caveat is documented).
- **2026-06-20 — Phase 4 built (triggers: in-cluster auth + CronJob + webhook listener).** Added the
  **in-cluster auth path** to `cloud.py` (`IN_CLUSTER_CONTEXT`; `current_context(in_cluster=True)` /
  `api_client(in_cluster=True)` use `load_incluster_config()` — which only loads inside a real pod, so
  it reports a synthetic, un-spoofable `in-cluster` context the guard still must have explicitly
  pinned). Threaded `in_cluster` through `cloudrun.create_run` + `_client_applier`. New
  **`loopkit/extensions/triggers.py`**: `verify_signature` (HMAC-SHA256, constant-time, fail-closed),
  `WebhookEvent`/`parse_event`/`should_trigger`/`event_to_run_spec`, `IdempotencyStore` (`InMemory` +
  `Redis` `SET NX EX`), a pure `WebhookApp.dispatch` (forged→401, ping→200, ignored→204, valid→202,
  duplicate→200, bad-JSON→400, create-raise→500) under a stdlib `http.server` `serve()` shell, and the
  CronJob side (`ScheduleSpec` + `cronjob_command` + `build_cronjob` running `loopkit cloud run
  --in-cluster` as `loopkit-control`; `create/delete/list_schedule`, guard-first, injectable seams).
  CLI: `loopkit cloud schedule/schedules/unschedule/webhook` + `--in-cluster` on `cloud run` (implies
  `--yes`, non-interactive). Opt-in `k8s/cloud/webhook/` manifests (Deployment + paid LoadBalancer +
  `secret.example.yaml`), excluded from the bootstrap glob (subdir, non-recursive `*.yaml`); a
  `cloud-webhook` Makefile target applies only the Deployment+Service via explicit `--context=`.
  Dockerfile now installs `[fleet,cloud]` (trigger pods need the k8s client). New
  `tests/test_triggers.py` (29) + fixed the `test_cloudrun.py` `pinned` fixture for the new
  `in_cluster` kwarg — **125 → 154 green**. Deferred-import invariant holds (triggers pulls no
  `kubernetes`/`redis` at import). **Gating item:** no DOKS cluster, so the live scheduled firing +
  signed delivery aren't yet exercised. Updated [`02`](architecture/02-cloud-architecture.md) (triggers
  / in-cluster auth / CLI surface now Built 🟢) and [`04`](architecture/04-security.md) (webhook HMAC +
  idempotency now Built 🟢).
- **2026-06-20 — Phase 3 built (run mechanics: sentinel shutdown + per-run Jobs).** `fleet.py` gained
  **sentinel shutdown**: `sentinel_task()`/`is_sentinel()`, `Worker.run_forever` exits 0 on a
  sentinel, `Coordinator.drain(N)` + `run_fleet/evolve(drain_workers=N)` enqueue N at *true*
  completion (evolve drains only after the final generation — workers must survive the inter-gen
  gaps). Fleet CLI gained `--redis-namespace` (per-run keyspace on `worker`/`run`/`evolve`) +
  `--drain-workers` (`run`/`evolve`). New **`loopkit/extensions/cloudrun.py`**: `RunSpec` +
  `sanitize_run_id` + pure builders (`build_namespace`/`_worker_sa`/`_resource_quota`/`_limit_range`/
  `_network_policy`/`_creds_secret`/`_coordinator_job`/`_worker_job` + `build_run_objects`) and the
  command builders (`coordinator_command` carries `--drain-workers`; `worker_command` carries the
  per-run keyspace), then `create_run`/`delete_run`/`list_runs`/`run_status`/`run_logs` — each runs
  the context guard first and takes an injectable applier/deleter/lister for token-free tests. Worker
  Job = the fine-grained work-queue pattern (`parallelism N`, `completions` unset, `emptyDir` scratch
  with sizeLimit, no-API SA, `imagePullSecrets`, `ttlSecondsAfterFinished`, `restartPolicy: Never`).
  CLI `loopkit cloud run/ls/status/logs/kill` (all guarded; `run`/`kill` confirm before mutating).
  New `tests/test_cloudrun.py` (20) + sentinel tests in `test_fleet.py` (4) + cloud-CLI guard tests
  (3) — **98 → 125 green**. Deferred-import invariant holds (cloudrun pulls no `kubernetes` at
  import). **Gating item:** no DOKS cluster, so the one live end-to-end run isn't yet exercised.
  Updated [`02`](architecture/02-cloud-architecture.md) (run lifecycle / work-queue / CLI now Built
  🟢) and [`04`](architecture/04-security.md) (per-run NetworkPolicy + worker SA + per-run Secret now
  Built 🟢).
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
