#!/usr/bin/env bash
# proposer.sh — a reference ShellProposer for `loopkit mold-batch` (the L5 judgment seam), stack-neutral.
#
# mold-batch materializes a tier-typed oracle SKELETON (acceptance/run.sh) and an env-liveness PROBE
# skeleton (acceptance/probe.sh) with FILL markers, then — when a proposer is configured — invokes this
# script to FILL both: a deterministic held-out oracle that FAILS on the current (buggy) tree and PASSES
# once the goal is fixed, plus a trivial guaranteed-pass probe through the SAME runner. The proposer is
# the judgment half of molding (mechanical code never writes a real test); a fresh-context headless
# coding agent does the filling.
#
# COPY THIS INTO YOUR REPO and edit the ONE per-repo block marked `EDIT ME` below. Everything else is
# generic: the MOLD_* contract, the agent prompt, and the FILL/probe validation are the same for any repo.
#
# ── Contract (loopkit/extensions/mold.py :: ShellProposer) ──────────────────────────────────────────
#   cwd                 = the target repo checkout (read-only for us by convention)
#   MOLD_TASK_ID        = task id
#   MOLD_TIER           = coverage tier (authz | wire-contract | silent-fallback | serializer |
#                         input-validation | concurrency | correctness)
#   MOLD_TIER_ASSERTION = the typed definition-of-done string for that tier
#   MOLD_GOAL_FILE      = path to the full goal text
#   MOLD_ORACLE_DIR     = the ONLY dir we may write into (run.sh + probe.sh + hidden test files)
#   MOLD_PROBE_FILE     = the env-liveness probe.sh to fill (set by loopkit ≥ Q3; unset on older loopkit
#                         ⇒ probe handling below is skipped, so this script stays drop-in compatible)
#   MOLD_TOUCHES_FILE   = optional: repo-relative SOURCE paths the FIX will touch (feeds `loopkit overlap`)
#   exit 0 = proposed (stdout tail becomes proposer-notes.md); non-zero = no proposal (needs-oracle)
#
# Output is UNTRUSTED by design: the mandatory isolated synth-gate (probe → fail-first) is what blesses
# an oracle, so this script never self-certifies.
set -uo pipefail

: "${MOLD_TASK_ID:?}" "${MOLD_TIER:?}" "${MOLD_GOAL_FILE:?}" "${MOLD_ORACLE_DIR:?}"
goal="$(cat "$MOLD_GOAL_FILE")"
skeleton=""; [ -f "$MOLD_ORACLE_DIR/run.sh" ] && skeleton="$(cat "$MOLD_ORACLE_DIR/run.sh")"
probe_skeleton=""; [ -n "${MOLD_PROBE_FILE:-}" ] && [ -f "$MOLD_PROBE_FILE" ] && \
  probe_skeleton="$(cat "$MOLD_PROBE_FILE")"

# ── EDIT ME: per-repo gate knowledge ────────────────────────────────────────────────────────────────
# This is the ONE repo-specific part. Point the oracle at your repo's REAL iteration gate — the same
# runner loopkit verifies against every tick — never a hand-rolled one. Delegating to the gate is
# load-bearing: a hand-rolled runner that diverges from the gate fails on an ENVIRONMENT error (missing
# dep, wrong interpreter, auth-down test DB, a non-relocatable venv broken by the isolated copy) that the
# synth-gate cannot tell from a real fail-first — so it would BLESS a broken oracle. The oracle must
# differ from the gate only in WHICH test runs, never HOW. If your gate needs env setup (a docker
# override, a `uv sync`/`npm ci`, a service up), put it IN the gate script so both share one environment.
#
# Replace the two lines below with your repo's gate command + one sentence of guidance for the agent.
# (Real worked example — spacer's docker/SCRAM/venv gates — lives in that repo's remediation/gates/.)
GATE_CMD="bash ./scripts/gate.sh"     # e.g. suite runner: pytest / go test / npm test, wrapped so it sets up env
gate_hint="run.sh MUST run the held-out test through this repo's iteration gate (\`$GATE_CMD <target>\`), \
never a hand-rolled runner — a divergent runner fails on an environment error the synth-gate reads as a \
false fail-first."
probe_hint="probe.sh MUST be a trivial GUARANTEED-PASS invocation of that SAME gate — a copied always-pass \
test run through it, or a collect/compile-only invocation (pytest --collect-only, go test -run '^$', …) — \
NEVER the held-out test."
# ── end EDIT ME ───────────────────────────────────────────────────────────────────────────────────────

# Probe section + rule only when loopkit supplied MOLD_PROBE_FILE (Q3+); skipped on older loopkit.
probe_section=""; probe_rule=""
if [ -n "${MOLD_PROBE_FILE:-}" ]; then
  probe_section="
## Probe skeleton (fill every FILL marker; keep the shape)
\`\`\`bash
$probe_skeleton
\`\`\`
"
  probe_rule="6. PROBE (required): also fill $MOLD_PROBE_FILE — the env-liveness probe synth-gate runs
   BEFORE fail-first, in the same isolated copy. $probe_hint
"
fi

prompt="You are writing a HELD-OUT ACCEPTANCE ORACLE for an automated remediation task. You are NOT
fixing the bug — you are writing the deterministic test that proves whether someone else fixed it.

## Task
id: $MOLD_TASK_ID
tier: $MOLD_TIER
tier definition-of-done: ${MOLD_TIER_ASSERTION:-"(none provided)"}

## Finding / goal
$goal

## Current oracle skeleton (fill every FILL marker; keep the overall shape)
\`\`\`bash
$skeleton
\`\`\`
$probe_section
## Rules
1. Explore the repo (cwd) READ-ONLY to find the real code paths, test conventions, and runners. Write
   files ONLY inside $MOLD_ORACLE_DIR — never modify the repo tree.
2. The finished oracle is $MOLD_ORACLE_DIR/run.sh: exit 0 = finding fixed, non-zero = finding present.
   It MUST fail on the CURRENT tree (fail-first) — assert the buggy BEHAVIOR is gone, not that files changed.
3. RUNNER (load-bearing): $gate_hint
4. Lock the CONTRACT, not the implementation: assert on observable behavior (HTTP status, wire shapes,
   function results), never on diff contents or internal structure. Prefer hand-rolled fakes over network.
5. Keep it minimal and deterministic — no sleeps-as-sync, no third-party network, no clock dependence.
   Extra test files go in $MOLD_ORACLE_DIR and run.sh wires them in.
${MOLD_TOUCHES_FILE:+   Also: write the repo-relative SOURCE paths you expect the FIX (not your test) to
   touch into $MOLD_TOUCHES_FILE, one per line — it feeds conflict prediction (loopkit overlap).
}${probe_rule}7. NEVER execute the gate, run.sh, or probe.sh during proposal — the synth-gate verifies them
   immediately after you finish (probe, then isolated fail-first), so self-testing is redundant spend and,
   on a shared gate resource, collides with a concurrent verify. Static checks only (e.g. \`bash -n\`).
8. Finish with one line: PROPOSED: <one-sentence summary of what the oracle asserts>."

# Grant write access to the oracle dir (and the touches file's dir when present). Swap `claude -p` for
# any headless coding-agent CLI; the scrubbed env carries no API key (subscription/OAuth on disk).
extra_dirs=(--add-dir "$MOLD_ORACLE_DIR")
[ -n "${MOLD_TOUCHES_FILE:-}" ] && extra_dirs+=(--add-dir "$(dirname "$MOLD_TOUCHES_FILE")")
out="$(printf '%s' "$prompt" | claude -p --permission-mode acceptEdits "${extra_dirs[@]}" 2>&1)"
status=$?
printf '%s\n' "$out" | tail -40

# ── Validation: propose only if the agent succeeded AND both files are complete ──────────────────────
# Match only the code-position fill TARGETS (`FILL_token` / `FILL/path`), never the bare word — a
# proposer may keep `# FILL 1 —` STEP LABELS or prose above the line it filled. Mirrors loopkit's
# _has_fill_markers; a naive `grep -q FILL` false-declines a fully-filled oracle.
if [ $status -ne 0 ] || [ ! -f "$MOLD_ORACLE_DIR/run.sh" ]; then
  echo "proposer: no usable proposal (agent exit=$status)"; exit 1
fi
if grep -Eq 'FILL[_/]' "$MOLD_ORACLE_DIR/run.sh"; then
  echo "proposer: FILL markers remain in run.sh — declining"; exit 1
fi
# The probe rides the same rule. `-f` FIRST: grep on a missing file exits non-zero, which would silently
# pass an ABSENT probe.
if [ -n "${MOLD_PROBE_FILE:-}" ]; then
  if [ ! -f "$MOLD_PROBE_FILE" ] || grep -Eq 'FILL[_/]' "$MOLD_PROBE_FILE"; then
    echo "proposer: probe missing or FILL markers remain ($MOLD_PROBE_FILE) — declining"; exit 1
  fi
  chmod +x "$MOLD_PROBE_FILE"
fi
chmod +x "$MOLD_ORACLE_DIR/run.sh"
exit 0
