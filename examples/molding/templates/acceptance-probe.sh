#!/usr/bin/env bash
# acceptance/<key>/probe.sh — the env-liveness probe that rides beside run.sh (Q3, dogfooded from a
# 6/6 false-blessing batch). fail-first asks "did the oracle fail on the buggy tree?" — but it cannot
# tell a DIAGNOSTIC failure (the assertion caught the bug) from an ENVIRONMENTAL one (test-DB auth
# down, a missing dep, a venv broken by the isolated verify copy): both exit non-zero. This file is
# the positive proof the other checks can't provide: the SAME runner run.sh uses must pass a TRIVIAL,
# GUARANTEED-GREEN invocation here, or the environment is broken and a failing run.sh proves nothing.
#
# $ACCEPTANCE_DIR points at this dir; CWD ($WORKSPACE) is the workspace clone.
# Contract: exit 0 = environment alive. NEVER run the held-out test here — a probe that can fail for
# code reasons defeats its purpose.
#
# Verified automatically: `loopkit mold-batch` refuses to bless run.sh while this has FILL markers,
# and `loopkit synth-gate --probe "bash acceptance/<key>/probe.sh" ...` runs it (inside the isolated
# copy) BEFORE fail-first — probe fails ⇒ verdict `env-broken`, never a blessing.
set -uo pipefail

# FILL 1 — the same runner as run.sh, on something trivially green. Pick the cheapest true positive:
#   pytest:    uv run pytest --collect-only -q >/dev/null        (imports conftest, touches the venv)
#   go:        go test -run TestNothingMatchesThis ./... >/dev/null
#   DB-backed: docker compose run --rm test psql -c 'SELECT 1'   (proves auth + connectivity)
FILL_probe_command
