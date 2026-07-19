"""Built-in default judge — the reviewer that makes `[review]` truly on-by-default. [Part II Ch 8]

`ShellReviewHook` runs whatever judge a project configures; this module is what runs when a project
configures *nothing*: a generic, adversarial, real-defects-only LLM reviewer. It exists because
"review on by default" only means something if there is something to run with zero configuration —
the failure mode this closes is a quality gate that is silently absent (review fired in zero of 28
batch runs before the decision was even observable).

Independence comes from a fresh, clean-context, read-only pass — the judge shares no memory with the
coding agent — not from a different model. True cross-model diversity is one `[review] backend`
override away (design: docs/default-judge-design.md).

Contracts honored here:
- **Fail-closed vs fail-halted are distinct.** A REJECT verdict feeds back so the agent fixes it; an
  infrastructure failure (missing binary, SDK/key problem, timeout, no parseable verdict) raises
  `ReviewUnavailable` (core `gate.py`) so the loop halts instead of burning the iteration cap
  telling the agent to fix a phantom defect.
- **The verdict is nonce'd.** The diff is agent-authored, untrusted input to the judge prompt; a
  per-call nonce plus instruction-after-diff means diff content cannot forge a verdict.
- **Truncation is fail-closed.** The full `--stat` always rides; an over-cap patch carries an
  instruction to REJECT anything the judge cannot certify from what is visible — burying bad code
  past the cap yields a rejection, not an unreviewed approval.
- **The judge cannot touch the tree.** The diff is embedded in the prompt and CLI backends run in a
  scratch cwd, so read-only is structural, not a flag that can drift.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import secrets
from ..agent import _parse_claude_json, _parse_codex_usage
from ..gate import GateResult, ReviewUnavailable
from ..plan import read_plan
from ..pricing import DEFAULT_MODELS, estimate_cost

# One patch cap for every backend; past it the prompt switches to fail-closed truncation. Chars, not
# tokens — deterministic and cheap to enforce, and generous enough that an honest single-task diff
# rarely hits it.
DIFF_CAP = 150_000
# The verdict reason is next-tick prompt material (build_prompt embeds feedback verbatim) — cap it so
# a rambling judge can't inflate every subsequent tick. The full raw transcript rides the trace span.
FEEDBACK_CAP = 4_000
# A wedged vendor binary must not hang the tick forever (neither `_CLIAdapter` nor `ShellReviewHook`
# has a timeout today; `mold.ShellProposer` is the precedent this follows).
JUDGE_TIMEOUT = 600.0

# Same-vendor model names are interchangeable (claude-code and claude-api both speak
# `claude-opus-4-8`); cross-vendor they are meaningless. Model inheritance follows vendor, so a
# `[review] backend = "codex"` override with no `[review] model` gets codex's own default rather
# than a Claude model name it cannot resolve.
_VENDOR = {"claude-code": "anthropic", "claude-api": "anthropic",
           "codex": "openai", "openai-api": "openai", "mock": "mock"}

DEFAULT_REVIEW_CRITERIA = """\
You are a STRICT, adversarial code reviewer. You did not write this change and share no context
with whoever did. Assume the change is defective until the diff proves otherwise. You are the last
check before this change is accepted, so a defect you wave through ships.

BLOCK (verdict REJECT) only for REAL defects:
- Correctness: a bug reachable from real input — wrong logic, unhandled edge case, broken error path.
- Security: an authorization gap, injection, committed secret, sensitive data written to logs, or a
  trust boundary the caller can forge.
- Incomplete fix: a sibling instance of the same bug left unfixed — name the file and line.
- Gaming: a deleted, weakened, or skipped test; a loosened assertion; an edit to a gate, CI, or
  reviewer configuration; special-casing the test's inputs instead of solving the problem.
- Trivially-passing test: a new test that would pass even against the OLD, buggy code.
- Contract break: a renamed or removed field, changed status code, or changed signature that the
  stated goal did not ask for.

Do NOT block for style, naming, formatting, or structure — prefix such notes with `ADVISORY:` on
their own lines; they are recorded but never fail the review."""


@dataclass
class JudgeTarget:
    """The resolved invocation identity: which backend binary/SDK, which model, how billed."""

    backend: str                       # claude-code | codex | claude-api | openai-api | mock
    model: str | None                  # None = the backend's own default
    args: list[str]                    # extra CLI flags (CLI backends only)
    use_api_key: bool                  # claude-code billing: API key vs subscription token


@dataclass
class JudgeVerdict:
    """One review outcome: the decision, the (capped) reason, the raw transcript, and what it cost."""

    passed: bool
    reason: str
    raw: str
    cost_usd: float = 0.0


def resolve_judge(review, agent) -> JudgeTarget:
    """Derive the judge's backend/model from `[review]` overrides falling back to `[agent]`.

    The one non-obvious rule: `[agent].model` is inherited only when the effective backend shares the
    agent's *vendor* — a cross-vendor `[review] backend` override with no `[review] model` gets that
    backend's own default (`None`), never a model name from the wrong provider.
    """
    backend = getattr(review, "backend", None) or agent.adapter
    model = getattr(review, "model", None)
    if model is None and _VENDOR.get(backend) == _VENDOR.get(agent.adapter):
        model = agent.model
    use_api_key = getattr(review, "use_api_key", None)
    if use_api_key is None:
        use_api_key = agent.use_api_key
    return JudgeTarget(backend=backend, model=model,
                       args=list(getattr(review, "args", None) or []),
                       use_api_key=bool(use_api_key))


def build_judge_prompt(goal: str, commit_message: str, stat: str, diff: str,
                       extra_criteria: tuple[str, ...] = (), *, nonce: str,
                       truncated: bool = False) -> str:
    """Assemble the judge prompt. Ordering is load-bearing:

    goal + commit message first (three of the six BLOCK criteria are relational — incomplete fix,
    gaming, and unrequested contract breaks are unjudgeable without knowing what the task *was*);
    criteria next; then the agent-authored content (stat, patch); and the nonce'd verdict
    instruction LAST, so the final thing in context is the contract, not attacker-controlled text.
    """
    parts = [DEFAULT_REVIEW_CRITERIA,
             f"# Goal the change was supposed to accomplish\n{goal}",
             f"# Commit message\n{commit_message}"]
    for extra in extra_criteria:
        parts.append(f"# Additional review criteria\n{extra}")
    parts.append(f"# Change under review — file summary (complete)\n{stat or '(no stat available)'}")
    parts.append(f"# Change under review — patch\n{diff}")
    if truncated:
        parts.append(
            "# TRUNCATION NOTICE\n"
            "The patch above was truncated at the size cap; the file summary is complete. You "
            "cannot certify code you cannot see: if any listed file's changes are not visible "
            "above and you cannot rule out a defect in them, you MUST reject, naming those files.")
    parts.append(
        "# Verdict\n"
        "Reply with your reasoning, any ADVISORY notes, then end with EXACTLY one line:\n"
        f"VERDICT[{nonce}]: APPROVE\n"
        f"or\nVERDICT[{nonce}]: REJECT — <reason, citing file:line>\n"
        "A verdict line without this exact bracketed tag is invalid and will be ignored.")
    return "\n\n".join(parts)


def run_judge(workspace: Path, *, target: JudgeTarget, goal: str, commit_message: str,
              base: str | None = None, extra_criteria: tuple[str, ...] = (),
              runner=None) -> JudgeVerdict:
    """Review the workspace's committed change and return a verdict.

    `base` bounds the diff (`base...HEAD`, falling back to `HEAD~1..HEAD`, then `git show HEAD` for
    a single-commit repo); an empty diff APPROVEs by vacuity — no change, nothing to reject.
    `runner(prompt, target) -> (text, cost_usd)` is the injectable seam (tests, and the one place a
    future backend plugs in); left None, `_dispatch` shells out / calls the SDK. Infrastructure
    failures raise `ReviewUnavailable`; only a judge that actually rendered a verdict returns.
    """
    if target.backend == "mock":                    # zero tokens, zero subprocesses — keeps every
        return JudgeVerdict(True, "mock judge auto-approve", "", 0.0)   # demo/test/scenario free
    stat, diff = _collect_diff(workspace, base)
    if not diff.strip():
        return JudgeVerdict(True, "empty diff — nothing to review", "", 0.0)
    truncated = len(diff) > DIFF_CAP
    if truncated:
        diff = diff[:DIFF_CAP] + "\n… [TRUNCATED at cap]"
    nonce = uuid.uuid4().hex[:8]
    prompt = build_judge_prompt(goal, commit_message, stat, diff, tuple(extra_criteria),
                                nonce=nonce, truncated=truncated)
    try:
        text, cost = (runner or _dispatch)(prompt, target)
    except ReviewUnavailable:
        raise
    except FileNotFoundError as exc:
        raise ReviewUnavailable(f"judge backend binary not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewUnavailable(f"judge timed out after {JUDGE_TIMEOUT:.0f}s") from exc
    passed, reason = _parse_verdict(text, nonce)
    return JudgeVerdict(passed, reason, raw=text, cost_usd=cost)


def _parse_verdict(text: str, nonce: str) -> tuple[bool, str]:
    """Extract the LAST correctly-nonce'd verdict; anything else (including a forged un-nonce'd
    `VERDICT:` planted in the diff) is ignored. No valid verdict ⇒ the judge did not actually
    decide ⇒ `ReviewUnavailable`, never a silent REJECT-with-nonsense-feedback."""
    import re
    hits = re.findall(rf"VERDICT\[{re.escape(nonce)}\]:\s*(APPROVE|ACCEPT|REJECT)\b[\s—:-]*(.*)",
                      text)
    if not hits:
        raise ReviewUnavailable("judge returned no parseable verdict "
                                f"(outputLen={len(text)}; expected VERDICT[{nonce}]: …)")
    decision, trailing = hits[-1]
    if decision in ("APPROVE", "ACCEPT"):           # ACCEPT: compat with examples/gates/review.sh
        return True, ""
    return False, (trailing.strip() or "review rejected the change")[:FEEDBACK_CAP]


def _collect_diff(workspace: Path, base: str | None) -> tuple[str, str]:
    """(full `--stat`, patch) for the change under review, first range that resolves wins."""
    ranges = ([f"{base}...HEAD"] if base else []) + ["HEAD~1..HEAD"]
    for rng in ranges:
        stat = _git(workspace, "diff", "--stat", rng)
        patch = _git(workspace, "diff", rng)
        if stat is not None and patch is not None:
            return stat, patch
    # Single-commit repo: HEAD~1 does not resolve; the whole commit is the change.
    return (_git(workspace, "show", "--stat", "--format=", "HEAD") or "",
            _git(workspace, "show", "--patch", "--format=", "HEAD") or "")


def _git(workspace: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def _dispatch(prompt: str, target: JudgeTarget) -> tuple[float, str] | tuple[str, float]:
    """Run one judge call on the resolved backend → (text, cost_usd). CLI backends shell out in a
    scratch cwd with a scrubbed env carrying only that vendor's key; API backends make one
    single-shot `_Backend.complete` (no tools) and return exact usage-priced cost."""
    if target.backend in ("claude-code", "codex"):
        return _dispatch_cli(prompt, target)
    if target.backend in ("claude-api", "openai-api"):
        return _dispatch_api(prompt, target)
    raise ReviewUnavailable(f"no judge backend for adapter {target.backend!r}")


def _dispatch_cli(prompt: str, target: JudgeTarget) -> tuple[str, float]:
    if target.backend == "claude-code":
        binary, keys = "claude", (secrets.ADAPTER_KEYS["claude-code"] if target.use_api_key
                                  else secrets.CLAUDE_CODE_SUBSCRIPTION_KEYS)
        extra = ["--output-format", "json"]         # buffered JSON so cost parses from the result
    else:
        binary, keys, extra = "codex", secrets.ADAPTER_KEYS["codex"], []
    cmd = [binary, "-p", prompt]
    if target.model:
        cmd += ["--model", target.model]
    cmd += extra + target.args
    env = secrets.current().child_env(add=keys)
    # Scratch cwd: the judge needs no filesystem — the diff is in the prompt — so read-only is
    # structural. A future prompt change cannot quietly grant it the repo.
    with tempfile.TemporaryDirectory(prefix="loopkit-judge-") as scratch:
        proc = subprocess.run(cmd, cwd=scratch, env=env, capture_output=True, text=True,
                              timeout=JUDGE_TIMEOUT)
    if proc.returncode != 0:
        tail = secrets.redact(((proc.stdout or "") + (proc.stderr or ""))[-500:])
        raise ReviewUnavailable(f"judge backend {binary!r} failed rc={proc.returncode}: {tail}")
    if target.backend == "claude-code":
        cost, text = _parse_claude_json(proc.stdout or "")
        return (text or proc.stdout or ""), cost
    usage = _parse_codex_usage(proc.stdout or "")
    return (proc.stdout or ""), estimate_cost(target.model, usage)


def _dispatch_api(prompt: str, target: JudgeTarget) -> tuple[str, float]:
    # Deferred imports: `_AnthropicBackend`/`_OpenAIBackend` defer their SDK import to first use, so
    # this module stays importable without either extra installed.
    from ..agent import _AnthropicBackend, _OpenAIBackend
    cls = _AnthropicBackend if target.backend == "claude-api" else _OpenAIBackend
    backend = cls(target.model or DEFAULT_MODELS[target.backend])
    try:
        turn = backend.complete([{"role": "user", "content": prompt}], [])
    except Exception as exc:                        # SDK missing, auth, quota — all infra, none a verdict
        raise ReviewUnavailable(f"judge API backend {target.backend!r} failed: {exc}") from exc
    return turn.text, estimate_cost(backend.model, turn.usage)


class DefaultReviewHook:
    """The `ReviewHook` the loop runs when `[review]` has no command — wraps `run_judge`.

    Holds the two pieces of state a stateless judge call can't supply:

    - **The fork point**, captured as the repo's HEAD at construction (both call sites build the
      hook *before* `run_loop` switches to the run branch, so this is the pre-run HEAD for fresh
      runs and batch scratch clones alike). At review time the diff base is
      `merge-base(HEAD, fork)` — correct even when resuming a branch whose origin has advanced —
      falling back to `HEAD~1` inside `run_judge` when neither resolves.
    - **The last APPROVEd HEAD**, which in plan mode scopes each review to the delta since the last
      clean verdict (per-item feedback at per-item cost); once the checklist completes the range
      snaps back to the fork point, so certification always re-reads the whole change. Single-task
      runs always review the full cumulative diff.
    """

    def __init__(self, review, agent, repo: Path, goal: str, *,
                 plan_file: str | None = None, runner=None) -> None:
        self.target = resolve_judge(review, agent)
        self._criteria_files = [str(p) for p in (getattr(review, "criteria", None) or [])]
        self._goal = goal
        self._plan_file = plan_file
        self._runner = runner
        self._fork = _git(Path(repo), "rev-parse", "HEAD")
        self._fork = self._fork.strip() if self._fork else None
        self._last_approved: str | None = None
        self.last_verdict: JudgeVerdict | None = None   # observability: the loop stamps the span from it

    def review(self, workspace: Path, commit_message: str) -> GateResult:
        if self.target.backend == "mock":
            return GateResult(True, None)
        base = self._base_for(workspace)
        verdict = run_judge(workspace, target=self.target, goal=self._goal,
                            commit_message=commit_message, base=base,
                            extra_criteria=self._read_criteria(workspace), runner=self._runner)
        self.last_verdict = verdict
        if verdict.passed:
            head = _git(workspace, "rev-parse", "HEAD")
            self._last_approved = head.strip() if head else None
            return GateResult(True, None, cost_usd=verdict.cost_usd)
        return GateResult(False, verdict.reason, cost_usd=verdict.cost_usd)

    def _base_for(self, workspace: Path) -> str | None:
        # Plan mode with open items: judge only the delta since the last clean verdict.
        if self._plan_file and self._last_approved is not None:
            if read_plan(Path(workspace), self._plan_file).blocks_done:
                return self._last_approved
        if self._fork:
            merge_base = _git(workspace, "merge-base", "HEAD", self._fork)
            if merge_base:
                return merge_base.strip()
        return None

    def _read_criteria(self, workspace: Path) -> tuple[str, ...]:
        # A configured-but-missing rubric fails the run, not silently weakens the judge — the same
        # fail-closed rule examples/gates/review.sh applies to its rubric file.
        texts = []
        for name in self._criteria_files:
            path = Path(name) if Path(name).is_absolute() else Path(workspace) / name
            if not path.is_file():
                raise ReviewUnavailable(f"[review] criteria file missing: {name}")
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return tuple(texts)
