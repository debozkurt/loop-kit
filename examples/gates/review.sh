#!/usr/bin/env bash
# examples/gates/review.sh — a GENERIC "peer LLM review" gate: the held-out half of the TWO-ORACLE
# pattern (Ch 9). A SECOND model — one the author doesn't control — scores the change against a rubric
# the author can't edit. Pair it with a deterministic verification gate:
#
#   [gate]
#   iteration  = "bash gate/mechanical.sh"     # fast, deterministic — runs every tick (the "did I plausibly do it")
#   acceptance = "bash gate/review.sh"          # held-out peer review — runs ONCE before DONE (the "is it actually right")
#
# DONE requires BOTH. Why two oracles: an agent can fit the visible check without solving the goal
# (overfitting); a held-out reviewer it can't see or tune catches that. Protect the verifier so a run
# can't weaken its own grader (Ch 9 — verifier hacking):
#   [safety]
#   protected_paths = ["gate/"]                 # gate/review.sh AND gate/rubric.md
#
# Properties to respect:
#   * LLM-judged ⇒ NONDETERMINISTIC. Keep it as the ACCEPTANCE oracle (run once), never the per-tick
#     iteration gate — a flaky per-tick verdict corrupts every stop decision.
#   * It costs tokens. Gating it behind the (free) mechanical gate means no review is spent on a draft
#     that isn't even structurally done.
# Exit 0 = ACCEPT; non-zero = REJECT (the reviewer's reasons feed back as the next tick's input).
set -uo pipefail

RUBRIC="${REVIEW_RUBRIC:-gate/rubric.md}"               # the grading criteria (a protected path)
BASE="${REVIEW_BASE:-origin/main}"                      # what to diff the change against

[ -f "$RUBRIC" ] || { echo "review: rubric '$RUBRIC' missing (fail-closed)"; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "review: 'claude' CLI not found (fail-closed)"; exit 1; }

# What to review: the change THIS run produced. Default = the diff vs the base branch, else the last
# commit. Swap in whatever your task should be judged on (a built artifact, one file, the whole tree).
CHANGE="$(git diff "$BASE"...HEAD 2>/dev/null)"
[ -n "$CHANGE" ] || CHANGE="$(git show --stat --patch HEAD 2>/dev/null)"
[ -n "$CHANGE" ] || { echo "review: no change to review (fail-closed)"; exit 1; }

read -r -d '' INSTR <<'EOF' || true
You are a STRICT, skeptical peer reviewer. Apply the rubric below to the change under review. Be
adversarial: assume a requirement is unmet until the change proves it. Reply with your reasoning, then
end with EXACTLY one line: `VERDICT: ACCEPT` or `VERDICT: REJECT`.
EOF

PROMPT="$INSTR

=== RUBRIC ($RUBRIC) ===
$(cat "$RUBRIC")

=== CHANGE UNDER REVIEW ===
$CHANGE
"

OUT="$(claude -p "$PROMPT" --output-format text 2>&1)" \
  || { echo "review: claude invocation failed (fail-closed):"; echo "$OUT" | tail -5; exit 1; }

echo "$OUT"
if echo "$OUT" | grep -qiE 'VERDICT:[[:space:]]*ACCEPT'; then
  exit 0
fi
echo "--- peer review REJECTED the change (or returned no clear ACCEPT verdict) ---"
exit 1
