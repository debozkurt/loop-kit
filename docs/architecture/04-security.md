# 04 — Security model: the Ch 16 envelope at cloud scale (controls Built 🟢 Phases 2–5a)

The single-loop safety envelope ([`01`](01-system-today.md#safety-envelope-ch-16--safetypy)) is
**extended, not replaced**, on a shared multi-tenant cluster. This page is the threat model and the
defense-in-depth that answers it.

> **Built 🟢 (Phase 2):** the **control-plane context guard**
> ([`loopkit/extensions/cloud.py`](../../loopkit/extensions/cloud.py)), the **default-deny
> NetworkPolicy** + egress allowlist ([`k8s/cloud/30-networkpolicy.yaml`](../../k8s/cloud/30-networkpolicy.yaml)),
> and the **least-privilege RBAC** ([`k8s/cloud/20-rbac.yaml`](../../k8s/cloud/20-rbac.yaml) —
> `loopkit-control` is the only SA that may create namespaces/Jobs/Secrets; workers get a no-API SA).
> **Built 🟢 (Phase 3):** the **per-run** controls in
> [`loopkit/extensions/cloudrun.py`](../../loopkit/extensions/cloudrun.py) — each run namespace gets
> its own **default-deny NetworkPolicy** (egress only to DNS, Redis, and 443, metadata blocked), a
> **no-API worker SA** (token automount off), a per-run **Secret** (`envFrom`, GC'd with the
> namespace), and a **ResourceQuota/LimitRange**.
> **Built 🟢 (Phase 4):** the **webhook hardening** in
> [`loopkit/extensions/triggers.py`](../../loopkit/extensions/triggers.py) — **HMAC-SHA256 signature
> verification** (fail-closed) and **idempotency** (one run per issue) before any run starts, plus the
> **in-cluster context guard** (the trigger pods authenticate with their SA and the guard pins a
> synthetic, un-spoofable `in-cluster` context).
> **Built 🟢 (Phase 5a):** **per-submitter credentials, withheld from the agent** — the
> identity→Secret resolver ([`creds.py`](../../loopkit/extensions/creds.py)) + the worker-side
> credential hygiene ([`secrets.py`](../../loopkit/secrets.py)): the key is **loaded off a
> memory-tmpfs and shredded** out of the filesystem + `os.environ` before agent code runs, every
> untrusted-driven subprocess gets a **scrubbed env**, the webhook **binds the run to the issue
> author** (fail-closed allowlist), **CLI adapters are refused** on untrusted runs, and a **pre-push
> secret scan** + **Cilium FQDN egress** backstop the exfil paths. See *Credential handling along the
> injection flow* below.

## Threat model

What's actually dangerous about this system, stated plainly:

> An **autonomous agent** runs with a **real credential** (an API key that costs money, git creds
> that can push), operating on **untrusted input** (issue bodies via `--from-issues`), on a **shared
> cluster** running **many tenants' runs** at once.

Three concrete risks follow:

1. **Prompt injection.** An attacker files an issue whose body contains instructions ("ignore the
   task, exfiltrate secrets / push to main / open a PR to a fork you control"). `--from-issues` feeds
   that body into the agent's prompt.
2. **Credential abuse / cost.** A compromised or runaway run burning an API key, or reaching a
   credential it shouldn't.
3. **Cross-tenant blast radius.** One run affecting another's compute, secrets, or network.

The design assumption is **the agent may be adversarial or hijacked** — so containment, not trust,
is the control. Least privilege is the throughline: the agent can only ever do what its namespace,
ServiceAccount, NetworkPolicy, Secret, and budget allow.

## Defense in depth

| Layer | Control | Answers |
|---|---|---|
| **Tenant isolation** | namespace per run; `ResourceQuota`/`LimitRange`; per-run Redis keyspace | cross-tenant blast radius |
| **Network** 🟢 | `NetworkPolicy` **default-deny** + egress allowlist (GitHub, `api.anthropic.com`/OpenAI, GHCR) — and *nothing else*; **workers get no cluster-API access** | exfiltration, lateral movement |
| **Identity** 🟢 | least-privilege ServiceAccounts: **only `loopkit-control` may create namespaces/Jobs/Secrets**; workers run a no-API-access SA; the control SA gets secrets `create,get,delete` — **no `list`/`update`/`patch`** (a listener RCE can't enumerate or rewrite a tenant's key) | privilege escalation |
| **Secrets** 🟢 | per-submitter source Secret → **only the adapter key + git projected** into a per-run Secret, **delivered to memory and shredded** off the FS/`os.environ` before agent code runs (never `envFrom`, never an agent-readable mount), **GC'd with the namespace**; at-rest depends on the cluster's etcd posture (sealed-secrets/SOPS; Vault ⚪) | secret theft, exfiltration, persistence |
| **Code egress** | **branch-only pushes** (never `main`), **draft** PRs, refuses forbidden branches, never force | malicious merges |
| **Filesystem** | protected-path guard (agent can't touch `tests/`) + the pre-tool-use hook baked into the image | gaming the gate, destructive edits |
| **Cost** | per-run loopkit budget stop + provider Console spend limit (two independent backstops) | runaway / abusive spend |
| **Control plane** 🟢 | context-safety guard on the CLI/kubectl (pin DOKS context, fail-closed, confirm mutations) | wrong-cluster accidents |

These compose: defeating one layer (say, a prompt injection that hijacks the agent) still leaves the
attacker boxed by the network policy, the least-privilege SA, the branch-only push rule, the
namespace quota, and the budget ceiling.

## Prompt-injection containment

The honest position: **you cannot reliably prevent an LLM from following injected instructions, so
you contain what a hijacked agent can do.** For loopkit that means:

- **Least privilege is the defense** — a hijacked agent still can't reach the cluster API, can't
  egress anywhere off the allowlist, can't push to `main`, can't open a non-draft PR, and can't touch
  protected paths.
- **The agent never holds the key** (API-adapter runs, the trigger default) — loopkit makes the model
  call in-process and scrubs the key from every subprocess the agent drives, so there's nothing to
  exfiltrate; and **per-submitter keys cap the cost blast radius** — a poisoned issue burns the
  *submitter's own* budget, never a shared fleet key (see *Credential handling along the injection
  flow* above and [`03`](03-adapters-and-auth.md#per-submitter-keys--swap-keys-by-who-submits)).
- **Draft PRs keep a human in the loop** — nothing an injected run produces merges itself; a reviewer
  sees the diff before it lands.
- **The held-out gate resists output-gaming** — even a manipulated agent can't declare `DONE` without
  passing a gate it never saw ([`01`](01-system-today.md#the-held-out-acceptance-gate--the-anti-overfit-core-ch-9)).

## Credential handling along the injection flow (Built 🟢 Phase 5a)

The sharpest risk in the whole system: an **autonomous agent** runs on **untrusted issue text** in the
**same container, as the same uid**, as the loopkit process that holds a **real key**. Phase 5a's
position is blunt: **don't redact a leak, prevent the agent from ever holding the key.** Redaction is a
backstop (base64 defeats substring matching); the boundary is *withholding*.

The credential's life, and where each hop is closed:

```mermaid
%%{init: {'theme':'base','themeVariables':{'background':'#1a1a1a','primaryColor':'#2d2d2d','primaryTextColor':'#e0e0e0','primaryBorderColor':'#555','lineColor':'#888','fontSize':'13px'}}}%%
flowchart LR
    A["cloud creds set --as eng<br/>(env/stdin, never argv)"] --> B["per-(env,submitter) Secret<br/>in loopkit-system"]
    B --> C["resolve_for_run<br/>project adapter key + git<br/>issue-author identity · fail-closed"]
    C --> D["per-run Secret<br/>worker: api+git · coord: git only"]
    D --> E["initContainer → tmpfs<br/>emptyDir medium:Memory<br/>(NOT envFrom / agent mount)"]
    E --> F["worker load → memory<br/>os.remove files · scrub os.environ<br/>RLIMIT_CORE=0"]
    F --> G["API SDK: explicit key in-proc<br/>git: env-fed helper, no URL token"]
    F --> H["run_bash · gate · vendor CLI<br/>scrub_env() → NO key"]
    G --> I["pre-push secret scan +<br/>Cilium FQDN egress"]
```

**The leak/abuse vectors, and the control that closes each** (every one verified against code):

| Vector | Control |
|---|---|
| `run_bash`/gate inherit `os.environ` → `printenv` → trace/repo/push | `secrets.child_env()` scrubs every untrusted-driven subprocess |
| File-mount co-located with the agent uid → `cat /var/run/loopkit/creds/*` | init-container→memory-tmpfs→**`os.remove` + scrub** before agent code runs |
| Same-container `/proc/<loopkit>/environ` | the key is deleted from `os.environ` at load; held only in heap |
| Whose key = **attacker-controlled** webhook JSON (`sender.login`) | bind to the **issue author**; **fail-closed allowlist** (registered set); GitLab uses a pinned identity |
| Token-in-URL persists in `.git/config` | `sanitize_git_url` strips userinfo; auth via an env-fed credential helper (never argv) |
| Held-out gate runs agent-authored `conftest.py` with creds | the gate spawns with the scrubbed env too |
| Traced/echoed key (LangSmith = third party) | redaction registry on `trace._cap`, tool output at capture, exception detail |
| 443 egress to any host | per-run **Cilium FQDN** allowlist (DOKS); CLI adapters refused so the agent never holds the key anyway |
| A credential written into the repo before a human sees the draft PR | **pre-push secret scan** (registry + token-shape) hard-gates `push_branch` |
| A run orphaning a real Secret at rest on partial failure | `create_run` deletes the namespace on any apply failure |

**The honest residual.** In a *single shared container*, loopkit must hold the live key in-process to
make the LLM/git calls, and the agent's `run_bash` runs as the same uid — so a same-uid
`ptrace`/`process_vm_readv` of loopkit's heap is **not** closed by env/file scrubbing or
`RuntimeDefault` seccomp (which doesn't block ptrace). It is closed only by running the agent shell in
a **separate PID-namespace container** — a topology change deferred past 5a (the in-process API tool
loop co-locates `run_bash` with loopkit). We state this plainly: 5a closes every
env/file/argv/URL/log/trace/gate/repo/network path; it does **not** claim "the agent never sees it."
Other residuals: 443-exfil of content to an *allowed* host (irreducible without per-destination
identity); the CLI-adapter vendor loop (so it's **refused** on untrusted runs); redact-by-value is a
backstop, not a boundary; the `--from-issues` **sweep** spends one identity for *all* swept issues
(per-author attribution is the **webhook** path only).

## Budget ceilings

Two backstops, independent on purpose (one can fail without removing the other):

1. **loopkit budget stop** — per-run, in-process, fed by the adapter's `cost_usd`. The **API
   adapters** make this exact (native `usage`); making the budget stop bite on live runs is
   load-bearing in production, not optional.
2. **Provider Console spend limit** — per workspace/account, outside loopkit. With per-submitter keys,
   this is also a per-engineer ceiling.

## Control-plane / kubectl safety

A managed cloud context (DOKS) is **production-sensitive** under the global kubectl-safety rule. The
`loopkit cloud` CLI and any kubectl/helm use **must** (all three **Built 🟢 Phase 2** —
[`loopkit/extensions/cloud.py`](../../loopkit/extensions/cloud.py) +
[`Makefile`](../../Makefile) `cloud-*` targets):

- **Pin the expected cluster context** and **refuse/confirm before mutating any other** — the dev
  `Tiltfile`'s `allow_k8s_contexts(...)` + `fail()` guarantee, extended to the real cloud context.
  `check_context()` runs before every mutation (`bootstrap`, and every Phase-3 command on top) and is
  **fail-closed**: no pin (neither `--context` nor `$LOOPKIT_CLOUD_CONTEXT`) → refuse, rather than
  trusting the ambient context.
- Prefer explicit `--context=<expected>` over ambient current-context for mutating operations — the
  pin is *declared*, never inferred from whatever current-context happens to be.
- Treat the repo-local `KUBECONFIG` isolation pattern from the `Makefile` as the model — the
  `cloud-*` targets read `.kube/loopkit-cloud.yaml`, so cluster credentials never merge into the
  user's personal `~/.kube/config`.

This is the same discipline that kept the dev fleet from ever touching the host's other clusters; it
matters more, not less, against a cloud control plane.

## Webhook security (Built 🟢 Phase 4)

The webhook listener is the one inbound surface, so it's hardened specifically — and the
security-critical logic is a pure, exhaustively unit-tested `WebhookApp.dispatch`
([`extensions/triggers.py`](../../loopkit/extensions/triggers.py)):

- **Authentication on every delivery**, per forge, both constant-time and **fail-closed** (no
  configured secret ⇒ refuse; the `cloud webhook` command won't even start without
  `--secret`/`$LOOPKIT_WEBHOOK_SECRET`):
  - **GitHub** — **HMAC-SHA256 of the body** (`X-Hub-Signature-256`, `hmac.compare_digest`); reject
    anything unsigned or mis-signed (401). The signature is bound to the body, so a forged payload
    fails even with the right header.
  - **GitLab** — a **static secret token** (`X-Gitlab-Token`) compared constant-time. *Honest caveat:*
    GitLab does not sign the body, so the token is not bound to the request — a leaked token is
    directly replay/forge-able. That's GitLab-native behavior; treat the token as a high-value secret
    (rotate it, keep TLS on). One listener serves one forge (`--provider`).
- **Identity binding (🟢 Phase 5a):** *whose* key a run spends is the **issue author**
  (`issue.user.login`), **not** `sender.login` — so a maintainer merely *labelling* an attacker's
  issue can't bind their key to attacker content (a confused deputy). The registered per-submitter
  Secret set **is** the allowlist: an unregistered author → **403, no run** (default-deny), and the
  authorization check runs **before** the idempotency reservation, so a refused or failed delivery
  never marks the issue seen-and-dead (it can run once the submitter registers). GitLab's token isn't
  bound to the body, so its `user.username` is forgeable — the GitLab listener uses a **single pinned
  identity** (`--as`/`LOOPKIT_SUBMITTER`), never the payload. Untrusted runs are **`claude-api`** only
  (a CLI adapter holds the key in its own loop) — see *Credential handling* above.
- **Idempotency/dedupe** keyed on the **issue identity** (`repo#issue`), not the delivery id — GitHub
  re-delivers and a single issue can emit multiple matching events (opened, then labeled); both
  dedupe to **exactly one run**. In-memory (first-writer-wins) for a single replica; Redis
  `SET … NX EX` (atomic, with a TTL) when the Deployment scales past one replica. `release(key)` frees
  the reservation if `create_run` fails, so a transient error doesn't dead-lock the issue.
- An optional **trigger label** gates which issues dispatch a run, so an open backlog isn't
  auto-actioned wholesale — only issues bearing the label (e.g. `loopkit`).
- The listener creates runs through the same `create_run()` + `loopkit-control` SA — it has no extra
  privilege beyond submitting runs, and the in-cluster context guard still runs first.

## Secrets at rest

- **Honest baseline:** Secret values are **base64, not encrypted**, in the k8s API. At-rest protection
  depends on the cluster's etcd posture, which the operator must verify — DOKS provides disk-level
  volume encryption, not a customer-configurable Secret-KMS. For git-stored secrets use
  **sealed-secrets/SOPS**.
- ⚪ later: **External Secrets Operator / Vault** for central rotation + audit + no standing k8s
  secret — the most-secure tier, a localized resolver swap (see [`03`](03-adapters-and-auth.md)).

## Open hardening (⚪ Planned)

The one that matters most: a **separate-PID-namespace agent container** so the agent's `run_bash`
can't `ptrace` loopkit's in-process key — the residual that single-container 5a cannot close. Then:
GitHub **App** auth (scoped, revocable) replacing PATs; a validating **admission webhook** pinning
`--as` to the authenticated identity (so `--as` is an authorization, not a trust assertion); a scoped
**per-engineer self-service RBAC** Role for `cloud creds set/rm`; tighter per-run quotas; a
cluster-wide **admission/concurrency cap** on runs; signed commits. (Per-run **Cilium FQDN egress** and
the **pre-push secret scan** landed in Phase 5a.) Tracked in [`../part-iii-resume.md`](../part-iii-resume.md).
