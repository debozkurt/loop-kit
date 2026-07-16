"""Unattended batch molding (Layer 5) — many tasks, no copilot per task. [Part IV]

Layers 1-4 gave the *molder* verified primitives: `detect` (mechanical config), `synth-gate`
(fail-first oracle verification), `route` (reliability-gated escalation), and the `loopkit-mold`
skill (the judgment playbook a copilot follows). This module is the connective tissue for the case
the kit was built for — **batch remediation**, where dozens of heterogeneous tasks each need a
molded instance and there is no copilot session per task.

It is deliberately *not* a monolith and *not* judgment-in-code:

- The **coverage-tier → typed-DoD table** carries the mechanical half of oracle proposal (classify
  the work → what its test must assert) — promoted from the skill into code because it IS a table.
- The **`ShellProposer` seam** carries the judgment half: an injected command (typically a
  fresh-context headless agent) fills what the template can't. No proposer configured ⇒ tasks stop
  at an annotated skeleton for a human/copilot to finish — mechanical-only never fakes an oracle.
- **`synth-gate` verification is mandatory and isolated** — a goal-derived oracle is untrusted
  input (the security boundary), so nothing is blessed without a fail-first proof in a throwaway
  copy, and nothing unblessed reaches the emitted batch.
- **The output is a reviewable artifact, never a run.** `mold-batch` ends by emitting a ready
  `loopkit batch` manifest + per-task provenance (detect evidence, oracle verdict, route decision);
  a human reviews the molded instances once, then launches `loopkit batch` themselves. The
  checkpoint is the seam between two commands, not pause/resume state inside one.

Two knobs govern "how much is molded at once", on independent axes:

- ``level`` — the stage ladder: ``detect`` < ``oracle`` < ``route`` < ``full``. Below-full stops
  early and emits partial instances; a future stage slots into the ladder without breaking callers.
- ``limit`` — how many unmolded tasks to process this invocation; a state file makes re-invocation
  resume where molding left off (successes are skipped, failures retry — so the human loop is
  "fill the skeleton, re-run").
"""
from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import tomllib
from pydantic import BaseModel, Field, model_validator

from .. import secrets
from ..log import get_logger
from .detect import RepoProfile, detect_repo
from .route import RouteDecision, route_from_report
from .synth_gate import OracleVerdict, verify_oracle

_log = get_logger("mold")

# The stage ladder — order is meaning: each level runs everything before it.
LEVELS = ("detect", "oracle", "route", "full")

# Task statuses. Success-per-level: detect→detected · oracle→verified · route→routed · full→ready.
# The rest are the honest stops: needs-oracle (skeleton awaiting judgment), oracle-rejected
# (fail-first refused to bless), needs-config (no test runner detected and none supplied).
DETECTED = "detected"
NEEDS_ORACLE = "needs-oracle"
ORACLE_REJECTED = "oracle-rejected"
VERIFIED = "verified"
ROUTED = "routed"
NEEDS_CONFIG = "needs-config"
READY = "ready"
_SUCCESS_FOR_LEVEL = {"detect": DETECTED, "oracle": VERIFIED, "route": ROUTED, "full": READY}

# The coverage-tier → typed-DoD table (the skill's `coverage-tiers.md`, as data): classifying the
# work turns "propose a test" into "propose THIS assertion" — the mechanical half of oracle
# synthesis. The full playbook (DoD assembly, two-test-sets discipline) stays in the skill.
TIER_ASSERTIONS: dict[str, str] = {
    "authz": ("a wrong-role / cross-tenant caller is rejected (403/404) AND the legitimate "
              "owner still succeeds"),
    "wire-contract": ("the wire shape (HTTP status + response fields) is locked before and after "
                      "the fix — no field renamed/removed unless the goal called for it"),
    "silent-fallback": ("the failure branch is exercised and lands on the SAFE default "
                        "(not just the happy path)"),
    "serializer": "the exact field set emitted — confidential fields ABSENT, public fields PRESENT",
    "input-validation": ("the boundary value (cap enforced, empty/whitespace handled) AND just "
                         "past it"),
    "concurrency": ("the race itself (transactional test / advisory lock / unique constraint) "
                    "fails without the fix"),
    "correctness": ("a test that fails against the current (buggy) code and passes after the fix"),
}

# The held-out oracle skeleton (the skill's `templates/acceptance-oracle.sh`, embedded so molding
# works from an installed package). FILL markers are the "judgment still owed" signal: a run.sh
# that still contains one is never verified, let alone blessed.
_ORACLE_SKELETON = """\
#!/usr/bin/env bash
# Held-out acceptance oracle for task '{task_id}' (tier: {tier}).
# The oracle must assert: {assertion}
#
# Contract: CWD is the workspace clone; $ACCEPTANCE_DIR points at this directory.
# Exit 0 = the fix is correct, non-zero = not yet (feedback on stdout).
# CRITICAL: it must FAIL on the current (buggy) tree — `loopkit mold-batch` verifies exactly that
# (fail-first) before this oracle is trusted; a FILL marker below blocks verification entirely.
set -uo pipefail

# FILL 1 — where the hidden test lands in the repo (a path the agent is NOT told about):
HOLDOUT="FILL/path/in/repo/test_holdout"
# FILL 2 — copy the hidden test that lives beside this script into the tree:
cp "$ACCEPTANCE_DIR/FILL_test_holdout" "$HOLDOUT"
trap 'rm -f "$HOLDOUT"' EXIT           # never committed, never seen by the agent

# FILL 3 — run JUST the held-out test through the repo's real runner:
FILL_test_command "$HOLDOUT"
"""


# --------------------------------------------------------------------------------------------
# Manifest — what to mold. Deliberately close to the batch manifest: mold consumes tasks that
# LACK a config/oracle and emits the completed batch manifest for the ones it finishes.
# --------------------------------------------------------------------------------------------
class MoldDefaults(BaseModel):
    """Batch-wide molding defaults."""

    repo: str | None = None               # the checkout the tasks target (per-task override below)
    provider: str = "auto"                # forge for issue-sourced goals
    proposer: str | None = None           # ShellProposer command — the judgment seam (see class)
    adapter: str | None = None            # emitted configs' agent (default: detect's pick)
    iteration: str | None = None          # emitted configs' iteration gate (default: detect's pick)
    max_cost_usd: float = 8.0             # per-task budget in emitted configs


class MoldSpec(BaseModel):
    """One task to mold: the goal, its coverage tier, and (optionally) calibration/fix references."""

    id: str
    goal: str | None = None
    issue: int | None = None
    title: str | None = None
    tier: str = "correctness"             # coverage tier → the oracle's typed assertion
    repo: str | None = None               # per-task target checkout (overrides [defaults] repo)
    iteration: str | None = None          # per-task iteration-gate override
    fix: str | None = None                # reference fix command → synth-gate's gold pass-on-fix
    report: str | None = None             # a `measure --out` ReliabilityReport for the route stage
    group: str | None = None              # passes through to the emitted batch manifest
    after: list[str] = Field(default_factory=list)
    touches: list[str] = Field(default_factory=list)  # predicted-touch paths — passes through for
                                          # `loopkit overlap` (advisory; never affects molding)

    @model_validator(mode="after")
    def _coherent(self) -> "MoldSpec":
        if self.goal is None and self.issue is None:
            raise ValueError(f"task '{self.id}': set either goal or issue — there is nothing to mold")
        if self.tier not in TIER_ASSERTIONS:
            raise ValueError(f"task '{self.id}': unknown tier '{self.tier}' "
                             f"(one of: {', '.join(TIER_ASSERTIONS)})")
        return self


class MoldManifest(BaseModel):
    """The whole molding batch: `[defaults]` + `[[task]]`, validated up front (same failure modes
    as the batch manifest: duplicate ids, dangling `after` references)."""

    defaults: MoldDefaults = Field(default_factory=MoldDefaults)
    task: list[MoldSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> "MoldManifest":
        ids = [t.id for t in self.task]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate task ids: {', '.join(sorted(dupes))}")
        known = set(ids)
        for t in self.task:
            missing = [d for d in t.after if d not in known]
            if missing:
                raise ValueError(f"task '{t.id}': after references unknown task(s): {', '.join(missing)}")
        for t in self.task:
            if t.repo is None and self.defaults.repo is None:
                raise ValueError(f"task '{t.id}': no repo — set [defaults] repo or a per-task repo")
        return self


def load_mold_manifest(path: str | Path) -> MoldManifest:
    """Read and validate a molding manifest TOML into a `MoldManifest`."""
    p = Path(path).expanduser()
    with p.open("rb") as handle:
        data = tomllib.load(handle)
    return MoldManifest.model_validate(data)


# --------------------------------------------------------------------------------------------
# The proposer seam — the judgment half, injected. Mechanical code never writes a real test.
# --------------------------------------------------------------------------------------------
@dataclass
class ProposeResult:
    ok: bool
    notes: str = ""


class ShellProposer:
    """Run an injected command that authors the oracle files — typically a fresh-context headless
    agent (the *triggering agent's* judgment, per the Part IV boundary; never a rule pretending
    to be judgment).

    Contract: the command runs with CWD = the target repo checkout (so it can read the code) and a
    scrubbed child env plus:

    - ``MOLD_TASK_ID`` / ``MOLD_TIER`` / ``MOLD_TIER_ASSERTION`` — what to prove;
    - ``MOLD_GOAL_FILE`` — a file holding the full goal text (env-safe for multi-line goals);
    - ``MOLD_ORACLE_DIR`` — where to (over)write ``run.sh`` + its hidden test files;
    - ``MOLD_TOUCHES_FILE`` — *optional byproduct*: the proposer explored the repo anyway, so it
      may write the repo-relative source paths it expects the FIX to touch, one per line. They
      fill the task's `touches` (advisory input to `loopkit overlap`) unless the author declared
      their own — observation over guessing, and never overriding a human declaration.

    Exit 0 = proposed (stdout kept as provenance notes); non-zero = no proposal. The proposer's
    output is *untrusted either way* — only `synth-gate` verification blesses it (and `touches`
    stays advisory by design, so a wrong observation costs at most a wrong warning).
    """

    def __init__(self, command: str, *, timeout: float = 900.0) -> None:
        self._command = command
        self._timeout = timeout

    def propose(self, spec: MoldSpec, oracle_dir: Path, workspace: Path,
                goal_file: Path, touches_file: Path | None = None) -> ProposeResult:
        env = {**secrets.current().child_env(), "PYTHONDONTWRITEBYTECODE": "1",
               "MOLD_TASK_ID": spec.id, "MOLD_TIER": spec.tier,
               "MOLD_TIER_ASSERTION": TIER_ASSERTIONS[spec.tier],
               "MOLD_GOAL_FILE": str(goal_file), "MOLD_ORACLE_DIR": str(oracle_dir)}
        if touches_file is not None:
            env["MOLD_TOUCHES_FILE"] = str(touches_file)
        try:
            proc = subprocess.run(self._command, cwd=workspace, shell=True, env=env,
                                  capture_output=True, text=True, timeout=self._timeout)
        except subprocess.TimeoutExpired:
            return ProposeResult(ok=False, notes=f"proposer timed out after {self._timeout}s")
        notes = ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
        return ProposeResult(ok=proc.returncode == 0, notes=secrets.redact(notes))


# --------------------------------------------------------------------------------------------
# The per-task pipeline: detect → propose → verify → route → emit. Pure over its inputs where
# possible; every stage leaves a provenance artifact in the task's output dir.
# --------------------------------------------------------------------------------------------
@dataclass
class MoldRow:
    """One task's molding outcome: how far it got and why it stopped there."""

    spec: MoldSpec
    status: str
    note: str = ""
    verdict: OracleVerdict | None = None
    route: RouteDecision | None = None


@dataclass
class MoldResult:
    rows: list[MoldRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)      # already molded (state file), untouched

    def by_status(self, status: str) -> list[MoldRow]:
        return [r for r in self.rows if r.status == status]

    @property
    def attention(self) -> list[MoldRow]:
        """Rows a human must look at before the batch is complete."""
        return [r for r in self.rows
                if r.status in (NEEDS_ORACLE, ORACLE_REJECTED, NEEDS_CONFIG)]


def oracle_command(oracle_dir: Path) -> str:
    """The gate command that runs a task's held-out oracle (CWD = the workspace, per the contract)."""
    return (f"ACCEPTANCE_DIR={shlex.quote(str(oracle_dir))} "
            f"bash {shlex.quote(str(oracle_dir / 'run.sh'))}")


def _has_fill_markers(path: Path) -> bool:
    try:
        return "FILL" in path.read_text()
    except OSError:
        return True                                       # unreadable = not a real oracle


def _read_touches(path: Path, cap: int = 50) -> list[str]:
    """Observed touch paths from a proposer's `touches.txt`: one per line, blanks/# skipped.

    Untrusted output gets a light cap — `touches` is advisory, but a runaway list shouldn't bloat
    the emitted manifest. The file itself stays in the task dir as provenance (and as the durable
    source for `emit_batch_manifest` on resumed runs, where the proposer doesn't re-run).
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    seen: list[str] = []
    for line in lines:
        entry = line.strip()
        if entry and not entry.startswith("#") and entry not in seen:
            seen.append(entry)
    return seen[:cap]


def mold_task(spec: MoldSpec, defaults: MoldDefaults, out_dir: Path, profile: RepoProfile, *,
              level: str, timestamp: str, proposer: ShellProposer | None = None,
              run_gate=None) -> MoldRow:
    """Mold one task up to `level`, leaving provenance at every stage (the reviewable instance).

    detect: record the repo profile. oracle: materialise the skeleton + tier guidance, let the
    proposer fill it, and — only for a FILL-free oracle — run the mandatory isolated fail-first
    verification (plus pass-on-fix when the spec carries a reference `fix`). route: decide
    single-vs-evolve from the spec's reliability report, or record "uncalibrated" honestly. full:
    render the per-task config wired to the blessed oracle.
    """
    task_dir = out_dir / spec.id
    task_dir.mkdir(parents=True, exist_ok=True)
    repo = Path(spec.repo or defaults.repo).expanduser().resolve()
    log = _log.bind(task=spec.id)
    (task_dir / "detect.json").write_text(profile.to_json() + "\n")
    if level == "detect":
        log.info("mold.stage", stage="detect", status=DETECTED)
        return MoldRow(spec=spec, status=DETECTED, note="repo profile recorded")

    # -- oracle -------------------------------------------------------------------------------
    acc_dir = task_dir / "acceptance"
    acc_dir.mkdir(exist_ok=True)
    run_sh = acc_dir / "run.sh"
    if not run_sh.exists():
        run_sh.write_text(_ORACLE_SKELETON.format(task_id=spec.id, tier=spec.tier,
                                                  assertion=TIER_ASSERTIONS[spec.tier]))
    goal_file = task_dir / "GOAL.md"
    goal_file.write_text(f"# {spec.title or spec.id}\n\ntier: {spec.tier}\n"
                         f"must assert: {TIER_ASSERTIONS[spec.tier]}\n\n{spec.goal or ''}\n")
    if proposer is not None and _has_fill_markers(run_sh):
        proposed = proposer.propose(spec, acc_dir, repo, goal_file,
                                    touches_file=task_dir / "touches.txt")
        (task_dir / "proposer-notes.md").write_text(proposed.notes + "\n")
        log.info("mold.propose", ok=proposed.ok, notesLen=len(proposed.notes))
        observed = _read_touches(task_dir / "touches.txt")
        if observed and not spec.touches:                 # author-declared touches always win
            spec.touches = observed
            log.info("mold.touches", observed=len(observed))
    if _has_fill_markers(run_sh):
        # Mechanical-only stops here, honestly: a skeleton is guidance, not an oracle. The human
        # loop is "fill the FILLs (or wire a --proposer), re-run mold-batch" — failures retry.
        log.info("mold.stage", stage="oracle", status=NEEDS_ORACLE)
        return MoldRow(spec=spec, status=NEEDS_ORACLE,
                       note="oracle skeleton awaits judgment (FILL markers present)")
    # Goal-derived oracles are untrusted input: verification is mandatory and isolated (a copy),
    # so an attacker-shaped oracle can neither be blessed green nor touch the real tree.
    verdict = verify_oracle(oracle_command(acc_dir), repo, timestamp=timestamp, fix=spec.fix,
                            isolate=True, run_gate=run_gate)
    (task_dir / "verdict.json").write_text(verdict.to_json() + "\n")
    if not verdict.blessed:
        failed = ", ".join(c.name for c in verdict.checks if not c.ok)
        log.info("mold.stage", stage="oracle", status=ORACLE_REJECTED, failed=failed)
        return MoldRow(spec=spec, status=ORACLE_REJECTED, verdict=verdict,
                       note=f"verification refused to bless: {failed}")
    if level == "oracle":
        log.info("mold.stage", stage="oracle", status=VERIFIED, sig=verdict.signature)
        return MoldRow(spec=spec, status=VERIFIED, verdict=verdict, note="oracle blessed fail-first")

    # -- route --------------------------------------------------------------------------------
    route: RouteDecision | None = None
    if spec.report:
        report = json.loads(Path(spec.report).expanduser().read_text())
        route = route_from_report(report, timestamp=timestamp)
        (task_dir / "route.json").write_text(route.to_json() + "\n")
        log.info("mold.stage", stage="route", strategy=route.strategy)
    else:
        # No measurement — say so, never guess. `measure --out` + `report = "..."` calibrates it.
        (task_dir / "route.json").write_text(json.dumps(
            {"strategy": "single", "reason": "uncalibrated — no reliability report supplied; "
             "run `loopkit measure --out` and set `report` to route on real pass^k"},
            indent=2) + "\n")
        log.info("mold.stage", stage="route", strategy="single", calibrated=False)
    if level == "route":
        return MoldRow(spec=spec, status=ROUTED, verdict=verdict, route=route,
                       note=route.strategy if route else "single (uncalibrated)")

    # -- full: the per-task config, wired to the blessed oracle --------------------------------
    iteration = spec.iteration or defaults.iteration or profile.test_command
    if not iteration:
        log.info("mold.stage", stage="full", status=NEEDS_CONFIG)
        return MoldRow(spec=spec, status=NEEDS_CONFIG, verdict=verdict, route=route,
                       note="no test runner detected and no iteration override supplied")
    config = _render_task_config(spec, defaults, profile, repo=repo, iteration=iteration,
                                 acceptance=oracle_command(acc_dir))
    (task_dir / "loopkit.toml").write_text(config)
    log.info("mold.stage", stage="full", status=READY)
    return MoldRow(spec=spec, status=READY, verdict=verdict, route=route,
                   note="config emitted; oracle wired")


def _render_task_config(spec: MoldSpec, defaults: MoldDefaults, profile: RepoProfile, *,
                        repo: Path, iteration: str, acceptance: str) -> str:
    """Render the per-task loopkit.toml (the `issue.loopkit.toml` template shape, values filled).

    `json.dumps` renders every string/list — its escapes are valid TOML basic-string syntax, so
    multi-line goals and quoted commands round-trip safely.
    """
    j = json.dumps
    adapter = defaults.adapter or profile.adapter
    protected = profile.protected_paths or []
    forbid = ["main", "master"]
    if profile.default_branch and profile.default_branch not in forbid:
        forbid.append(profile.default_branch)
    return (
        f"# generated by loopkit mold-batch for task '{spec.id}' — review before running\n"
        f"goal   = {j(spec.goal or f'Resolve issue #{spec.issue}')}\n"
        f"repo   = {j(str(repo))}\n"
        f"branch = {j(f'loopkit/{spec.id}')}\n\n"
        f"[agent]\nadapter      = {j(adapter)}\nmax_cost_usd = {defaults.max_cost_usd}\n\n"
        f"[gate]\niteration  = {j(iteration)}\nacceptance = {j(acceptance)}\n\n"
        f"[stops]\nmax_iter          = 12\nno_progress_after = 4\n\n"
        f"[safety]\nprotected_paths    = {j(protected)}\n"
        f"require_clean_tree = true\n"
        f"allow_branches     = [\"loopkit/*\"]\n"
        f"forbid_branches    = {j(forbid)}\n"
    )


# --------------------------------------------------------------------------------------------
# The batch driver: state-aware iteration + the emitted, ready-to-run batch manifest.
# --------------------------------------------------------------------------------------------
def _load_state(out_dir: Path) -> dict:
    state_path = out_dir / "state.json"
    if not state_path.exists():
        return {"tasks": {}}
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"tasks": {}}


def _already_molded(state: dict, spec_id: str, level: str) -> bool:
    """True iff the task previously *succeeded* at this level or deeper — failures always retry."""
    entry = state["tasks"].get(spec_id)
    if not entry:
        return False
    prior_level, status = entry.get("level"), entry.get("status")
    if prior_level not in LEVELS:
        return False
    return (LEVELS.index(prior_level) >= LEVELS.index(level)
            and status == _SUCCESS_FOR_LEVEL.get(prior_level))


def mold_batch(manifest: MoldManifest, out_dir: Path, *, level: str, timestamp: str,
               limit: int | None = None, force: bool = False,
               proposer: ShellProposer | None = None, run_gate=None) -> MoldResult:
    """Mold up to `limit` unmolded tasks to `level`, then (at level full) emit the batch manifest.

    Idempotent by the state file: a task that already succeeded at this level (or deeper) is
    skipped unless `force`; a task that stopped at needs-oracle / oracle-rejected is always
    retried, because the expected loop is "human (or proposer) improves the oracle → re-run".
    The emitted `batch.toml` includes only READY tasks; everything else is listed in a trailing
    comment block so nothing silently disappears.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level '{level}' (one of: {', '.join(LEVELS)})")
    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(out_dir)
    profiles: dict[str, RepoProfile] = {}                 # detect once per distinct repo
    result = MoldResult()
    processed = 0
    _log.info("mold.start", tasks=len(manifest.task), level=level, limit=limit or "-")
    for spec in manifest.task:
        if not force and _already_molded(state, spec.id, level):
            result.skipped.append(spec.id)
            continue
        if limit is not None and processed >= limit:
            break
        repo = str(Path(spec.repo or manifest.defaults.repo).expanduser().resolve())
        if repo not in profiles:
            profiles[repo] = detect_repo(repo)
        row = mold_task(spec, manifest.defaults, out_dir, profiles[repo], level=level,
                        timestamp=timestamp, proposer=proposer, run_gate=run_gate)
        result.rows.append(row)
        state["tasks"][spec.id] = {"level": level, "status": row.status, "timestamp": timestamp}
        processed += 1
    (out_dir / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    if level == "full":
        emit_batch_manifest(manifest, out_dir, state)
    _log.info("mold.done", processed=processed, skipped=len(result.skipped),
              attention=len(result.attention))
    return result


def emit_batch_manifest(manifest: MoldManifest, out_dir: Path, state: dict) -> Path:
    """Write the ready-to-run `loopkit batch` manifest from every READY task (past or present run).

    This artifact is the human checkpoint: review the molded instances, then
    `loopkit batch --tasks <out>/batch.toml`. Config paths are relative to the manifest, so the
    whole molded directory travels as one reviewable unit. Tasks that aren't ready are listed in a
    comment block — visible, never silently dropped.
    """
    j = json.dumps
    ready_ids = {spec.id for spec in manifest.task
                 if (state["tasks"].get(spec.id) or {}).get("status") == READY}
    # A ready task whose `after` dependency is NOT ready can't be emitted either — the batch
    # manifest would refuse the dangling edge, and dropping the edge instead would silently run
    # the dependent without its base. Demote until stable (edges may chain).
    demoted: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for spec in manifest.task:
            if spec.id in ready_ids:
                missing = next((d for d in spec.after if d not in ready_ids), None)
                if missing is not None:
                    ready_ids.discard(spec.id)
                    demoted[spec.id] = f"waiting on '{missing}'"
                    changed = True
    ready = [s for s in manifest.task if s.id in ready_ids]
    pending = [(s, (state["tasks"].get(s.id) or {})) for s in manifest.task
               if s.id not in ready_ids]
    lines = ["# generated by loopkit mold-batch — review the molded instances, then run:",
             f"#   loopkit batch --tasks {out_dir / 'batch.toml'}",
             "", "[defaults]", f"provider = {j(manifest.defaults.provider)}"]
    for spec in ready:
        lines += ["", "[[task]]", f"id = {j(spec.id)}"]
        if spec.goal:
            lines.append(f"goal = {j(spec.goal)}")
        if spec.issue is not None:                        # no resolved goal ⇒ batch fetches the
            lines.append(f"issue = {spec.issue}")         # issue itself; either way the PR closes it
        lines.append(f"config = {j(f'{spec.id}/loopkit.toml')}")
        if spec.group:
            lines.append(f"group = {j(spec.group)}")
        if spec.after:
            lines.append(f"after = {j(spec.after)}")
        # Declared touches win; else the proposer's observed touches.txt (durable across resumes,
        # where a skipped task's proposer never re-ran to refill the in-memory spec).
        touches = spec.touches or _read_touches(out_dir / spec.id / "touches.txt")
        if touches:
            lines.append(f"touches = {j(touches)}")
    if pending:
        lines += ["", "# -- not ready (molding incomplete — see state.json / each task dir) --"]
        lines += [f"#   {spec.id}: {demoted.get(spec.id) or entry.get('status', 'unmolded')}"
                  for spec, entry in pending]
    path = out_dir / "batch.toml"
    path.write_text("\n".join(lines) + "\n")
    _log.info("mold.emit", ready=len(ready), pending=len(pending))
    return path
