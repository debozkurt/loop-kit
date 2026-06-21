# 03 — Adapters, auth & credentials (Adapters + cost **Built 🟢**, per-submitter auth **Built 🟢 Phase 5a**)

How loopkit drives a coding agent (provider-agnostic), and how credentials reach it — both the
static per-environment model and the dynamic **per-submitter** model.

> **Built 🟢:** the full **2×2 adapter matrix** + **real per-adapter cost** (`pricing.py`, Phase 0),
> and the **per-submitter credential machinery** (Phase 5a): the identity→Secret resolver
> ([`creds.py`](../../loopkit/extensions/creds.py)), key-projection, the fail-closed fallback, and the
> worker-side hygiene that keeps the resolved key **out of the agent's reach**
> ([`secrets.py`](../../loopkit/secrets.py)). The deep treatment of *how the key is withheld from a
> prompt-injected agent* lives in [`04-security.md`](04-security.md) → *Credential handling along the
> injection flow*; this page is the resolution model.

## The `Agent` protocol & the 2×2 adapter matrix

`run_loop` depends only on the `Agent` contract — "make edits toward the goal this tick" — so the
provider is a swappable strategy. Part III fills out a full 2×2: **CLI vs API × Claude vs OpenAI.**

| | **CLI adapter** (shell out to a binary that loops internally) | **API adapter** (in-process loop via the SDK) |
|---|---|---|
| **Claude** | `claude-code` — `claude -p --output-format json` 🟢 | `claude-api` — Anthropic SDK + tool-calling 🟢 |
| **OpenAI** | `codex` — `codex` CLI headless 🟢 | `openai-api` — OpenAI SDK + function-calling 🟢 |

The two axes are deliberate, and each earns its place:

- **CLI vs API is not redundant.** A **CLI adapter** shells out to an agent binary that runs its
  *own* internal agentic loop for one loopkit tick; it's fast to ship and matches how people run
  these tools today, but loopkit only sees stdout. An **API adapter** implements the per-tick
  edit/bash loop in-process via the SDK (loopkit-defined `edit_file`/`run_bash` tools), which is more
  code but yields **native `usage`/cost** — the thing that makes the budget ceiling actually bite
  (no scraping CLI text). Build both; pick per run with `--adapter`.
- **Claude vs OpenAI** is provider choice; the matrix keeps them symmetric behind one interface.

`MockAgent` remains the fifth, token-free adapter that drives every test and the green dev fleet.

**Model selection is config, not architecture.** The adapter chooses the provider; the model
(`claude-opus-4-8` by default for Claude; the OpenAI model for the OpenAI adapters) is a config knob.
Keep that in `AgentConfig`, not hardcoded.

### Per-tick fit

`run_loop` owns the outer loop (prompt → guard → commit → gates → stops); the adapter owns "produce
this tick's edits." A CLI adapter satisfies a tick with one headless invocation against the cloned
repo; an API adapter satisfies it by running its function-calling loop until it yields control back.
Either way, **loopkit's gates, commit-every-tick, safety guard, and hard stops are unchanged** — the
adapter is the only thing that varies.

## The pluggable credential model

A credential is resolved by **`(environment, submitter)`** — one source Secret per engineer-per-env,
holding all their keys — and the **adapter selects which key** is *projected* into the run's namespace
(only that key + git creds, never the whole bag). The literal three-part `(env, adapter, submitter)` is
the doc's earlier framing; in code the *Secret* is keyed by `(env, submitter)` and the *adapter* is a
within-Secret selector — the resolution is `creds.secret_name(env, submitter)` + `creds.project(data,
adapter)`:

```
(environment, submitter)  ──resolve──▶  source Secret  ──project──▶  ns/run-<id>/loopkit-creds
  · adapter selects the var               (loopkit-system)  · adapter key   (delivered to a memory tmpfs,
  · registered set = allowlist                              · + git creds    loaded + SHREDDED, GC'd with ns)
```

- **Adapter → which key:**
  - Claude (`claude-code`/`claude-api`): **either** `ANTHROPIC_API_KEY` **or**
    `CLAUDE_CODE_OAUTH_TOKEN` (subscription). Both are supported with no adapter change — the Claude
    Code CLI honors either (API key wins if both are set, so the Secret carries exactly one). The
    `claude-api` adapter uses the API key path.
  - OpenAI (`codex`/`openai-api`): `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` for Azure/compatible).
  - Plus **git creds** for clone/push/PR (PAT/deploy-key now; GitHub App ⚪ later).
- **Environment → segmentation:** `prod` and `dev` map to different namespaces, different Secrets,
  and different provider workspaces/accounts — so they get **independent spend limits**.

This is the static model: a team/fleet default key per `(env, adapter)`. The dynamic model below
layers on top by adding the third key part.

## Per-submitter keys — "swap keys by who submits"

Engineers can run jobs under **their own** API key, resolved dynamically from who submitted the run.

- **Submitter identity** comes from the entry point: the authenticated CLI user (kubeconfig/OIDC
  identity, or explicit `--as`), the **GitHub actor** on a webhook (issue/PR author in the payload),
  or the configured owner of a CronJob.
- **Resolution (🟢 Phase 5a — pre-provisioned per-engineer Secrets, hardened):** each engineer
  registers once (`loopkit cloud creds set --as <eng> --adapter …`, env/stdin only → a Secret in
  `ns/loopkit-system`; re-run per adapter to accumulate). At run creation the submitter's identity
  (CLI `--as`, the webhook **issue author**, or the CronJob's pinned `--as`) selects their Secret;
  `resolve_for_run` **projects** only the adapter key + git and records the exact canonical identity
  (an injective check guards a sanitize collision). Fallback to the shared `fleet` key is
  **fail-closed**: never on the untrusted webhook/cron path unless `--allow-fleet-fallback`, and a
  confirm prompt on the interactive CLI — an unregistered submitter is refused, not silently shared.
  `cloud creds ls` shows key *names* only, never values. Covers all three entry points uniformly.

**Why this is safe enough to start with, and how it hardens:**

| Hardening step | Effect |
|---|---|
| DOKS **etcd encryption-at-rest** (or sealed-secrets/SOPS) | the Secret isn't plaintext in etcd |
| **RBAC** | an engineer can set/read only *their own* creds; only `loopkit-control` copies into a run ns |
| **Per-run scoped, short-lived copy** | the key lands only in `ns/run-<id>`, mounted only into that run's pods, deleted with the namespace |

**The migration to Vault is a resolver swap, not a redesign.** The run-creation side is identical
regardless of source — it always produces a per-run, namespace-scoped, TTL-GC'd Secret. Only the
*resolver* (identity → source key) changes. So v1 → ESO/Vault (⚪ planned, the most-secure option)
is localized to the resolver; nothing downstream moves.

**Per-user keys are also a security control, not just convenience.** Because `--from-issues` feeds
**untrusted issue bodies** to an agent holding a real key, the key's blast radius matters: with each
engineer on their own key + their own spend limit, a poisoned issue can at worst burn **that
submitter's own budget** — never a shared fleet key. Per-user keys *are* the prompt-injection
blast-radius containment. See [`04-security.md`](04-security.md).

## Billing & cost control

**The subscription path is for dev, not the fleet — and the reason is recent.** As of **2026-06-15**
Anthropic ended the "subscription subsidy for agents": headless/programmatic Claude Code (`claude -p`,
the path a worker pod runs) now draws a **separate monthly agent-credit pool billed at API list
rates** (~$20 Pro / $100 Max 5x / $200 Max 20x), and parallel workers exhaust the 5-hour/weekly caps
fast. So the subscription gives **no cost advantage** at fleet scale and runs out quickly.

| Context | Credential | Why |
|---|---|---|
| **Production fleet** | dedicated **API key** (own Console workspace + spend limit) | the supported automation path; real cost controls; scales with your API tier |
| **Local / dev single runs** | subscription **OAuth token** | uses the Max plan you already have, fine at low volume |

Both remain *configurable* per environment (the pluggable model above) — this is the recommended
default, not a lock-in.

**Two independent budget backstops:**

1. **loopkit's budget stop** (`stops.py`) — per-run, fed by the adapter's cost. This is where the
   **API adapters pay off**: native `usage` → accurate `cost_usd` → the budget stop bites mid-run.
   (CLI adapters can parse `claude -p --output-format json`, which emits cost/usage; the API adapters
   make it exact.) Real per-adapter cost parsing is now **built** — `pricing.py` holds the per-model
   price table (input/output/cache tiers), the API adapters sum native `usage` into an exact
   `cost_usd`, `claude-code` parses `total_cost_usd`, and `codex` derives cost from token usage; each
   span in a LangSmith trace carries the same number (see [`01`](01-system-today.md#observability--two-layers-logs--traces)).
   `loopkit doctor` warns when a model is unpriced (cost 0.0 → the budget stop can't fire).
2. **The provider's Console spend limit** — per workspace/account, independent of loopkit. With
   per-submitter keys this doubles as a **per-engineer budget ceiling** for free.
