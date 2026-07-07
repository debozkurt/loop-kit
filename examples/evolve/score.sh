#!/usr/bin/env bash
# examples/evolve/score.sh — a GENERIC candidate scorer for evolutionary search (a ShellScorer).
#
# Ranks a FINISHED candidate's worktree; prints one float on the last line (higher = fitter; a
# non-zero exit or unparseable output scores -inf). evolve scores ALL finished candidates — including
# ones that GAVE UP (no_progress) with a tiny non-fix diff — so it must establish CORRECTNESS first,
# then rank the correct ones by a quality signal:
#   1. correctness (required): the held-out oracle must pass, else disqualify.
#   2. minimality: among correct candidates, the smaller diff wins (a minimal correct fix is cleaner).
# Swap step 2 for your own quality signal (an LLM judge's score, benchmark timing, etc.).
# CWD = the candidate's worktree.
set -uo pipefail
ORACLE="${GATE_ORACLE:?set GATE_ORACLE to your held-out acceptance command}"
BASE="${GATE_BASE:-origin/main}"

if ! eval "$ORACLE" >/dev/null 2>&1; then
  echo "-1000000"      # doesn't actually solve the goal → disqualified
  exit 0
fi
lines="$(git diff "$BASE"...HEAD 2>/dev/null | wc -l | tr -d ' ')"
echo $(( 100000 - lines ))
