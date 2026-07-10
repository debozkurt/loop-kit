#!/usr/bin/env bash
# acceptance/<key>/run.sh — a held-out oracle runner skeleton (spacer `acceptance/<finding>/run.sh`,
# generalized). It copies a HIDDEN test into the tree, runs it through the repo's suite, then removes
# it — so the test is never committed and never seen by the agent while it edits. This is the check the
# loop is graded on but cannot optimize against.
#
# $ACCEPTANCE_DIR points at this dir; CWD ($WORKSPACE) is the workspace clone.
# Contract: exit 0 = the fix is correct, non-zero = not yet (feedback on stdout).
#
# CRITICAL: verify this oracle FAILS on the current (buggy) tree before you trust it — the fail-first
# check: `loopkit synth-gate "bash acceptance/<key>/run.sh"` (add --fix <cmd> for the fail→pass proof).
# An oracle that passes on the buggy tree certifies nothing.
set -uo pipefail

# FILL 1 — where the hidden test lands in the repo (a path the agent is NOT told about):
HOLDOUT="FILL/path/in/repo/test_holdout.py"
# FILL 2 — the hidden test file that ships with this oracle (lives here, beside run.sh):
cp "$ACCEPTANCE_DIR/FILL_test_holdout.py" "$HOLDOUT"
trap 'rm -f "$HOLDOUT"' EXIT           # always remove it — never committed, never seen

# FILL 3 — run JUST the held-out test through the repo's real suite/runner (docker, tox, go test, …):
python -m pytest "$HOLDOUT" -q
