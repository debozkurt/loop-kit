# loopkit — Part III resume (cloud productionization)

**Read this first when picking up Part III.** It is the single source of truth for the current
phase: state, locked decisions, the build sequence, sharp edges, and the next step. For *how the
system is built/designed*, read the architecture wiki: [`architecture/`](architecture/README.md).
The auto-memory `project_loopkit` only points here.

> **Current state (2026-06-21):** **All of Part III is now built (Phases 0–6, incl. Phase 5b this
> session); the only outstanding work is the two live steps — a GitHub remote and a real DOKS cluster —
> and the rest of Phase 6 (observability + the v2 layer).**
> **Phase 5b (this session) landed the `loopkit-skills` git repo — the cross-run flywheel made durable
> across machines.** Ch 17's `FileSkillRegistry` was durable across processes on one filesystem; the cloud
> fleet's ephemeral pods share none, so the flywheel needed a network home. New **`GitSkillRegistry`**
> (`extensions/skills.py`, composing `FileSkillRegistry`): **clone the skills repo at run start** (read
> edge — every prior lesson rendered into the prompt), **gated commit + push on `DONE`** (write edge),
> with a `fetch`+`rebase`+retry so concurrent worker pods don't lose a write (skills are one file per
> name → file-disjoint). The transport (`_SubprocessGitTransport`, injectable) reuses a new public
> **`remote.run_git`** for credential hygiene, never force-pushes, and is **best-effort** (a sync failure
> WARNs, never fails the run that earned the skill). Wired into the worker with one flag —
> **`fleet worker --skills-repo`** (`make_repo_runner(skills_repo=…)` builds a per-task `GitSkillRegistry`
> gated by the held-out acceptance gate, executor-aware) — and the cloud path
> **`cloud run --skills-repo`** → `RunSpec.skills_repo/skills_branch` → `worker_command` (worker only;
> the coordinator does no write-back). **Reuses loopkit-core's git token (no new Secret) over the
> already-allowlisted github.com egress — zero new infra.** **264 tests green** (was 252; +12 in
> `tests/test_skills_repo.py`, all against a real local bare repo, no tokens/network) + the runnable
> **`loopkit demo 23`** (two pods sharing only a git repo: A learns + pushes, B clones + solves tick 1).
> Documented in [`part-iii-skills-repo.md`](part-iii-skills-repo.md). Live-pending: pointing it at a real
> GitHub `loopkit-skills` repo (needs the remote + a cluster).
> **Phase 6 (prior step this session) landed agent isolation — the keyless-executor sidecar split**, closing the one
> residual Phase 5a could not close in a single container (a same-uid `ptrace`/heap read of the in-process
> key) for the cloud worker, by construction. New core **`executor.py`**: a `ToolExecutor` seam
> (`dispatch`/`run_gate`) with **`LocalToolExecutor`** (in-process default — exact prior behavior) and
> **`RemoteToolExecutor`** (length-prefixed-JSON Unix-socket client, degrade-on-unreachable) + a `serve()`
> server and a new **`loopkit executor`** CLI command (the keyless sidecar). `_APIAdapter`/`ShellGate`/
> `run_loop`/`build_agent`/`make_repo_runner` all take an injected executor; the **cloud worker**
> (`fleet worker --executor-socket`) injects `RemoteToolExecutor` so the agent's tool calls + the held-out
> gate run in a **different-uid, separate-PID-namespace, credential-free** container, while loopkit-core
> holds the key only for the LLM call + git. **`cloudrun._pod_spec` rewritten to the two-container split**:
> loopkit-core gets creds via **`envFrom`** (dropping the init-container→tmpfs→shred), the executor is a
> **native sidecar** (initContainer `restartPolicy: Always`) with no Secret, a **shared workspace + socket
> emptyDir** (`fsGroup`, `umask 002`), uid 1001. **Refinement from the design doc:** `secrets.py`'s
> load-shred + `child_env` scrub were **kept** (not deleted) — they remain the containment for the two
> tiers with **no sidecar** (the CI tier + untrusted local); the sidecar is the cloud-worker kernel
> boundary, and the shred is harmless/redundant inside the split. **252 tests green** (was 241; +9 socket
> round-trip in `tests/test_executor.py`, +2 net pod-spec). Additive to the core contracts, `None`-safe,
> deferred-import invariant preserved. Designed + now documented-built in
> [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md). The **live DOKS proof** (ptrace from the
> executor fails; the run still completes) awaits a cluster.
> **Phase 5c (prior session) landed the CI deployment
> tier** — the middle of the three tiers — so the single loop runs from GitHub Actions / GitLab CI with
> **no cluster**: `loopkit run` gained `--from-event`/`--from-issue`/`--open-pr`/`--adapter` (glue over a
> new `triggers.parse_event_payload` + `issues.fetch_issue` + `remote.sync_done(issue=N)`), `loopkit
> init --ci github|gitlab` scaffolds the workflow, and `examples/ci/` ships both templates. A GitLab
> credential fix (`secrets.GIT_ENV` += `GITLAB_TOKEN`, `remote.CRED_HELPER` GitHub→GitLab fallback) makes
> `glab`/git-push authenticate through the Phase-5a hygiene without ever handing the agent a token. **240
> tests green** (was 219; +21 token-free). Additive — touched no cloud control-plane code. Part II (the
> extension library) and the dev kind/Tilt fleet are done and verified live (see
> [`part-ii-resume.md`](part-ii-resume.md)).
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
| **Skills home** (Built 🟢 Phase 5b) | Dedicated **`loopkit-skills` git repo** (cross-run learned state); `GitSkillRegistry` clones at start + gated push on `DONE`, rebase-retry for concurrent pods, loopkit-core's git token | [`02`](architecture/02-cloud-architecture.md#storage-model--almost-nothing-is-persistent-by-design) · [`5b`](part-iii-skills-repo.md) |
| **Security** (Built 🟢 P2–P6) | Ch 16 envelope extended: default-deny + per-run **Cilium FQDN** egress, least-priv SAs (no write verbs on the listener SA), **credential withheld from the agent** (load-shred + scrub + redact + pre-push scan), branch-only/draft PRs, context guard, **+ P6 agent isolation: the untrusted tool surface runs in a keyless, different-uid/PID-ns executor sidecar (kernel boundary, closes the same-uid in-pod key-read residual)** | [`04`](architecture/04-security.md) |
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
- **Phase 5c — CI deployment tier ✅ BUILT (live drop-in optional).** The single loop now runs from
  **GitHub Actions / GitLab CI** with **no cluster** — the forge is the trigger/scheduler/secret-store/
  identity/sandbox. `loopkit run` gained **`--from-event`** (a new `triggers.parse_event_payload`
  auto-detects the forge by body shape), **`--from-issue`** + **`--provider`** (a new
  `issues.fetch_issue` single-object counterpart of `fetch_issues`), **`--open-pr`** (flips `[remote]`
  on for the invocation, threading the issue number into `remote.sync_done(issue=N)`), and **`--adapter`**
  (the templates pass `claude-api`). **`loopkit init --ci github|gitlab`** scaffolds the workflow + a
  starter `loopkit.toml`/`PROMPT.md`; both templates also ship in **`examples/ci/`** (a drift-guard test
  keeps them identical). A **GitLab credential fix** (`secrets.GIT_ENV` += `GITLAB_TOKEN`,
  `remote.CRED_HELPER` GitHub→GitLab fallback) makes `glab`/git-push authenticate through the Phase-5a
  hygiene while the agent's scrubbed shell still gets nothing. Additive — no cloud code touched. Designed
  + now documented-built in **[`part-iii-ci-mode.md`](part-iii-ci-mode.md)**. *Acceptance met:* a labeled
  issue → a draft PR that closes it, `MockAgent` covering the `--from-event`/`--from-issue` paths
  token-free (`tests/test_ci.py`, 219 → 240 green); the live drop-the-template proof is optional.
  **Teaching form (same session):** two runnable labs — `loopkit demo 20` (triggers-as-infrastructure)
  and `loopkit demo 21` (the CI tier, `--live`-capable) — plus the
  [`part-iii-ecosystem.md`](part-iii-ecosystem.md) module, bring Part III to the Parts I–II
  scenario-per-concept standard.
- **Phase 5b — Skills repo ✅ BUILT (live drop-in pending a remote + cluster).** The `loopkit-skills`
  repo wired into the worker: a new **`GitSkillRegistry`** (composing `FileSkillRegistry`) clones it at
  run start (read edge) and pushes a **gated** write-back on `DONE` (write edge), with `fetch`+`rebase`+
  retry for concurrent pods (skills are one file per name → file-disjoint). Reuses a new public
  `remote.run_git` for credential hygiene, never force-pushes, best-effort (never fails the run).
  `fleet worker --skills-repo` (`make_repo_runner(skills_repo=…)`, gated by the held-out acceptance gate,
  executor-aware) + `cloud run --skills-repo` → `RunSpec` → `worker_command` (worker only). No new Secret
  (loopkit-core's git token), no new infra (github.com egress already allowlisted). Designed + built in
  **[`part-iii-skills-repo.md`](part-iii-skills-repo.md)**. *Acceptance met:* a `run_loop` reaching DONE
  pushes a skill to a local bare repo and a second `run_loop` with a fresh clone **renders it + solves on
  tick 1** — the literal "a solved run writes a skill back that a later run reads," token-free
  (`tests/test_skills_repo.py`, 252 → 264 green); gated/idempotent/bootstrap/concurrent-rebase asserted.
  **Lab `loopkit demo 23`** (two pods sharing only a git repo).
- **Phase 6 — Agent isolation (the residual-closer) ✅ BUILT (live ptrace-fails proof pending a DOKS
  cluster).** The **sidecar / keyless-executor split** that closes the same-uid in-pod memory-read
  residual 5a can't close in a single container — **built this session**, documented in
  [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md). New core `executor.py` (`ToolExecutor`
  seam + `Local`/`Remote` + `serve` + `loopkit executor` CLI); `_APIAdapter`/`ShellGate`/`run_loop`/
  `build_agent`/`make_repo_runner` take an injected executor; `cloudrun._pod_spec` rewritten to a
  two-container worker (loopkit-core + keyless executor native sidecar, creds via `envFrom` into
  loopkit-core only). *Acceptance:* **proven by 252 token-free tests** — the API adapter drives tools
  through a `RemoteToolExecutor` over a real socket (a file is written only if the call crossed the
  wire); `ShellGate` runs the gate remotely; an unreachable executor degrades to a tool/gate error; the
  pod spec asserts two containers, the key `envFrom`'d **only** into loopkit-core, a different-uid native
  sidecar, and no Secret-backed volume anywhere. The **live** proof (`cat /proc/<core-pid>/mem` from the
  executor fails — separate PID namespace — and the run still produces a branch + draft PR) **awaits a
  DOKS cluster**. **Refinement:** `secrets.py`'s shred/scrub were kept (containment for the no-sidecar
  CI + local tiers), not deleted; the worker pod dropped the init/tmpfs delivery for `envFrom`.
- **Phase 6 (rest) — observability.** Logs/metrics shipping, the read-only dashboard, and the v2 layer
  (KEDA, ESO/Vault, GitHub App, tighter quotas) — see *the ecosystem-vs-hand-rolled map*: an operator +
  `LoopRun` CRD, Argo Events for the webhook, KEDA `ScaledJob` for the queue, a GitHub App for auth, all
  of which replace thin slices we hand-rolled for the v1. A **separate-pod** executor split (own network
  namespace) would also close same-pod 443-exfil of *content* — deferred.

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

**Every coded phase of Part III is now built and tested (0–6, incl. 5b).** The env-grab is replaced by
the identity→Secret resolver, the key is withheld from the agent, the trigger paths bind the run to the
issue author, the single loop runs forge-CI-natively with no cluster, the cloud worker's untrusted tool
surface runs in a **keyless, isolated executor container** (a kernel boundary, not a timed shred), and
the cross-run flywheel has a **durable git-repo home** (`loopkit-skills`, clone-at-start + gated
push-on-`DONE`). **The remaining build step is Phase 6 (rest); everything else is live-enablement:**

1. **Phase 6 (rest) — observability + the v2 layer (BUILD NEXT)** (logs/metrics shipping, the read-only
   dashboard; KEDA/ESO/Vault/GitHub App; a separate-pod executor split for same-pod 443-exfil of
   *content*).

**Still queued:** the two live steps (a GitHub remote → GHCR + the optional CI drop-in + the real
`loopkit-skills` repo for the 5b flywheel; a DOKS cluster → live-apply 2–6, including the Phase-6
ptrace-fails proof), and the rest of Phase 6 (observability, KEDA/ESO/Vault/GitHub App). Carry the
invariants in
[`../CLAUDE.md`](../CLAUDE.md): extend at the seams, `None`-safe, thin stack, test-as-you-go,
log-as-you-go, **trace-as-you-go**, **credentials never reach the agent's reach**, and **every mutating
cloud command goes through the context guard**. **Update this doc and the architecture wiki as each
phase lands** (the documentation contract).

## Changelog

- **2026-06-21 — Prior-art pass: ACI + two-oracle-gate lessons adopted; lessons doc in both repos.**
  A grounded survey of the canonical harnesses (Anthropic's own, SWE-agent/OpenHands/Aider/mini-swe,
  the framework runtimes, the eval harnesses) → **[`part-iii-prior-art.md`](part-iii-prior-art.md)**
  (source-by-source mapping: what validates loopkit's bets, what it under-weighted, the ranked
  follow-ups). Verdict: loopkit is unusually well-aligned; the gaps cluster in ACI ergonomics, the
  measurement layer, and intra-run context. **Three cheap, field-validated wins implemented:**
  (1) **edit-time validation** — `executor.validate_syntax` + `_WorkspaceTools._write` refuse a
  broken `.py`/`.json` edit at the tool boundary (SWE-agent's ACI guardrail; the bad state never
  lands); (2) **shaped gate feedback** — `executor.shape_failure_output` (used by `run_gate`) surfaces
  the failing lines + the summary tail, budget-bounded, instead of a blind tail (Anthropic, *Writing
  tools for agents*; short output unchanged); (3) **the two-oracle gate** — optional `gate.regression`
  (held-out PASS_TO_PASS) + `run_loop(regression_gate=…)`, **None-safe** (unconfigured ⇒ acceptance
  alone certifies — exact prior behavior); DONE now requires acceptance AND regression, SWE-bench's
  FAIL_TO_PASS + PASS_TO_PASS. **275 → 287 tests** (+12 `tests/test_aci_gates.py`). Tamper defense
  ("the diff mustn't touch the verifier") was already enforced by the protected-path guard — documented,
  not re-built. **Ported to the course (the loops manual):** new **`loops/prior-art.md`** "Prior Art &
  Lessons from the Field" (the manual's patterns mapped to the canonical sources + the sharper lessons),
  cross-linked from the README index and Ch 19. **Tracked next (not built):** `pass^k` reliability
  metric (the measurement-layer roadmap), a persistent agent scratchpad, a `PreToolUse` hook seam, a
  ranked repo-map, an offline-re-gradeable trajectory log. Docs sweep: updated
  [`01`](architecture/01-system-today.md) (two-oracle + ACI), [`CONTROL-FILES.md`](CONTROL-FILES.md) +
  the root README (`gate.regression`), and the architecture [`README`](architecture/README.md)
  page-map (linked prior-art + security-review).

- **2026-06-21 — Security hardening (full-flow review; Findings A–C fixed).** An adversarial end-to-end
  pass over the cloud flow (focused on the Phase-6 sidecar + the Phase-5b flywheel) surfaced and closed
  three issues — documented in **[`part-iii-security-review.md`](part-iii-security-review.md)** (A–G; A–C
  fixed, D–G tracked). **A (sidecar bypass via git hooks):** loopkit-core runs `git` in the workspace the
  untrusted executor can write, so a planted `.git/hooks/*` or injected `.git/config` would have executed
  **as the key-holder** (and could tamper the PR after the gate). Closed by hardening every loopkit-core
  git call — `durability.HARDENED_GIT_FLAGS` (`core.hooksPath=/dev/null` + `core.fsmonitor=false`, pinned
  on the command line so an injected `.git/config` is overridden) threaded through `durability._git` /
  `remote.run_git` / `fleet._git` / the `_prepare_repo` clone, plus a **credential-helper reset** on
  authenticated ops so an injected helper can't capture the token. **C (CI/local key read):** the
  in-process key was protected only by the node's `kernel.yama.ptrace_scope`; now the key-holder is marked
  **non-dumpable** (`prctl(PR_SET_DUMPABLE,0)` in `secrets._harden`), so a same-uid child/sibling can't
  read its heap or `/proc/<pid>/environ` regardless of `ptrace_scope` (closes the CI/local same-uid read
  and backstops A's key-disclosure sub-case). **B (flywheel poisoning):** skill content derives from the
  goal (attacker-controlled) and reaches every future prompt — `skills._sanitize_skill` (in `_vet`, all
  tiers) refuses credential-shaped content, caps length, strips control chars; the default distiller
  quotes a truncated goal as provenance; the render header is reframed advisory; **per-tenant namespacing**
  documented as the multi-tenant blast-radius control. **252 → 275 tests** (+11
  `tests/test_security_hardening.py`, incl. a behavioral hook-bypass proof). Updated
  [`04-security.md`](architecture/04-security.md) (adjacency hardening + flywheel containment),
  [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md) (the closed adjacency), and
  [`part-iii-skills-repo.md`](part-iii-skills-repo.md) (content guards). **Tracked follow-ups (D–G):**
  Job/tool timeouts (`activeDeadlineSeconds` + subprocess timeouts), Redis AUTH, separate-pod netns for
  content-exfil, shallow-clone + render-cap for the skills repo.

- **2026-06-21 — Phase 5b built (the `loopkit-skills` git repo: the cross-run flywheel, durable across
  machines).** Gave Ch 17's write-back flywheel a network home for the cloud fleet, whose ephemeral pods
  share no filesystem. New **`GitSkillRegistry`** (`extensions/skills.py`) — **composition, not a fork**:
  it wraps a `FileSkillRegistry` over a cloned working tree, so the loop's read/write edges
  (`build_prompt` render / `run_loop` DONE write-back) are untouched. On construction it **clones/pulls**
  the repo (read edge — `render()` reads the local clone every tick, no per-tick network); `write_back`
  delegates to the file registry (gate→distil→dedupe→store) and, only on a mint, **commits + pushes** the
  new `.md`. New **`GitTransport`** seam + default **`_SubprocessGitTransport`**: clone-or-pull (tolerates
  a brand-new empty remote → bootstrap), gated commit + push with a `fetch`+`rebase origin/<branch>`+retry
  (concurrent pods land file-disjoint skills, so the rebase doesn't conflict), **never force-pushes**, and
  is **best-effort** (any failure WARNs + returns False — a skill that can't sync must never fail the run
  that earned it). Promoted a public **`remote.run_git(repo, *args, authenticated=…)`** (the single
  git-with-hygiene entrypoint; `_git`/`_git_auth` refactored onto it) so the transport reuses the scrubbed
  env + env-fed credential helper with no duplication. Wiring: **`make_repo_runner(skills_repo=…,
  skills_branch=…)`** builds a per-task `GitSkillRegistry` cloned into the task's own scratch
  (`scratch/skills-repo`, kept out of the target clone), gated by the held-out **acceptance gate**
  (gated-never-ungated; executor-aware so it runs in the Phase-6 sidecar) → `run_loop(skills=…)`;
  **`fleet worker --skills-repo/--skills-branch`** (+ envvars) and **`cloud run --skills-repo`** →
  `RunSpec.skills_repo/skills_branch` → `worker_command` (worker only — the coordinator does no
  write-back). **No new Secret** (loopkit-core's git token, Phase 6) and **no new infra** (github.com
  egress already allowlisted). **Decision: direct push to the skills repo's `main`** (it's loopkit's own
  state store, distinct from a target repo whose `main` the Ch 16 envelope protects; a PR-per-skill would
  block the flywheel's compounding) — the gate + git history are the guard. **252 → 264 tests**
  (`tests/test_skills_repo.py` ×12, all against a **real local bare repo** — the full flywheel through
  `run_loop` [A pushes → B clones + solves tick 1], gated/idempotent/bootstrap/concurrent-rebase, the
  injectable-transport units, and `worker_command`/`coordinator_command` wiring). New **`loopkit demo 23`**
  (two pods sharing only a git repo). Deferred-import invariant held (core+skills+fleet pull no
  kubernetes/redis/langsmith). Documented in [`part-iii-skills-repo.md`](part-iii-skills-repo.md);
  updated [`01`](architecture/01-system-today.md) (skills row → three storage tiers) and
  [`02`](architecture/02-cloud-architecture.md) (skills flywheel → Built 🟢). **Live drop-in** (a real
  `loopkit-skills` GitHub repo, second run reads the first's skill) awaits the remote + a cluster.

- **2026-06-21 — Phase 6 built (agent isolation: the keyless-executor sidecar split).** Closed 5a's one
  residual — a same-uid in-pod `ptrace`/heap read of the in-process key — for the cloud worker, by
  construction. New core **`executor.py`** (stdlib-only): a `ToolExecutor` seam (`dispatch` + `run_gate`),
  **`LocalToolExecutor`** (in-process default — exact prior behavior; `_WorkspaceTools` moved here,
  re-exported from `agent.py`), **`RemoteToolExecutor`** (length-prefixed-JSON over a Unix socket,
  one-request-per-connection, **degrade-on-unreachable** → tool/gate error not a crash), a `serve()`
  server, and a new top-level **`loopkit executor`** CLI command (the keyless sidecar; graceful SIGTERM).
  `_APIAdapter`, `ShellGate`, `run_loop`, `build_agent`, and `make_repo_runner` all take an injected
  executor (default `Local`); the cloud worker (`fleet worker --executor-socket`, env
  `LOOPKIT_EXECUTOR_SOCKET`) injects `RemoteToolExecutor` so the agent's tool calls + the held-out gate
  run in the sidecar. **`cloudrun._pod_spec` rewritten** to the two-container worker: loopkit-core (uid
  1000) gets creds via **`envFrom`** (dropping the init-container→tmpfs→shred + `CREDS_DIR` + the
  `creds`/`creds-src` volumes), the **executor native sidecar** (initContainer `restartPolicy: Always`,
  uid 1001, **no Secret/envFrom**) runs the untrusted surface; shared **workspace + socket emptyDirs**
  (`fsGroup` 1000, `umask 002`, socket `0660`), `GIT_CONFIG safe.directory=*` on the executor; the
  coordinator stays single-container. The worker fail-closed key check re-keys off a new
  `LOOPKIT_CREDS_EXPECTED` marker (envFrom replaced `LOOPKIT_CREDS_DIR`). **Refinement from the design
  doc:** `secrets.py`'s load-shred + `child_env` scrub were **kept** (not deleted globally) — they are the
  containment for the two tiers with **no sidecar** (the CI tier + untrusted local); the sidecar is the
  cloud-worker kernel boundary and the shred is harmless/redundant inside the split. **241 → 252 tests**
  (`tests/test_executor.py` ×9 socket round-trip — incl. the API adapter + `ShellGate` driving a real
  socket peer, and degrade-on-unreachable — + the rewritten `test_cloudrun.py` pod-spec assertions: two
  containers, the key `envFrom`'d into loopkit-core only, a different-uid native sidecar, no Secret-backed
  volume anywhere). Deferred-import invariant held (core imports pull no kubernetes/redis/langsmith).
  Updated [`part-iii-agent-isolation.md`](part-iii-agent-isolation.md) (Designed→Built + resolved
  decisions), [`04`](architecture/04-security.md) (residual → closed for the cloud worker; new
  *Agent isolation* layer row), [`02`](architecture/02-cloud-architecture.md) (two-container topology),
  [`03`](architecture/03-adapters-and-auth.md) (envFrom delivery), and [`architecture/README.md`](architecture/README.md)
  (master diagram + tier table). **Live DOKS proof** (ptrace from the executor fails; run still completes)
  awaits a cluster.

- **2026-06-21 — Part III teaching form restored (curriculum labs + ecosystem module).** Brought Part
  III up to the Parts I–II standard: the *runnable-scenario* teaching form Part III had dropped. Two new
  `demo`/`learn` chapters — **ch20 "Triggers as infrastructure"** (scripted: drives the pure
  `WebhookApp.dispatch` through six deliveries — forged→401/no-run, signed→one run, retry + second event
  →dedup, unlabelled→ignored — so the run count never exceeds one; the Ch 12 trigger seam productionized,
  GitHub HMAC vs GitLab token) and **ch21 "The CI deployment tier"** (live-capable: a canned GitHub issue
  event → `parse_event_payload` → goal → the real `run_loop` over the demo-repo → DONE → the simulated
  `--open-pr` outward edge with `Closes #N`; narrates the three tiers + platform-primitives-first). Both
  token-free by default (`MockAgent` + the demo-repo's real pytest gates); ch21 swaps in claude-code under
  `--live`, ch20 is scripted-only (it teaches plumbing, like ch12). Registered in
  `scenarios/__init__.py`, covered by `test_scenarios.py` (the play-all smoke test + an explicit
  registry/`live_supported` assertion). New **[`docs/part-iii-ecosystem.md`](part-iii-ecosystem.md)** —
  the GitHub/GitLab teaching module: the three deployment tiers, *use-the-platform's-primitives-first*
  (trigger/secrets/identity/sandbox per tier), the CI tier in depth (a render-verified mermaid flow + a
  GitHub-vs-GitLab table), triggers-as-infrastructure, the two labs, and the `loopkit init --ci`
  real-project on-ramp (fulfils the backlogged ecosystem-module note). Cross-linked from
  `USING-ON-YOUR-REPO.md`, the architecture wiki, and the root README. **241 tests green** (was 240;
  ch20/21 ride the existing play-all smoke test, +1 explicit registry/`live_supported` assertion).

- **2026-06-21 — Phase 5c built (CI deployment tier: run the single loop from forge CI, no cluster).**
  Added the middle of the three deployment tiers. `loopkit run` gained **`--from-event <path>`** (read a
  forge issue-event JSON — Actions `$GITHUB_EVENT_PATH` / GitLab CI — via a new
  **`triggers.parse_event_payload`** that auto-detects GitHub vs GitLab by body shape, since there are no
  HTTP headers/signature on disk), **`--from-issue <n>` + `--provider`** (fetch one issue via a new
  **`issues.fetch_issue`** — `gh/glab issue view`, the single-object sibling of `fetch_issues`, factored
  over a shared `_run_forge_json`), **`--open-pr`** (per-invocation flip of `[remote]` on → push + draft
  PR, threading the captured issue number into `remote.sync_done(issue=N)` for `Closes #N`), and
  **`--adapter`** (override; the templates pass `claude-api`, no binary in CI). **`loopkit init --ci
  github|gitlab`** scaffolds the workflow alongside the starter `loopkit.toml`/`PROMPT.md`; both
  templates ship in **`examples/ci/`** (+ a README) with a drift-guard test keeping them byte-identical
  to the CLI constants. **GitLab credential fix:** `secrets.GIT_ENV` += `GITLAB_TOKEN` and
  `remote.CRED_HELPER` now falls back GitHub→GitLab, so `glab` (issue fetch + MR) and the HTTPS git push
  authenticate through the Phase-5a hygiene — re-injected only into loopkit's *own* forge subprocess; the
  agent's scrubbed `child_env()` still gets no token (asserted by a new `test_secrets` case). Additive:
  no cloud control-plane code touched; the deferred-import invariant holds (the CI path reuses `triggers`
  without pulling `[cloud]`/`[fleet]`). **219 → 240 tests** (`tests/test_ci.py` ×12 + parser/fetch/secrets
  units). Updated [`part-iii-ci-mode.md`](part-iii-ci-mode.md) (Designed→Built) and
  [`architecture/README.md`](architecture/README.md) (CI tier → 🟢 Built). **Sharp edge documented:** CI
  cost/identity is **per-repo, not per-submitter** (CI secrets are repo-scoped) — per-engineer attribution
  stays a cloud-tier feature; the doc says so rather than faking it.

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
