# Part III — security review (full-flow + sidecar adjacencies)

> **Scope.** An adversarial end-to-end pass over the cloud run flow, focused on the Phase-6 agent-isolation
> boundary (the keyless-executor sidecar) and the Phase-5b skills flywheel. Every claim is grounded in
> code. **Findings A–C are fixed (Built 🟢, 2026-06-21, +11 tests in `tests/test_security_hardening.py`);
> D–G are tracked follow-ups.** This doc is the canonical record; [`04-security.md`](architecture/04-security.md)
> carries the summary rows.

## The flow and the trust boundary

```
trigger (webhook HMAC / cron / CLI / CI) → create_run() ─ guard ─ apply ns · no-API SA · quota ·
                                                          NetworkPolicy · Cilium FQDN · creds Secret · Jobs
worker pod (ns/run-<id>)
  ├─ executor  (uid 1001, NO secret)        serve() on a Unix socket   ← runs every untrusted command
  └─ loopkit-core (uid 1000, key via envFrom)
        secrets.install(load → shred env + non-dumpable) · umask 002 · RemoteToolExecutor(sock)
        BRPOP → clone target → /scratch/work · clone skills → /scratch/skills-repo
        tick: build_prompt(skills) · LLM call (KEY, in core) · tool calls ──socket──► executor (keyless)
              protected-path guard (core) · git commit (core) · gates ──socket──► executor.run_gate
        on DONE: skills.write_back (gate via executor; push via core git) · push branch + draft PR (core)
```

**The invariant under test:** *loopkit-core holds the key and runs only trusted code; the executor runs
every model-/issue-influenced command and never has the key.*

## Findings

| # | Sev | Finding | Status |
|---|---|---|---|
| A | High | Executor can write `.git/` in the shared workspace → loopkit-core's git runs planted hooks/config as the key-holder (sidecar bypass) | **Fixed 🟢** |
| B | High | Skills flywheel is a stored-injection channel: attacker goal → skill → shared `main` → every future prompt | **Fixed 🟢** (content) + namespacing (deploy) |
| C | Med-High | CI/local tier: in-process key protected only by node `ptrace_scope` (unpinned) | **Fixed 🟢** |
| D | Medium | No subprocess timeouts + serial executor + no Job `activeDeadlineSeconds` → a hung tool wedges the run | **Fixed 🟢** |
| E | Medium | Shared Redis has no AUTH; the executor shares core's netns + `:6379` is allowed → cross-run keyspace tamper | Tracked |
| F | Medium | `github.com` must be in the FQDN allowlist → content exfil to an attacker's repo is *within policy* | Tracked (separate-pod) |
| G | Medium | Skills repo: full-history clone-per-task + unbounded render into the prompt | **Fixed 🟢** |

---

### A — git-hook / `.git`-write bypass of the sidecar — **Fixed 🟢**

**The crack.** The split assumes untrusted code never runs as uid 1000 in loopkit-core's namespace. But
loopkit-core runs `git` *inside the shared workspace*: `durability.commit_progress` (`git commit` every
tick, no `--no-verify`), `git checkout` (`ensure_branch`/`revert_uncommitted`), `git clone`, and
`remote.push_branch` (`git push`). The worker sets `umask 002` (`cli.py`) so the executor (gid 1000) can
edit cloned files — which also makes `.git/hooks` group-writable. The executor's `run_bash` is unconfined
within its container, so it can `echo … > .git/hooks/pre-commit; chmod +x`. loopkit-core's next `git
commit` would execute that hook **as uid 1000, in its own namespace, with its ambient env** — and the
protected-path guard can't see it (`.git/` never appears in `git status`).

**Impact.** *Always:* the invariant is void — a `pre-commit` hook can tamper the tree **after the gate
passed but before the push** (backdoor the PR), and egress as core. *Key disclosure:* depends on the
node's `kernel.yama.ptrace_scope` (a descendant hook reading the ancestor's heap/`environ` is allowed at
scope 0, denied at ≥1) — unpinned, so the boundary silently depended on node config. `.git/config` was
also writable → an injected `credential.helper` (multi-valued — both run) could capture the token, and
`core.fsmonitor` runs a command on status/add/commit.

**Fix.** Harden every loopkit-core git invocation (`durability.HARDENED_GIT_FLAGS`, threaded through
`durability._git`, `remote.run_git`, `fleet._git`, and the `_prepare_repo` clone):
`-c core.hooksPath=/dev/null -c core.fsmonitor=false` — command-line `-c` is highest precedence, so it
**overrides any injected `.git/config`** and makes git ignore `.git/hooks/` entirely. Authenticated ops
additionally **reset the credential-helper list** (`-c credential.helper=` before loopkit's) so an injected
helper is dropped. Plus the non-dumpable hardening from C. *Proven:* `test_security_hardening` plants a
*blocking* `pre-commit` (exit 1) and a `post-checkout` hook and asserts the commit succeeds and the hook
body never runs.

*Residual:* this neutralizes the known config/hook exec vectors but is a deny-list; the stronger structural
control is a **core-private `.git`** (clone `--separate-git-dir` onto a volume the executor can't mount,
with loopkit-core git pinned to it via `GIT_DIR`/`GIT_WORK_TREE`) — eliminates the class outright. Tracked
as the next hardening if the threat model warrants it.

### B — skills flywheel stored-injection / poisoning — **Fixed 🟢 (content) + namespacing (deploy)**

A skill's content derives from the **goal** (an issue body on trigger paths) and is rendered into **every
future run's** prompt; the write-back gate only checks learn-worthiness, not content; and Phase-5b pushes
directly to the shared repo's `main` (no human gate) — so a run that reaches DONE could persist
instructions across runs and tenants. **Fix** (`skills._sanitize_skill`, applied in `_vet` so all tiers
get it): refuse any skill carrying a credential-shaped value (`secrets.scan_for_secrets`); cap guidance
length + strip control chars; the default distiller now **quotes a truncated goal as provenance** rather
than echoing it as an imperative; the rendered block header is reframed **advisory, not authoritative**.
**Deployment control** for blast radius: namespace the skills home **per tenant** (a separate
`--skills-repo`/`--skills-branch`), so a poisoned skill only re-enters its own runs — already supported,
documented as the multi-tenant recommendation. *Honest note:* content sanitization is a mitigation, not a
boundary (a determined injection in free-text guidance survives); namespacing + a review gate for any
*shared* skills are the real controls, and direct-push-to-`main` is correct only single-tenant.

### C — CI/local key protection was node-dependent — **Fixed 🟢**

The CI tier (5c) runs the agent in-process with no sidecar; `secrets.load` shreds env vars but the key
lives in heap, and `run_bash` is a same-uid child. The shred defeats `printenv`/`os.environ`, but **not**
`cat /proc/<loopkit>/environ` (the proc file still shows the original exec-time block — `os.environ.pop`
doesn't scrub it) or a same-uid ptrace — both governed by the node's `ptrace_scope`. **Fix:** mark the
key-holder **non-dumpable** (`prctl(PR_SET_DUMPABLE, 0)` in `secrets._set_non_dumpable`, called from
`_harden`, so every worker/run entrypoint gets it). A non-dumpable process has root-owned `/proc/<pid>/{mem,environ}`
→ a same-uid neighbour can't read the heap or environ and can't ptrace it, **independent of `ptrace_scope`**.
Linux-only, best-effort (no-op on macOS/dev). This also backstops A's key-disclosure sub-case for cloud
loopkit-core. *Proven:* a subprocess probe asserts `Dumpable:\t0` in `/proc/self/status` on Linux.

---

## Follow-ups (D–G)

- **D — liveness bounds — Fixed 🟢.** Nested deadlines so nothing wedges a run. Per-call **subprocess
  timeouts**: `executor.TOOL_TIMEOUT` (120 s) on the agent's `run_bash` and `executor.GATE_TIMEOUT`
  (600 s) on the held-out gate — a `run_bash "sleep infinity"` now fails the *tool call* (the model reads
  the timeout and adapts) and a hung gate **fails closed** (a DONE cert can never come from a gate that
  didn't finish). The `RemoteToolExecutor` socket deadline sits *above* `GATE_TIMEOUT` (so the executor
  returns a clean failed-gate result rather than the client severing mid-gate). A Job-wide
  **`activeDeadlineSeconds`** (`RunSpec.active_deadline_seconds`, 3 h default) on **both** Jobs is the
  outer wall: a pod that somehow outlives the per-call deadlines is terminated by the kubelet instead of
  burning a node. *Not changed:* `executor.serve()` stays a serial accept loop — there is exactly one
  loopkit-core peer per pod, so serial is correct, and the per-call timeout bounds how long any one
  request can hold it. Tests: `test_executor.py` (tool/gate timeout + the socket-above-gate invariant),
  `test_cloudrun.py::test_both_jobs_have_an_active_deadline_wall`.
- **E — shared Redis isolation.** `10-redis.yaml` has no `requirepass`/ACL and the per-run NetworkPolicy
  allows `:6379` from all pods in the ns (incl. the executor, which shares core's netns). The per-run
  keyspace is a **prefix, not a boundary** → a prompt-injected agent can read/write other runs' keys.
  *Plan:* Redis AUTH with a per-run password in the run Secret, or an ACL per keyspace.
- **F — content exfil within the allowlist.** `github.com`/`*.githubusercontent.com` must be allowed for
  the tool to work, so the executor can `git push`/`curl` workspace + issue content to an
  attacker-controlled repo *within policy* (the pre-push scan only covers core's work-branch push). This
  is the known same-pod-netns residual; the real fix is the deferred **separate-pod** executor (own netns).
- **G — skills repo growth — Fixed 🟢.** `_SubprocessGitTransport.pull` now does a **shallow clone**
  (`--depth 1 --no-local`) — a per-task clone only ever needs the current tip, never the history; the
  shallow boundary is exactly the merge-base the file-disjoint concurrent-push rebase needs, so the
  rebase-retry still works (and the concurrent test now exercises a *genuinely* shallow clone, matching
  production). `render()` is bounded by a **render budget** (`skills._MAX_RENDER`, 12 KB): skills past the
  budget are dropped with a visible `_[N more skill(s) omitted]_` note — honest, not silent — atop the
  existing per-skill `_MAX_GUIDANCE` (2 KB) cap. *Still open (richer, deferred):* a **relevance-ranked**
  selection (render the skills closest to *this* goal, not the first name-sorted N) and a skills-repo size
  cap. Tests: `test_skills_repo.py` (`test_clone_is_shallow_but_renders_the_whole_tip`,
  `test_render_is_budget_bounded_and_honest`).

## Test coverage added

**A–C (264 → 275):** `tests/test_security_hardening.py` (11): the pre-commit/post-checkout hook-bypass
(behavioral), the hardened-flags + credential-helper-reset argv, the skill secret-refusal / length-cap /
control-char-strip / distiller-reframe, and the non-dumpable probe (Linux-asserted, no-raise everywhere).

**D + G (287 → 293):** liveness bounds — `tests/test_executor.py` (`run_bash` times out → tool error;
gate times out → fail-closed; the socket deadline sits above the gate deadline) +
`tests/test_cloudrun.py::test_both_jobs_have_an_active_deadline_wall`; bounded flywheel —
`tests/test_skills_repo.py` (shallow clone renders the whole tip from one local commit; render is
budget-bounded and the omission is an honest note).
