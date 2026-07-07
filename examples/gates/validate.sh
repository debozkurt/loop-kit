#!/usr/bin/env bash
# examples/gates/validate.sh — a GENERIC pre-loop VALIDATION check for `run --validate <cmd>`.
#
# Runs BEFORE the agent. Exit 0 = proceed; non-zero = abort (loopkit exits 3, the loop never runs)
# — so a stale or already-done task never spends a run. The canonical check: does the goal still
# REPRODUCE? Run your held-out acceptance oracle against the CURRENT tree; if it already PASSES, the
# work is already done (or the code drifted) — abort. This is the fail-first check, automated as a
# preflight (the mirror image of the acceptance gate: there the oracle must PASS to finish; here it
# must FAIL to start).
#
#   run --validate "GATE_ORACLE='bash gate/acceptance.sh' bash examples/gates/validate.sh"
set -uo pipefail
ORACLE="${GATE_ORACLE:?set GATE_ORACLE to your held-out acceptance command}"

if eval "$ORACLE" >/dev/null 2>&1; then
  echo "validate: the acceptance oracle ALREADY PASSES on the current tree — the goal appears already"
  echo "  done (or the code drifted). Aborting before the loop; verify by hand."
  exit 1
fi
echo "validate: the goal still reproduces (oracle fails on the current tree) — proceeding."
exit 0
