#!/usr/bin/env bash
# examples/gates/has-tests.sh — a GENERIC "the change must ship a test" structural gate.
#
# Enforces test-as-you-go mechanically: DONE is refused unless the run branch's diff adds or modifies
# at least one test file. It does NOT judge test quality (that's a reviewer's / human's job) — it only
# makes "wrote a test" a checkable stop condition instead of a hope, closing the loophole where an
# agent satisfies the acceptance gate without leaving any test behind for maintainers.
#
# Wire it as part of the acceptance gate (so it only has to hold at DONE), e.g.:
#   [gate]
#   acceptance = "bash gate/has-tests.sh && <your held-out test command>"
#
# Contract (loopkit gate): exit 0 = pass, non-zero = fail, stdout = feedback into the next tick.
# CWD = the workspace. Override the base ref with GATE_BASE (default: origin/main).
set -uo pipefail

base="${GATE_BASE:-origin/main}"
git rev-parse --verify "$base" >/dev/null 2>&1 || base="$(git rev-parse --abbrev-ref HEAD)@{upstream}"

changed="$(git diff --name-only "$base"...HEAD 2>/dev/null)"
if [ -z "$changed" ]; then
  echo "has-tests: no committed changes vs $base yet"
  exit 1
fi

# Common test-file conventions across languages. Extend the alternation for your stack.
#   Go: *_test.go   Python: test_*.py / *_test.py / tests/   JS-TS: *.test.* / *.spec.*
#   Rust: #[test] usually lives in-file, so also accept a tests/ dir   Java: *Test.java
if echo "$changed" | grep -Eq \
  '(_test\.go$|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)tests?/|\.(test|spec)\.[jt]sx?$|Test\.java$|_spec\.rb$)'; then
  exit 0
fi

echo "has-tests: FAIL — the diff ships no test file. Add a test co-located with the change."
echo "Files changed:"
echo "$changed" | sed 's/^/  /'
exit 1
