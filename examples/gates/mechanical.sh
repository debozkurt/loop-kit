#!/usr/bin/env bash
# examples/gates/mechanical.sh — a GENERIC deterministic VERIFICATION gate (Ch 6-7).
#
# The fast, in-sample oracle the loop optimizes EVERY tick. It must be deterministic (no model), so a
# pass/fail verdict is a trustworthy stop signal — a flaky iteration gate corrupts every stop decision
# (probe it with `loopkit run --check-gate 5`). This skeleton just shows the contract:
#
#   exit 0          = pass
#   exit non-zero   = fail
#   stdout/stderr   = the feedback loopkit feeds back into the next tick's prompt
#
# Replace the checks below with whatever "plausibly correct" means for YOUR repo. Collect ALL failures
# so the agent gets the full list each tick (not just the first). Wire it as:
#   [gate]
#   iteration = "bash gate/mechanical.sh"
set -uo pipefail
fails=()

# --- replace these example checks with your real ones (tests / lint / build / structure / links) ---

command -v python >/dev/null 2>&1 || fails+=("python not found on PATH")

# A test suite:
#   python -m pytest -q                          || fails+=("tests failing")
# A linter / type-checker:
#   ruff check .                                 || fails+=("lint failing")
#   mypy src                                     || fails+=("type errors")
# A build (compiles cleanly = plausibly right):
#   go build ./... && go vet ./...               || fails+=("build/vet failing")
# A pure-shell structural assertion (no framework needed):
#   grep -q "## Unreleased" CHANGELOG.md         || fails+=("CHANGELOG missing an Unreleased section")
#   test -f docs/api.md                          || fails+=("docs/api.md not generated")

# ---------------------------------------------------------------------------------------------------
if [ ${#fails[@]} -eq 0 ]; then
  echo "verification gate: PASS"
  exit 0
fi
echo "VERIFICATION GATE FAILED — fix these and re-run:"
for f in "${fails[@]}"; do echo "  - $f"; done
exit 1
