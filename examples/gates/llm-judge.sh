#!/usr/bin/env bash
# examples/gates/llm-judge.sh — a GENERIC in-loop LLM-as-judge review command (for `--review`).
#
# Pairs with the `run --review <cmd>` flag (loopkit's ShellReviewHook). After each tick's commit, a
# FRESH agent session — no shared context with the coding agent — reviews ONLY the diff against an
# adversarial checklist and returns a verdict. A REJECT (or any non-verdict — FAIL-CLOSED) blocks
# DONE and its output is fed back as the next tick's feedback, so the agent self-corrects.
#
# Why a separate fresh session: a judge that shares the coder's context inherits its blind spots.
# Clean context + a "default to REJECT" framing is the cheap way to get independence. The judge reads
# the diff as text (no tools needed, so it cannot modify the workspace).
#
#   run --review "GATE_RUBRIC=./rubric.md bash gate/llm-judge.sh"
#
# Env: GATE_BASE (default origin/main), GATE_RUBRIC (optional extra criteria file),
#      GATE_JUDGE_CMD (the headless reviewer; default `claude -p`). CWD = the workspace.
set -uo pipefail

base="${GATE_BASE:-origin/main}"
git rev-parse --verify "$base" >/dev/null 2>&1 || base="HEAD~1"
judge_cmd="${GATE_JUDGE_CMD:-claude -p}"

diff="$(git diff "$base"...HEAD 2>/dev/null)"
[ -z "$diff" ] && diff="$(git diff HEAD~1..HEAD 2>/dev/null)"
if [ -z "$diff" ]; then echo "llm-judge: no diff to review — APPROVE by vacuity"; exit 0; fi

rubric=""
[ -n "${GATE_RUBRIC:-}" ] && [ -f "$GATE_RUBRIC" ] && rubric="

## Extra criteria (REJECT if any is unmet)
$(cat "$GATE_RUBRIC")"

read -r -d '' checklist <<'EOF'
You are a skeptical reviewer. Find reasons this diff should NOT be accepted; default to suspicion.
REJECT if any holds:
1. It papers over the symptom instead of fixing the root cause.
2. It weakens a check to pass — a test deleted/skipped/xfailed, a loosened assertion, a gate/CI edit.
3. It special-cases a specific test input (hardcoded values, magic branches) instead of a general fix.
4. A shipped test would still pass against the OLD (unfixed) code — i.e. it doesn't exercise the bug.
5. It breaks an interface/contract the change did not intend to (renamed/removed field, changed status).
6. It introduces a new secret, credential, or logged sensitive value.
EOF

prompt="$checklist$rubric

## The committed diff to review (vs $base)
\`\`\`diff
$diff
\`\`\`

End your response with EXACTLY one final line: 'VERDICT: APPROVE' or 'VERDICT: REJECT — <reason>'."

verdict="$(printf '%s' "$prompt" | $judge_cmd 2>&1)"
echo "$verdict"

# Fail closed: only an explicit APPROVE passes. REJECT, judge error, or a missing verdict blocks DONE.
last="$(printf '%s\n' "$verdict" | grep -aiE 'VERDICT:[[:space:]]*(APPROVE|REJECT)' | tail -1)"
printf '%s' "$last" | grep -qaiE 'VERDICT:[[:space:]]*APPROVE' && exit 0
[ -z "$last" ] && echo "llm-judge: no VERDICT line (judge error/refusal) — failing closed"
exit 1
