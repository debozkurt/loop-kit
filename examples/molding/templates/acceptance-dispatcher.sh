#!/usr/bin/env bash
# acceptance-dispatcher.sh — the held-out acceptance gate, chained (spacer `gates/acceptance.sh`,
# generalized). Runs, in order: (1) the diff-ships-a-test structural gate, then (2) this issue's
# held-out oracle. The oracle is the check the loop never optimizes against — the agent never sees it
# (it lives outside the workspace) — so passing it is real evidence the fix works, not evidence a
# visible test was gamed.
#
# Wire it:  [gate] acceptance = "GATE_KEY=<key> bash gate/acceptance-dispatcher.sh"
# CWD = the workspace clone.  Contract: exit 0 = pass, non-zero = fail (feedback on stdout).
set -uo pipefail

KEY="${GATE_KEY:?set GATE_KEY to the issue oracle dir name}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"      # where the gate scripts live (a protected dir)
HAS_TESTS="${GATE_HAS_TESTS:-$HERE/has-tests.sh}"          # copy examples/gates/has-tests.sh next to this
ORACLE="${GATE_ORACLE:-$HERE/../acceptance/$KEY/run.sh}"   # this issue's held-out oracle runner

# 1. Structural: the diff must ship a test (test-as-you-go).
if [ -x "$HAS_TESTS" ] || [ -f "$HAS_TESTS" ]; then
  if ! bash "$HAS_TESTS"; then
    exit 1
  fi
fi

# 2. Held-out oracle for this issue.
if [ ! -f "$ORACLE" ]; then
  echo "acceptance: no oracle for '$KEY' (expected $ORACLE)"
  exit 1
fi
WORKSPACE="$PWD" ACCEPTANCE_DIR="$(dirname "$ORACLE")" bash "$ORACLE"
