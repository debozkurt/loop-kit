"""Oracle synthesis — the fail-first verification of a proposed held-out gate (Part IV, Layer 2).

Proposing an acceptance test is easy; *proving it certifies anything* is the whole job, and it is the
half a copilot almost never does. loopkit's held-out **acceptance** gate is trustworthy only because
the loop can't optimize against it — but a gate the loop can't see is worthless unless it actually
discriminates a *buggy* tree from a *fixed* one. Two ways an unverified oracle lies:

  1. It **already passes** on the current (buggy) tree — so the goal doesn't reproduce, the code
     drifted / was already fixed, or the "test" never asserts the target behavior. It would certify
     DONE on tick zero. This is the classic verifier-hacking-adjacent failure: a green gate that
     measures nothing.
  2. It **can never pass** — an unsatisfiable oracle (a typo'd import, a wrong path, an assertion no
     correct fix would satisfy). It would fail forever and the loop would burn its whole budget
     against a mirage.

`synth-gate` is the primitive that catches both, by running the oracle across the fail→pass
transition SWE-bench validates its FAIL_TO_PASS tests with (apply the gold patch, confirm the test
flips):

  - **fail-first (mandatory):** run the oracle against the current tree — it MUST FAIL. This is the
    load-bearing check; it generalizes the `run --validate` pre-loop seam into a first-class
    "is this oracle real?" question, and it is the same guard the CI/unattended tier makes mandatory
    for goal-derived (attacker-shaped) oracles.
  - **pass-on-fix (optional, given a reference fix):** materialize an isolated copy, apply the fix,
    re-run the oracle — it MUST PASS. This proves the oracle is *satisfiable* and genuinely
    *discriminates* buggy-from-fixed. A gate that always fails certifies as little as one that always
    passes; only a gate that flips is real.

Only when every check holds does the oracle get **blessed** — and the verdict carries the oracle
command, the fix (if any), a short signature, the loopkit version, and a timestamp, so a blessing is
an auditable, reproducible provenance record rather than a transient "looked fine to me".

Design notes matching the kit's invariants:
  - **Verification, not generation.** This module never writes a test. The copilot (or a human)
    authors the oracle; loopkit only certifies it. If a step were "an LLM writes the oracle" it would
    belong in the `loopkit-mold` skill, not in code.
  - **Reuses the core gate machinery.** The oracle runs through a `ToolExecutor.run_gate` — the exact
    path the loop's held-out gate uses (credential-free child env, fail-closed timeout, shaped output),
    so what `synth-gate` blesses behaves identically when the loop later runs it. The runner is
    injectable so tests/other executors can substitute a fake — no tokens, no network.
  - **Never mutates the caller's tree.** The pass-on-fix check (and `--isolate` for fail-first) runs in
    a throwaway copy/clone; the reference fix only ever touches that copy.
  - Stdlib-only (`hashlib`, `json`, `shutil`, `tempfile`, `subprocess`, `dataclasses`) plus the core
    executor — importing this pulls no optional dependency; the core keeps no runtime dependency on it.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .. import secrets
from ..executor import GATE_TIMEOUT, LocalToolExecutor, shape_failure_output
from ..gate import GateResult
from ..log import get_logger

# The gate runner contract we reuse: `(command, workspace) -> GateResult`. The default is the core
# LocalToolExecutor's `run_gate` — the same code path the loop's held-out gate takes — so the oracle
# is verified exactly as it will later be run. Tests/other executors can inject their own.
GateRunner = Callable[[str, Path], GateResult]

# The check names, so callers/tests key off a stable string rather than positional order.
ENV_LIVE = "env-live"            # a trivial probe through the oracle's runner must PASS (env sanity)
FAIL_FIRST = "fail-first"        # the oracle must FAIL on the current (buggy) tree
PASS_ON_FIX = "pass-on-fix"      # given a reference fix, the oracle must PASS on the fixed tree

# Shell-level breakage signatures. An oracle that exits non-zero for one of THESE reasons did not
# "reproduce the bug" — it is a broken script (a parse error from an unbalanced quote, a missing
# interpreter, a non-executable file). fail-first treats any non-zero exit as met, so without this it
# would BLESS a broken oracle that then rejects every fix forever. (Real observed cases: an apostrophe
# in `${VAR:?…oracle's dir}` → `unexpected EOF`; a bare `python -m pytest` where only `python3`/`uv`
# exist → `command not found`.)
_BROKEN_ORACLE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("shell parse error", re.compile(r"syntax error|unexpected EOF|unexpected end of file", re.I)),
    ("command not found", re.compile(r"command not found|:\s*not found", re.I)),
    ("interpreter/script missing or not executable",
     re.compile(r"Permission denied|No such file or directory", re.I)),
)

# Tokens in an oracle command that look like a shell script we can parse-check up front.
_SH_TOKEN_RE = re.compile(r"(?:^|\s)(/?[\w.@/+-]+\.sh)\b")


def broken_oracle_reason(output: str | None) -> str | None:
    """If the oracle's output shows shell-level breakage, return a short reason, else None.

    Distinguishes "the oracle FAILED because it is broken" (a parse error / missing command) from "the
    oracle FAILED because the bug reproduces" — only the latter is a genuine fail-first.
    """
    if not output:
        return None
    for reason, pat in _BROKEN_ORACLE_PATTERNS:
        if pat.search(output):
            return reason
    return None


def parse_check_oracle_scripts(oracle: str, *, which=shutil.which) -> str | None:
    """Proactively `bash -n` any shell script the oracle command invokes.

    Returns a short error string if a referenced `.sh` file has a syntax error (catching e.g. an
    unbalanced quote BEFORE a wasted gate run, with a precise message), else None. Best-effort: if
    `bash` isn't available or no script path resolves, it simply finds nothing and the runtime
    output-pattern check (`broken_oracle_reason`) remains the safety net.
    """
    bash = which("bash")
    if not bash:
        return None
    for m in _SH_TOKEN_RE.finditer(oracle):
        path = Path(m.group(1))
        if not path.is_file():
            continue
        proc = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            return f"`bash -n {path.name}` failed: {lines[-1] if lines else 'syntax error'}"
    return None


@dataclass
class OracleCheck:
    """One leg of the verification: what we expected of the oracle, and whether it held.

    `expected` is "fail" or "pass" (what a *real* oracle does at this stage); `passed_gate` is what
    the oracle's exit code actually said (True == exit 0); `ok` is whether those agree. `evidence` is
    the shaped, budget-bounded gate output — kept even on success so the molder can eyeball *why* it
    failed-first (that it fails for the right reason, not an unrelated import error).
    """

    name: str
    expected: str                        # "fail" | "pass"
    passed_gate: bool                    # did the oracle command exit 0?
    ok: bool                             # did the outcome match `expected`?
    detail: str                          # one-line human summary of the outcome
    evidence: str | None = None          # shaped oracle output (bounded), for the molder to inspect
    broken: bool = False                 # the oracle FAILED for a BROKEN reason (parse error / missing
                                         # command / non-exec) — a non-genuine reproduction, never blessed


@dataclass
class OracleVerdict:
    """The result of verifying one proposed oracle — the molding provenance artifact.

    `blessed` is the bottom line: every check held, so the oracle is a real, fail-first (and, if a fix
    was supplied, satisfiable) held-out gate. JSON-serializable so it can be stored beside the oracle
    as an auditable attestation and re-compared later.
    """

    oracle: str
    target: str
    blessed: bool
    checks: list[OracleCheck]
    signature: str                       # short hash of (oracle, fix, mode[, probe]) — the provenance identity
    loopkit_version: str
    timestamp: str                       # ISO-8601; supplied by the caller (no hidden clock)
    fix: str | None = None
    isolated: bool = False               # was the fail-first check run in a throwaway copy?
    # Env-liveness (the fix-free half of pass-on-fix): did a trivial guaranteed-pass probe through
    # the oracle's own runner PASS in the same tree fail-first ran in? True/False when a probe was
    # supplied; None = unprobed (no probe given — fail-first's diagnosis is then unconfirmed against
    # environmental breakage: SCRAM auth, SIGABRT/non-relocatable venv, missing deps all exit
    # non-zero exactly like a genuine reproduction).
    env_live: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def oracle_signature(oracle: str, fix: str | None, mode: str, probe: str | None = None) -> str:
    """A short, stable hash of what was verified — the oracle command, the fix, the copy mode, and
    (when supplied) the env-liveness probe.

    Ties a blessing to the *exact* inputs it certified: change the oracle or the reference fix and the
    signature changes, so a stored verdict can never be silently reused for a different oracle. Mirrors
    `measure.harness_signature` — a certificate that doesn't name what it certifies isn't a certificate.
    The probe key is added only when present, so every pre-probe signature stays byte-stable.
    """
    payload: dict = {"oracle": oracle, "fix": fix, "mode": mode}
    if probe is not None:
        payload["probe"] = probe
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def _default_runner() -> GateRunner:
    # Deferred construction (not a module-level singleton) so importing this module runs no code that
    # touches the executor's process state; matches gate.py's lazy LocalToolExecutor construction.
    return LocalToolExecutor().run_gate


def _materialize(src: Path, dst: Path, *, mode: str) -> None:
    """Faithfully reproduce `src` at `dst` so the fix + oracle run against a throwaway tree.

    `copy` (default): `shutil.copytree` — includes the *uncommitted* working state, so "the current
    tree" the molder is verifying is exactly what gets checked, and `.git` comes along so a
    `git apply`/`git checkout` fix works. `clone`: `git clone` — committed state only (a real repo /
    URL, no working-tree cruft). Copy is the default because at molding time the buggy state is
    usually the working tree, not necessarily a committed ref.
    """
    if mode == "copy":
        # symlinks=True is load-bearing, not tidiness: copytree's default DEREFERENCES links, which
        # turns an in-tree venv's `bin/python` (a link to the interpreter) into a bare copy of that
        # binary, severed from its `../lib/libpython*.dylib` — the copy then dies under dyld and every
        # oracle run in the tree fails environmentally. Recreating links AS links also means a stale
        # link in the source copies fine instead of aborting the whole materialization.
        shutil.copytree(src, dst, symlinks=True)
    elif mode == "clone":
        proc = subprocess.run(["git", "clone", "--quiet", str(src), str(dst)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {(proc.stderr or proc.stdout).strip()[-200:]}")
    else:
        raise ValueError(f"unknown mode {mode!r} (expected: copy | clone)")


def _run_oracle(run_gate: GateRunner, oracle: str, workspace: Path, *, expect_pass: bool,
                name: str, tail: int) -> OracleCheck:
    """Run the oracle once and score it against `expect_pass` (what a real oracle does at this stage)."""
    result = run_gate(oracle, workspace)
    passed = bool(result.passed)
    ok = (passed == expect_pass)
    expected = "pass" if expect_pass else "fail"
    if name == FAIL_FIRST:
        detail = ("the oracle FAILS on the current tree — it reproduces the goal, so passing it later "
                  "is real evidence" if ok else
                  "the oracle ALREADY PASSES on the current tree — it certifies nothing (goal does not "
                  "reproduce, the code drifted/was already fixed, or the test never asserts the target)")
    else:  # PASS_ON_FIX
        detail = ("the oracle PASSES once the reference fix is applied — it is satisfiable and "
                  "discriminates buggy-from-fixed" if ok else
                  "the oracle STILL FAILS after the reference fix — it does not discriminate "
                  "buggy-from-fixed (an unsatisfiable oracle certifies nothing either)")
    # Keep the oracle's output as evidence. On a fail-first pass the GateResult carries the failing
    # log (useful — it shows *how* it fails); on a surprising outcome it's the diagnosis. When the
    # gate passed we have no captured output, so synthesize a short note.
    evidence = result.feedback if result.feedback else ("(oracle exited 0 — no output captured)"
                                                        if passed else None)
    # A fail-first "failure" that is really shell-level breakage (a parse error, a missing command) is
    # NOT a genuine reproduction — flag it so it can't be blessed, with a distinct, actionable detail.
    broken = False
    if name == FAIL_FIRST and not passed:
        reason = broken_oracle_reason(result.feedback)
        if reason:
            broken, ok = True, False
            detail = (f"the oracle FAILED but for a BROKEN reason ({reason}) — not a genuine "
                      "reproduction of the goal; fix the oracle script, then re-verify")
    if evidence:
        evidence = shape_failure_output(evidence, budget=tail)
    return OracleCheck(name=name, expected=expected, passed_gate=passed, ok=ok, detail=detail,
                       evidence=evidence, broken=broken)


def verify_oracle(oracle: str, workspace: Path, *, timestamp: str, fix: str | None = None,
                  mode: str = "copy", isolate: bool = False, tail: int = 2000,
                  run_gate: GateRunner | None = None,
                  loopkit_version: str | None = None,
                  probe: str | None = None) -> OracleVerdict:
    """Verify a proposed held-out oracle is real: env-live (given a probe), fail-first, and (given a
    fix) fail→pass.

    Runs the mandatory **fail-first** check — the oracle must FAIL on the current tree — and, when
    `fix` is supplied, the gold **pass-on-fix** check: apply the reference fix to an isolated copy and
    require the oracle to PASS. The oracle is `blessed` iff every check holds.

    **env-live** (`probe`, optional): fail-first alone cannot distinguish a *diagnostic* failure (the
    assertion caught the bug) from an *environmental* one (auth to a test DB down, a missing dep, a
    non-relocatable venv SIGABRTing in the copy) — both exit non-zero, and output-signature matching
    only ever catches the failure classes already seen. The probe is the positive proof: a trivial
    guaranteed-pass command through the oracle's OWN runner, run in the SAME tree fail-first will use
    (crucially: inside the isolated copy when isolating — a copy can break an env that was healthy at
    the source). If even the probe cannot PASS, the environment is broken: the verdict records
    `env-live` failed + `env_live: false`, **fail-first is skipped** (its output would be the same
    environmental noise misread as a reproduction), and the oracle is never blessed. No probe ⇒
    `env_live: null` — verified as before, honestly marked unprobed. This is the fix-free half of
    pass-on-fix: an env-broken oracle can never pass, but proving that normally needs a fix; the
    probe needs none.

    Isolation: with a `fix`, both checks run in one throwaway copy (the fix only touches that copy, and
    fail→pass is proven on a single materialized tree). Without a fix, fail-first runs **in place** by
    default (cheap, matching `run --validate`); pass `isolate=True` to run it in a copy too — the right
    choice when the oracle is goal-derived/untrusted (CI) and must not touch or litter the real tree.

    `run_gate` defaults to the core `LocalToolExecutor.run_gate` (the exact held-out-gate path);
    inject a fake for tests. `timestamp` is passed in, not read from a hidden clock, so the verdict is
    reproducible.
    """
    log = get_logger("synth-gate")
    workspace = Path(workspace).resolve()
    runner = run_gate or _default_runner()
    sig = oracle_signature(oracle, fix, mode, probe)
    from .. import __version__ as _v
    version = loopkit_version or _v

    # Validity precheck (cheap, before any gate run): a broken oracle script "fails" for the wrong
    # reason and fail-first would bless it. `bash -n` the scripts it invokes and short-circuit with a
    # distinct BROKEN verdict — a precise "fix the oracle" signal, not a bogus blessing.
    parse_err = parse_check_oracle_scripts(oracle)
    if parse_err:
        log.warn("verify.oracle_broken", stage="parse", reason=parse_err)
        check = OracleCheck(
            name=FAIL_FIRST, expected="fail", passed_gate=False, ok=False, broken=True,
            detail=f"the oracle is BROKEN, not failing-for-the-right-reason — {parse_err}. "
                   "Fix the oracle script, then re-verify.",
            evidence=parse_err)
        return OracleVerdict(oracle=oracle, target=str(workspace), blessed=False, checks=[check],
                             signature=sig, loopkit_version=version, timestamp=timestamp, fix=fix,
                             isolated=False)

    # A fix forces isolation (we're about to mutate a tree); `isolate` opts fail-first into a copy too.
    isolated = isolate or fix is not None
    checks: list[OracleCheck] = []
    env_live: bool | None = None

    def _env_probe(tree: Path) -> bool:
        """Run the env-liveness probe in `tree`; append its check. Returns False ⇒ short-circuit."""
        nonlocal env_live
        if probe is None:
            return True
        log.info("verify.start", check=ENV_LIVE, isolated=isolated)
        result = runner(probe, tree)
        env_live = bool(result.passed)
        if env_live:
            checks.append(OracleCheck(
                name=ENV_LIVE, expected="pass", passed_gate=True, ok=True,
                detail="the runner passes a trivial probe in this tree — a fail-first failure "
                       "below is diagnostic, not environmental"))
            return True
        evidence = (shape_failure_output(result.feedback, budget=tail)
                    if result.feedback else None)
        log.warn("verify.env_broken", check=ENV_LIVE)
        checks.append(OracleCheck(
            name=ENV_LIVE, expected="pass", passed_gate=False, ok=False, broken=True,
            detail="the ENVIRONMENT is broken — the oracle's runner cannot pass even a trivial "
                   "guaranteed-pass probe in this tree, so a failing oracle here proves nothing "
                   "(the auth-down / missing-dep / non-relocatable-venv class). fail-first was "
                   "SKIPPED; fix the environment (or the probe), then re-verify.",
            evidence=evidence))
        return False

    if not isolated:
        # Common case: verify fail-first in place, exactly like the `--validate` pre-loop seam.
        if _env_probe(workspace):
            log.info("verify.start", check=FAIL_FIRST, isolated=False, hasFix=False)
            checks.append(_run_oracle(runner, oracle, workspace, expect_pass=False,
                                      name=FAIL_FIRST, tail=tail))
    else:
        # Isolated: materialize once, PROBE the copy (materialization itself can break an env that
        # was healthy at the source — the observed case: copytree duplicating a non-relocatable
        # venv), then run fail-first there, then (if a fix) apply it and run pass-on-fix on the
        # SAME tree — the truest fail→pass discrimination proof, and it never touches the caller.
        with tempfile.TemporaryDirectory(prefix="loopkit-synth-") as scratch:
            copy = Path(scratch) / "tree"
            log.info("verify.materialize", mode=mode)
            _materialize(workspace, copy, mode=mode)
            if _env_probe(copy):           # env-broken ⇒ skip every oracle run in this dead tree
                log.info("verify.start", check=FAIL_FIRST, isolated=True, hasFix=fix is not None)
                checks.append(_run_oracle(runner, oracle, copy, expect_pass=False,
                                          name=FAIL_FIRST, tail=tail))
                if fix is not None:
                    # Apply the reference fix in the copy with the same credential-free child env the
                    # gate gets, so the fix can't reach a token either. A fix that itself errors is a
                    # failed pass-on-fix check (we can't prove the oracle flips if the fix never landed).
                    env = {**secrets.current().child_env(), "PYTHONDONTWRITEBYTECODE": "1"}
                    proc = subprocess.run(fix, cwd=copy, shell=True, env=env, capture_output=True,
                                          text=True, timeout=GATE_TIMEOUT)
                    if proc.returncode != 0:
                        out = shape_failure_output((proc.stdout or "") + (proc.stderr or ""),
                                                   budget=tail)
                        log.warn("verify.fix_failed", rc=proc.returncode)
                        checks.append(OracleCheck(
                            name=PASS_ON_FIX, expected="pass", passed_gate=False, ok=False,
                            detail=f"the reference fix did not apply cleanly (exit {proc.returncode}) "
                                   "— cannot prove the oracle flips",
                            evidence=out or None))
                    else:
                        log.info("verify.start", check=PASS_ON_FIX, isolated=True)
                        checks.append(_run_oracle(runner, oracle, copy, expect_pass=True,
                                                  name=PASS_ON_FIX, tail=tail))

    blessed = all(c.ok for c in checks)
    log.info("verify.done", blessed=blessed, checks=len(checks),
             failed=[c.name for c in checks if not c.ok], sig=sig, envLive=env_live)
    return OracleVerdict(
        oracle=oracle, target=str(workspace), blessed=blessed, checks=checks, signature=sig,
        loopkit_version=version, timestamp=timestamp, fix=fix, isolated=isolated,
        env_live=env_live)
