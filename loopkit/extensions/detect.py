"""Deterministic repo introspection — the mechanical, safety-critical half of molding (Part IV, Layer 3).

Molding loopkit to a repo is a judgment task a Claude/Codex **copilot** already does well — *except*
for the parts where a guess is dangerous. A copilot that hallucinates the test command wastes a run;
one that hallucinates which paths are protected can let the loop weaken its own held-out gate or churn
a migration/lockfile it should never touch. Those are not judgment calls — they are readable off file
markers, deterministically, at zero tokens. `detect` reads them so neither a copilot nor an unattended
agent has to guess:

  - **test runner** → the `[gate].iteration` command: `pyproject.toml`/`pytest.ini`/`tox.ini`/
    `setup.cfg` → `python -m pytest -q`; a real `package.json` `scripts.test` → `<pm> test`;
    `go.mod` → `go test ./...`; `Cargo.toml` → `cargo test`; a `Makefile` with a `test:` target →
    `make test`. First present in that fixed order is the primary; the rest are recorded as
    alternatives so the molder can swap.
  - **protected-path candidates** → `[safety].protected_paths`: the test directory (so the loop can't
    weaken its own gate — the held-out invariant, Ch 9), CI config, chart/deploy dirs, migrations, and
    dependency lockfiles. Only paths that actually exist are proposed — evidence, not a guess.
  - **default branch** → augments `[safety].forbid_branches` (the loop must never push there): from
    `origin/HEAD`, else a local `main`/`master`, else the current HEAD.
  - **agent on PATH** → `[agent].adapter`: `claude` → `claude-code`, `codex` → `codex`.

`detect` **proposes, it does not decide.** It prints a `loopkit.toml` for the molder to refine (`--write`
is an opt-in for the quick no-copilot case, and never overwrites an existing config without `--force`).
It deliberately leaves the two things it cannot read from a marker — the **goal** (what "done" means)
and the **held-out acceptance oracle** (author it, then verify with `loopkit synth-gate`) — as annotated
placeholders. That line is the whole Part IV thesis: the copilot keeps the judgment; loopkit supplies
the determinism the judgment can't self-supply. A layer that's just "an LLM writes your X" belongs in
the `loopkit-mold` skill, not here.

Every detected fact carries its **evidence** (the marker that decided it) and a **confidence**, so the
proposal is auditable rather than opaque. The `RepoProfile` is JSON-serializable for the unattended
tier (an agent parses it) and re-comparable later.

Stdlib-only (`json`, `re`, `shutil`, `subprocess`, `tomllib`, `dataclasses`, `pathlib`) — importing
this pulls no optional dependency, and the core keeps no runtime dependency on it (the `measure.py` /
`synth_gate.py` shape). It is the most standalone of the three primitives: pure introspection, no
executor, no fleet.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..log import get_logger

# Confidence levels, so callers/tests key off a stable string.
HIGH = "high"        # a specific, declared marker decided it (e.g. pytest.ini → pytest)
MEDIUM = "medium"    # a reasonable inference (a local branch stands in for the default)
LOW = "low"          # a weak fallback (the current HEAD as the "default" branch)
NONE = "none"        # nothing found — the molder must supply this

# The npm placeholder `scripts.test` (`npm init` writes it) — a test command that isn't one.
_NPM_PLACEHOLDER = "no test specified"

# Curated protected-path candidates, checked in order. Each is (path, category) — only paths that
# EXIST in the repo are proposed (evidence, not a guess). The test directory comes first because it is
# the one the held-out-gate invariant most needs guarded: a loop that can edit `tests/` can "pass" by
# weakening its own grader (Ch 9, verifier hacking). Directories get a trailing slash on emit.
_PROTECTED_CANDIDATES: list[tuple[str, str]] = [
    # test dirs — the gate's own files (only the first that exists is added; see _detect_protected)
    ("tests", "test directory — the loop must not weaken its own held-out gate (Ch 9)"),
    ("test", "test directory — the loop must not weaken its own held-out gate (Ch 9)"),
    ("spec", "test directory — the loop must not weaken its own held-out gate (Ch 9)"),
    # CI / release config — a run must not rewrite how it is built or shipped
    (".github/workflows", "CI config"),
    (".gitlab-ci.yml", "CI config"),
    (".circleci", "CI config"),
    ("Jenkinsfile", "CI config"),
    ("azure-pipelines.yml", "CI config"),
    # deploy / charts — blast-radius on the cluster
    ("charts", "deploy/chart config"),
    ("helm", "deploy/chart config"),
    ("deploy", "deploy/chart config"),
    ("kustomize", "deploy/chart config"),
    # data migrations — irreversible on real data
    ("migrations", "data migrations — irreversible on real data"),
    ("alembic", "data migrations — irreversible on real data"),
    ("db/migrate", "data migrations — irreversible on real data"),
    # dependency lockfiles — supply-chain / reproducibility
    ("poetry.lock", "dependency lockfile"),
    ("uv.lock", "dependency lockfile"),
    ("Pipfile.lock", "dependency lockfile"),
    ("package-lock.json", "dependency lockfile"),
    ("pnpm-lock.yaml", "dependency lockfile"),
    ("yarn.lock", "dependency lockfile"),
    ("Cargo.lock", "dependency lockfile"),
    ("go.sum", "dependency lockfile"),
    ("Gemfile.lock", "dependency lockfile"),
    ("composer.lock", "dependency lockfile"),
]

# The three test directory names, in preference order — only the first present is protected (a repo
# usually has one). Kept as a set for the "is this candidate a test dir?" check in _detect_protected.
_TEST_DIRS = ("tests", "test", "spec")


@dataclass
class Detection:
    """One deterministically-decided fact about the repo, with the evidence and confidence behind it.

    `key` groups facts for the table (`test-runner`, `adapter`, `default-branch`, `protected-path`, and
    `test-runner-alt` for a non-primary candidate). `evidence` names the marker that decided it — so the
    proposal is auditable, never "the tool just picked this". `value` is None when nothing was found.
    """

    key: str
    value: str | None
    evidence: str
    confidence: str        # HIGH | MEDIUM | LOW | NONE


@dataclass
class RepoProfile:
    """The full deterministic read of a repo → a proposed `loopkit.toml`.

    Every mechanical field is decided by a file marker (recorded in `detections`); the two judgment
    fields `detect` cannot read — the goal and the held-out acceptance oracle — are deliberately left
    for the molder (they surface as annotated placeholders in `to_toml`). JSON-serializable so the
    unattended tier can consume it.
    """

    root: str
    test_command: str | None            # → [gate].iteration; None = no runner detected
    protected_paths: list[str]          # → [safety].protected_paths (existing candidates only)
    default_branch: str | None          # → augments [safety].forbid_branches; also a good [remote].pr_base
    adapter: str                        # → [agent].adapter
    detections: list[Detection] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_toml(self) -> str:
        """Render a *proposed* `loopkit.toml` — the mechanical scaffold, with the judgment left annotated.

        Mirrors `_templates._CONFIG_TEMPLATE` (same commented, teach-as-you-go shape) so a molder reads
        a familiar file, but every detected value is injected with its evidence inline. The goal and the
        held-out acceptance oracle are left as guided placeholders — `detect` must not fake the two
        things it cannot read from a marker.
        """
        return _render_toml(self)


# --------------------------------------------------------------------------------------------
# The detectors — each pure over the filesystem / git, each returns Detection(s).
# --------------------------------------------------------------------------------------------
def _exists(root: Path, rel: str) -> bool:
    return (root / rel).exists()


def _package_manager(root: Path) -> str:
    """The node package manager to invoke `test` through, inferred from the lockfile present."""
    if _exists(root, "pnpm-lock.yaml"):
        return "pnpm"
    if _exists(root, "yarn.lock"):
        return "yarn"
    return "npm"


def _node_test_command(root: Path) -> str | None:
    """A real `scripts.test` in package.json → `<pm> test`; the npm placeholder → None (not a test)."""
    try:
        data = json.loads((root / "package.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    script = str((data.get("scripts") or {}).get("test") or "").strip()
    if not script or _NPM_PLACEHOLDER in script.lower():
        return None
    return f"{_package_manager(root)} test"


def _python_has_pytest(root: Path) -> str | None:
    """Any conventional Python test-config marker → the concrete pytest command."""
    # pyproject/setup.cfg count only when they actually configure pytest OR a tests dir exists — a
    # bare pyproject (metadata only) isn't evidence of a runnable test suite.
    if _exists(root, "pytest.ini") or _exists(root, "tox.ini"):
        return "pytest.ini/tox.ini"
    for cfg, marker in (("pyproject.toml", "tool.pytest"), ("setup.cfg", "tool:pytest")):
        if not _exists(root, cfg):
            continue
        try:
            text = (root / cfg).read_text()
        except OSError:
            continue
        if marker in text:
            return cfg
        # a pyproject/setup.cfg alongside a tests dir is still good evidence of a pytest project
        if any(_exists(root, d) for d in _TEST_DIRS):
            return cfg
    return None


def _makefile_test_target(root: Path) -> str | None:
    """A `test:` target in a Makefile → make is the declared test entry point."""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        if not _exists(root, name):
            continue
        try:
            text = (root / name).read_text()
        except OSError:
            continue
        # a target line begins at column 0 as `test:` (optionally with prereqs); recipe lines are TAB-led
        if re.search(r"(?m)^test[ \t]*:", text):
            return name
    return None


def _detect_test_runner(root: Path) -> list[Detection]:
    """All conventional test commands the markers support, primary first (fixed priority order).

    The order is a deliberate call: Python's markers give the most concrete command (`python -m pytest`),
    a declared `scripts.test`/`Makefile test:` is the author's own word, and `go`/`cargo` are unambiguous
    conventions. First present wins as `test-runner`; the rest become `test-runner-alt` so a molder can
    swap without re-deriving. Determinism matters more than getting the priority perfect — the molder
    reviews it.
    """
    candidates: list[tuple[str, str]] = []   # (command, evidence)
    py_marker = _python_has_pytest(root)
    if py_marker:
        candidates.append(("python -m pytest -q", py_marker))
    node_cmd = _node_test_command(root)
    if node_cmd:
        candidates.append((node_cmd, "package.json scripts.test"))
    if _exists(root, "go.mod"):
        candidates.append(("go test ./...", "go.mod"))
    if _exists(root, "Cargo.toml"):
        candidates.append(("cargo test", "Cargo.toml"))
    mk = _makefile_test_target(root)
    if mk:
        candidates.append(("make test", f"{mk}: test target"))

    if not candidates:
        return [Detection("test-runner", None, "no pytest/npm/go/cargo/make test marker found", NONE)]
    detections = [Detection("test-runner", candidates[0][0], candidates[0][1], HIGH)]
    for cmd, ev in candidates[1:]:
        detections.append(Detection("test-runner-alt", cmd, ev, MEDIUM))
    return detections


def _detect_adapter(which=shutil.which) -> Detection:
    """Which coding-agent binary is on PATH → the adapter. `which` is injectable for tests."""
    claude = which("claude")
    if claude:
        return Detection("adapter", "claude-code", f"claude on PATH ({claude})", HIGH)
    codex = which("codex")
    if codex:
        return Detection("adapter", "codex", f"codex on PATH ({codex})", HIGH)
    # Nothing on PATH — default to the recommended adapter but say so; the molder installs or overrides.
    return Detection("adapter", "claude-code",
                     "no claude/codex on PATH — defaulting to claude-code (install it or set [agent].adapter)",
                     LOW)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # `git -C <root>` rather than cwd juggling — deterministic target, no directory side effects.
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _detect_default_branch(root: Path) -> Detection:
    """The repo's default branch — the one the loop must never push to (→ forbid_branches, pr_base)."""
    # 1. the remote's declared default (origin/HEAD -> origin/<branch>): the authoritative answer.
    head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        branch = head.stdout.strip().split("/", 1)[-1]      # origin/main -> main
        return Detection("default-branch", branch, "origin/HEAD", HIGH)
    # 2. a conventional local branch stands in when there's no remote.
    for cand in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}").returncode == 0:
            return Detection("default-branch", cand, f"local branch {cand}", MEDIUM)
    # 3. the current checkout, as a weak last resort (skip a detached HEAD).
    cur = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if cur.returncode == 0 and cur.stdout.strip() and cur.stdout.strip() != "HEAD":
        return Detection("default-branch", cur.stdout.strip(), "current HEAD", LOW)
    return Detection("default-branch", None, "not a git repo / no branches", NONE)


def _detect_protected(root: Path) -> list[Detection]:
    """Existing protected-path candidates, in the curated order — one Detection each (evidence-backed).

    Only the *first* test directory that exists is protected (a repo has one); every other candidate is
    added if it exists. A path that doesn't exist is not proposed — a protected path for a nonexistent
    dir is noise.
    """
    detections: list[Detection] = []
    test_dir_added = False
    for rel, category in _PROTECTED_CANDIDATES:
        if not _exists(root, rel):
            continue
        if rel in _TEST_DIRS:
            if test_dir_added:                              # already protected one test dir; skip others
                continue
            test_dir_added = True
        is_dir = (root / rel).is_dir()
        value = f"{rel}/" if is_dir else rel                # dirs get a trailing slash (the guard's idiom)
        detections.append(Detection("protected-path", value, category, HIGH))
    return detections


def detect_repo(root: str | Path, *, which=shutil.which) -> RepoProfile:
    """Introspect `root` deterministically → a `RepoProfile` (a proposed `loopkit.toml` + the audit trail).

    Pure over the filesystem + git; `which` is injectable so a test can pin adapter detection without
    touching the real PATH. Every returned fact carries its evidence — nothing here guesses.
    """
    log = get_logger("detect")
    root = Path(root).expanduser().resolve()

    runner = _detect_test_runner(root)
    adapter = _detect_adapter(which)
    branch = _detect_default_branch(root)
    protected = _detect_protected(root)

    detections = [*runner, adapter, branch, *protected]
    profile = RepoProfile(
        root=str(root),
        test_command=runner[0].value,                       # the primary; alts live in detections
        protected_paths=[d.value for d in protected if d.value],
        default_branch=branch.value,
        adapter=adapter.value or "claude-code",
        detections=detections)
    log.info("detect.done", root=str(root), testRunner=profile.test_command,
             adapter=profile.adapter, defaultBranch=profile.default_branch,
             protected=len(profile.protected_paths))
    return profile


# --------------------------------------------------------------------------------------------
# TOML emission — hand-formatted (mirrors _CONFIG_TEMPLATE) so the proposal keeps the friendly comments.
# --------------------------------------------------------------------------------------------
def _toml_str(value: str) -> str:
    """A TOML basic string — the detected values are simple, but escape the two chars that could break it."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_str_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _render_toml(p: RepoProfile) -> str:
    # forbid_branches: the safe defaults, plus the detected default branch if it's something else
    # (e.g. `develop`/`trunk`) — the loop must never push to whatever this repo actually ships from.
    forbid = ["main", "master"]
    if p.default_branch and p.default_branch not in forbid:
        forbid.append(p.default_branch)

    if p.test_command:
        iter_line = f"iteration = {_toml_str(p.test_command)}"
    else:
        iter_line = ('iteration = "<your test command>"   # detect found no test runner — set this '
                     "(Ch 6-7)")

    protected_note = "existing candidates — trim to the task (Ch 9 + 16)"
    if p.protected_paths:
        protected_line = f"protected_paths = {_toml_str_list(p.protected_paths)}"
    else:
        protected_line = 'protected_paths = ["tests/"]'
        protected_note = "detect found none — protect at least the gate's own test files (Ch 9)"

    adapter_ev = next((d.evidence for d in p.detections if d.key == "adapter"), "")

    return f"""\
# loopkit.toml — PROPOSED by `loopkit detect` (deterministic introspection). Review before running.
# detect reads the mechanical, safety-critical config off file markers. It deliberately leaves the two
# things no marker can tell it — fill these in:
#   * goal        — what "done" means for this work (below).
#   * acceptance  — the held-out oracle. Author it, then prove it real: `loopkit synth-gate`.
# Validate the finished file with `loopkit doctor`.
goal = "Describe exactly what 'done' means — the condition the loop drives toward."
repo = "."
branch = "loopkit/run"           # never main/master (Ch 16)

[agent]
adapter = {_toml_str(p.adapter)}          # detected: {adapter_ev}
max_cost_usd = 5.0               # budget ceiling (Ch 14) — bites on real cost (see `doctor`)

[prompt]
anchors = ["PROMPT.md"]          # fixed context reloaded each tick (Ch 4-5)

[gate]
{iter_line}
# acceptance = "<held-out oracle>"   # AUTHOR THIS: a DIFFERENT check the loop never sees (Ch 9). Then
#                                    # verify it: `loopkit synth-gate '<oracle>' --fix <ref>` (exit 0 = real)

[stops]
max_iter = 20                    # Ch 13
no_progress_after = 3

[safety]
{protected_line}  # {protected_note}
require_clean_tree = true
allow_branches = ["loopkit/*"]
forbid_branches = {_toml_str_list(forbid)}

# [remote]                       # opt-in OUTWARD edge (Ch 16): at DONE, push the branch + open a draft PR.
# enabled = true                 # OFF by default — nothing leaves your machine. Needs gh/glab authed.
# open_pr = true                 # one-run alternative (no block): `loopkit run --open-pr`
"""
