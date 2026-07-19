"""The pre-flight Config — the whole manual as one object (Chapter 18).

A loopkit run is fully described by one declarative file (`loopkit.toml`). Every field maps
to a chapter: the agent (1-3), prompt anchors (4-5), the two gates (6-7, 9), the three hard
stops (13-14), the durable branch (15), and the safety envelope (16). Validation is
pydantic's job, so a typo in the file becomes a clear error up front instead of a confusing
failure twenty minutes into a run.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field, field_validator


class AgentConfig(BaseModel):
    """How to invoke the model (Ch 1-3) and the budget ceiling it runs under (Ch 14)."""

    adapter: str = "mock"                 # mock | claude-code | codex | claude-api | openai-api
    model: str | None = None              # provider default if None (e.g. claude-opus-4-8 for claude-api)
    max_cost_usd: float = Field(default=10.0, ge=0)
    max_tool_calls: int = Field(default=25, ge=1)   # per-tick tool-call cap for the API adapters
    args: list[str] = Field(default_factory=list)
    # claude-code billing: False (default) = the subscription (on-disk `claude` login / OAuth token),
    # with ANTHROPIC_API_KEY withheld so an ambient shell key can't silently bill the API. True (or
    # `run --api-key`) opts into the billed ANTHROPIC_API_KEY. No effect on the API adapters (always API).
    use_api_key: bool = False


class PromptConfig(BaseModel):
    """The anchor files reloaded into a fresh context every tick (Ch 4-5)."""

    anchors: list[str] = Field(default_factory=lambda: ["PROMPT.md"])


class PlanConfig(BaseModel):
    """Plan-driven backlog mode (shape #2): point one loop at a markdown checklist it grinds through,
    one item per tick. `file` is both a prompt anchor the agent maintains AND the loop's completion
    signal — the run is not DONE while any `- [ ]` item is open. None = off (single-task behavior)."""

    file: str | None = None               # e.g. "IMPLEMENTATION_PLAN.md"; None = plan mode off


class GateConfig(BaseModel):
    """The gates: the in-sample iteration gate, the held-out acceptance gate, and an optional
    held-out regression gate (the two-oracle pattern — see `acceptance`/`regression`)."""

    iteration: str                        # fast, in-sample — what the loop optimizes (Ch 6-7)
    acceptance: str | None = None         # held-out, run once before DONE (Ch 9)
    # The second oracle (SWE-bench's FAIL_TO_PASS + PASS_TO_PASS): `acceptance` proves the *target*
    # behavior now works; `regression` proves previously-passing behavior was *preserved*. A fix that
    # passes its target by breaking something else must fail. Optional and None-safe — None means the
    # acceptance gate alone certifies DONE (exact prior behavior).
    regression: str | None = None


class StopsConfig(BaseModel):
    """The hard stops that make the loop halt (Ch 13)."""

    max_iter: int = Field(default=30, ge=1)
    no_progress_after: int = Field(default=3, ge=1)
    # Plan-driven backlog only: halt if no checklist item completes for this many ticks (NoProgress
    # watches the git tree, which a churning plan-mode agent keeps changing — this watches the
    # done-count). Coarser than no_progress_after because a plan item legitimately spans several
    # ticks. Keep it < max_iter so it can fire before the cap. Ignored off plan mode.
    plan_stall_after: int = Field(default=6, ge=1)
    # Review only: halt after this many CONSECUTIVE fresh review rejections (an APPROVE resets).
    # A reject loop bills two model calls per tick (agent + judge) and NoProgress can't see it —
    # the agent keeps editing, so the signature keeps changing. Ignored when review is off.
    review_stall_after: int = Field(default=4, ge=1)


class SafetyConfig(BaseModel):
    """Blast-radius containment (Ch 16) and the protected-path guard (Ch 9 + 16)."""

    protected_paths: list[str] = Field(default_factory=list)
    require_clean_tree: bool = True
    allow_branches: list[str] = Field(default_factory=lambda: ["loopkit/*"])
    forbid_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    # >=2 → run the iteration gate N times on the initial tree at run start and refuse to start unless
    # every run agrees (a flaky gate corrupts the stop oracle, Ch 9). 0/1 = skip (default; exact prior
    # behavior). `run --check-gate N` overrides this per-invocation.
    gate_stability_runs: int = 0


class RemoteConfig(BaseModel):
    """Sync the loop's branch to a git remote after a run reaches DONE — opt-in, never `main`.

    The loop is always durable locally (commit every tick, Ch 15); this is the *outward* edge:
    push that branch to GitHub/GitLab and optionally open a PR/MR for a human to review. Every
    field defaults to the safe choice — `enabled=False` (nothing leaves your machine unless you
    ask), draft PRs (a human merges), and the loop's own branch only (the forbid_branches guard
    still applies, so a misconfigured run can't push to `main`).
    """

    enabled: bool = False                 # master switch — no push/PR happens unless this is True
    name: str = "origin"                  # the git remote to push to
    push: bool = True                     # push the loop branch when the run reaches DONE
    open_pr: bool = False                 # after pushing, open a PR/MR via gh/glab
    provider: str = "auto"                # auto (detect from remote URL) | github | gitlab
    pr_base: str = "main"                 # the base branch a PR/MR targets
    draft: bool = True                    # open the PR/MR as a draft (a human reviews + merges)


class TraceConfig(BaseModel):
    """LangSmith full-tree tracing (Ch 14-15 observability) — optional, auto-on by default.

    Traces capture the full human-readable input/output of every step, all tool use, and organized
    cost/usage/model metadata — the rich complement to the always-on payload-free logs. `langsmith`
    is an optional extra (`loopkit[trace]`); when `enabled` is None (the default), tracing turns on
    automatically iff `langsmith` is installed *and* a LangSmith API key (or `LANGSMITH_TRACING`) is
    in the environment. Set `enabled = true`/`false` to force it; nothing is captured when off.
    """

    enabled: bool | None = None           # None = auto (on iff langsmith + a LangSmith key present)
    project: str | None = None            # LangSmith project; falls back to env, then "loopkit"


class ReviewDecision(NamedTuple):
    """The resolved review outcome for ONE invocation: ``kind`` says WHAT runs (``"off"`` /
    ``"command"`` — a shell judge in ``command`` / ``"default"`` — the built-in judge,
    ``command`` is None), plus a short, human-readable ``reason``. The reason exists so every
    entry point can LOG *why* review is on or off — the decision was previously invisible, which
    is exactly how review once fired in zero of 28 batch runs behind a reassuring "default-on"
    banner.

    ``on`` derives from ``kind``, NOT from ``command`` — the built-in judge has no shell command,
    and deriving on-ness from ``command`` would render the default judge as "off" at every call
    site while it silently runs: precisely the invisible-decision bug class this type exists to
    kill. ``kind`` has no default: any construction site must say what it means, loudly."""

    command: str | None
    reason: str
    kind: str                              # "off" | "command" | "default"

    @property
    def on(self) -> bool:
        return self.kind != "off"


class ReviewConfig(BaseModel):
    """Continuous review (Ch 8): an adversarial command run after each *advancing* tick; a clean
    review (exit 0) is a precondition for DONE, and a failing review's output feeds back so the agent
    self-corrects (the fix→re-review loop).

    ``enabled`` (default True) is the master switch — set it false to turn review OFF everywhere.
    ``command`` is a custom judge shell command; leave it unset to run the **built-in default judge**
    (extensions/judge.py — see docs/default-judge-design.md), so review is truly on-by-default: a
    project that configures nothing still gets an adversarial review gating DONE. Precedence when
    enabled: an explicit override (``run --review`` / manifest ``review =``) wins, else ``command``,
    else the built-in judge. ``--no-review`` disables for a single invocation."""

    command: str | None = None            # the review/judge shell command; None = use the built-in judge (once wired)
    enabled: bool = True                  # master switch; false = review off everywhere
    # Built-in judge identity (extensions/judge.py). Unset = inherit from [agent]: backend from
    # `adapter`, model from `model` (same-vendor only — a cross-vendor backend override gets that
    # backend's own default), use_api_key tri-state (None = inherit). `criteria` layers project
    # rubric file(s) onto the bundled checklist; keep them under safety.protected_paths so the
    # author can't tune its own grader.
    backend: str | None = None            # judge backend override; None = [agent].adapter
    model: str | None = None              # judge model override; None = [agent].model (same vendor)
    args: list[str] = Field(default_factory=list)   # extra CLI flags for a CLI judge backend
    use_api_key: bool | None = None       # claude-code billing override; None = [agent].use_api_key
    criteria: list[str] = Field(default_factory=list)  # rubric files appended to the bundled prompt

    def decide(self, override: str | None = None, disabled: bool = False) -> ReviewDecision:
        """Resolve the effective review AND a reason. Precedence (unchanged from the original
        resolver): ``--no-review`` wins, then an explicit override (``--review`` / manifest ``review =``
        — deliberately strong enough to run even when ``enabled=false``), then the ``enabled`` gate,
        then the configured ``command``, and finally the **built-in default judge** — review is now
        truly on-by-default: nothing configured still reviews."""
        # Reasons carry no on/off prefix — callers render the state (see cli run-line / batch log),
        # so the reason states only the cause.
        if disabled:
            return ReviewDecision(None, "--no-review", "off")
        if override is not None:
            return ReviewDecision(override, "explicit override (--review / manifest review=)",
                                  "command")
        if not self.enabled:
            return ReviewDecision(None, "disabled ([review] enabled = false)", "off")
        if self.command is not None:
            return ReviewDecision(self.command, "[review] command", "command")
        return ReviewDecision(None, "built-in judge ([review] command unset — on by default)",
                              "default")

    def resolved(self, override: str | None = None, disabled: bool = False) -> str | None:
        """Back-compat thin wrapper returning just the shell command; prefer ``decide()``. NOTE:
        the built-in default judge has no shell command, so this returns None for it — check
        ``decide().on`` / ``decide().kind`` for on-ness, never this."""
        return self.decide(override=override, disabled=disabled).command


class Config(BaseModel):
    """The whole loop as one object. `goal` and `gate.iteration` are the only required fields."""

    goal: str
    repo: str = "."
    branch: str = "loopkit/run"
    agent: AgentConfig = Field(default_factory=AgentConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    gate: GateConfig
    stops: StopsConfig = Field(default_factory=StopsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    @field_validator("goal")
    @classmethod
    def _goal_nonempty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("goal must be a non-empty description of what 'done' means")
        return value

    def repo_path(self) -> Path:
        return Path(self.repo).expanduser().resolve()


def load_config(path: str | Path) -> Config:
    """Read and validate a `loopkit.toml` into a Config."""
    p = Path(path).expanduser()
    with p.open("rb") as handle:
        data = tomllib.load(handle)
    return Config.model_validate(data)
