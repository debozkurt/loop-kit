# Billing & credentials — read this before your first real run

The one page that saves a surprise invoice. (Design rationale lives in
[`architecture/03-adapters-and-auth.md`](architecture/03-adapters-and-auth.md); this is the operator
quickstart.)

## TL;DR

- **`claude-code` runs on your Claude Code *subscription* by default.** An ambient `ANTHROPIC_API_KEY`
  in your shell is **withheld** from the agent, so it can't silently bill the metered API.
- **Opt into the billed API key** with `loopkit run --api-key` (or `[agent] use_api_key = true`).
- **`loopkit doctor` shows which one will pay** — check it before you spend.
- **On a subscription the dollar budget can read `$0`**, so bound a run with **`--max-iter`**, not just
  `max_cost_usd`.

## Which credential pays

| Adapter | Authenticates with | Billing |
|---|---|---|
| `claude-code` (default) | the on-disk `claude` login or `CLAUDE_CODE_OAUTH_TOKEN` | your **subscription** |
| `claude-code --api-key` | `ANTHROPIC_API_KEY` | the metered **API** |
| `claude-api` | `ANTHROPIC_API_KEY` (the SDK) | the metered **API** (always) |
| `codex` / `openai-api` | `OPENAI_API_KEY` | the metered API |
| `mock` | none | free (simulated cost) |

`doctor`'s `agent` row names the active path, e.g.:
```text
agent  ok  /opt/homebrew/bin/claude · auth subscription (claude login / CLAUDE_CODE_OAUTH_TOKEN)
           · ANTHROPIC_API_KEY present but withheld (--api-key to bill it)
```
The run banner's first line corroborates it: `creds.loaded … keys=0 names=-` means no API key is in
play.

## Local: run on the subscription

Just run it — `claude-code` defaults to the subscription:
```bash
loopkit run                 # subscription; an ambient ANTHROPIC_API_KEY is withheld
loopkit run --api-key       # opt in: bill ANTHROPIC_API_KEY instead
```
(Prerequisite: you're logged in — `claude` works on its own. On older loopkit builds you had to strip
the key manually with `env -u ANTHROPIC_API_KEY loopkit run`; that's now the default.)

## CI: subscription via an OAuth token

Use the [`examples/ci/github-actions-claude-code.yml`](../examples/ci/github-actions-claude-code.yml)
template:
```bash
claude setup-token                                            # prints a long-lived OAuth token
gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo <owner>/<repo>   # and do NOT set ANTHROPIC_API_KEY
```
`claude-code` picks up `CLAUDE_CODE_OAUTH_TOKEN` and runs on the subscription. (The `claude-api`
template bills `ANTHROPIC_API_KEY` instead — pick per your cost model.)

## Cloud / fleet: a dedicated API key

At fleet scale the subscription gives **no cost advantage** (headless Claude Code draws a capped
agent-credit pool at API rates since 2026-06-15) and exhausts its caps fast. Use a **dedicated API
key** with its own spend limit — per-submitter keys resolve by who launched the run (see
[`architecture/03-adapters-and-auth.md`](architecture/03-adapters-and-auth.md)).

## Why the budget can read `$0` on a subscription

loopkit's budget stop (Ch 14) is fed by the cost the adapter reports. On a subscription, `claude`
often reports `total_cost_usd` as 0 (or omits it) — usage is drawn from the plan, not billed per
token. loopkit also now parses the CLI's array output correctly (a build that returned the array used
to read `$0` regardless), so the budget *can* bite when a number is present — but **don't rely on the
dollar ceiling alone on a subscription. Bound the run with `--max-iter`.** See
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) → "cost: $0.00".
