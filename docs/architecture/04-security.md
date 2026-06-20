# 04 — Security model: the Ch 16 envelope at cloud scale (Designed 🟡)

The single-loop safety envelope ([`01`](01-system-today.md#safety-envelope-ch-16--safetypy)) is
**extended, not replaced**, on a shared multi-tenant cluster. This page is the threat model and the
defense-in-depth that answers it.

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
| **Network** | `NetworkPolicy` **default-deny** + egress allowlist (GitHub, `api.anthropic.com`/OpenAI, GHCR) — and *nothing else*; **workers get no cluster-API access** | exfiltration, lateral movement |
| **Identity** | least-privilege ServiceAccounts: **only `loopkit-control` may create namespaces/Jobs/Secrets**; workers run a no-API-access SA | privilege escalation |
| **Secrets** | per-run, namespace-scoped, mounted into pods only, **GC'd with the namespace**; etcd encryption-at-rest (or sealed-secrets/SOPS; Vault ⚪) | secret theft, persistence |
| **Code egress** | **branch-only pushes** (never `main`), **draft** PRs, refuses forbidden branches, never force | malicious merges |
| **Filesystem** | protected-path guard (agent can't touch `tests/`) + the pre-tool-use hook baked into the image | gaming the gate, destructive edits |
| **Cost** | per-run loopkit budget stop + provider Console spend limit (two independent backstops) | runaway / abusive spend |
| **Control plane** | context-safety guard on the CLI/kubectl (pin DOKS context, confirm mutations) | wrong-cluster accidents |

These compose: defeating one layer (say, a prompt injection that hijacks the agent) still leaves the
attacker boxed by the network policy, the least-privilege SA, the branch-only push rule, the
namespace quota, and the budget ceiling.

## Prompt-injection containment

The honest position: **you cannot reliably prevent an LLM from following injected instructions, so
you contain what a hijacked agent can do.** For loopkit that means:

- **Least privilege is the defense** — a hijacked agent still can't reach the cluster API, can't
  egress anywhere off the allowlist, can't push to `main`, can't open a non-draft PR, and can't touch
  protected paths.
- **Per-submitter keys cap the cost blast radius** — a poisoned issue burns the *submitter's own*
  budget/key, never a shared fleet key (see [`03`](03-adapters-and-auth.md#per-submitter-keys--swap-keys-by-who-submits)).
- **Draft PRs keep a human in the loop** — nothing an injected run produces merges itself; a reviewer
  sees the diff before it lands.
- **The held-out gate resists output-gaming** — even a manipulated agent can't declare `DONE` without
  passing a gate it never saw ([`01`](01-system-today.md#the-held-out-acceptance-gate--the-anti-overfit-core-ch-9)).

## Budget ceilings

Two backstops, independent on purpose (one can fail without removing the other):

1. **loopkit budget stop** — per-run, in-process, fed by the adapter's `cost_usd`. The **API
   adapters** make this exact (native `usage`); making the budget stop bite on live runs is
   load-bearing in production, not optional.
2. **Provider Console spend limit** — per workspace/account, outside loopkit. With per-submitter keys,
   this is also a per-engineer ceiling.

## Control-plane / kubectl safety

A managed cloud context (DOKS) is **production-sensitive** under the global kubectl-safety rule. The
`loopkit cloud` CLI and any kubectl/helm use **must**:

- **Pin the expected cluster context** and **refuse/confirm before mutating any other** — the dev
  `Tiltfile`'s `allow_k8s_contexts(...)` + `fail()` guarantee, extended to the real cloud context.
- Prefer explicit `--context=<expected>` over ambient current-context for mutating operations.
- Treat the repo-local `KUBECONFIG` isolation pattern from the `Makefile` as the model — credentials
  for the loopkit cluster never merge into the user's personal `~/.kube/config`.

This is the same discipline that kept the dev fleet from ever touching the host's other clusters; it
matters more, not less, against a cloud control plane.

## Webhook security

The webhook listener is the one inbound surface, so it's hardened specifically:

- **HMAC signature verification** on every GitHub delivery — reject anything unsigned or mis-signed,
  so a run can't be forged by an unauthenticated POST.
- **Idempotency/dedupe** on the delivery/issue id — GitHub re-delivers; a re-delivery must not start a
  second run for one issue.
- The listener creates runs through the same `create_run()` + `loopkit-control` SA — it has no extra
  privilege beyond submitting runs.

## Secrets at rest

- v1: **etcd encryption-at-rest** on DOKS, or **sealed-secrets/SOPS** so Secrets aren't plaintext in
  git/etcd.
- ⚪ later: **External Secrets Operator / Vault** for central rotation + audit + no standing k8s
  secret — the most-secure tier, a localized resolver swap (see [`03`](03-adapters-and-auth.md)).

## Open hardening (⚪ Planned)

GitHub **App** auth (scoped, revocable, higher rate limits) replacing PATs; tighter per-run quotas;
a cluster-wide **admission/concurrency cap** on runs; signed commits; egress via an explicit proxy
with per-run allowlists. Tracked in [`../part-iii-resume.md`](../part-iii-resume.md).
