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

from pydantic import BaseModel, Field, field_validator


class AgentConfig(BaseModel):
    """How to invoke the model (Ch 1-3) and the budget ceiling it runs under (Ch 14)."""

    adapter: str = "mock"                 # mock | claude-code | codex
    model: str | None = None
    max_cost_usd: float = Field(default=10.0, ge=0)
    args: list[str] = Field(default_factory=list)


class PromptConfig(BaseModel):
    """The anchor files reloaded into a fresh context every tick (Ch 4-5)."""

    anchors: list[str] = Field(default_factory=lambda: ["PROMPT.md"])


class GateConfig(BaseModel):
    """The two gates: the in-sample iteration gate and the held-out acceptance gate."""

    iteration: str                        # fast, in-sample — what the loop optimizes (Ch 6-7)
    acceptance: str | None = None         # held-out, run once before DONE (Ch 9)


class StopsConfig(BaseModel):
    """The hard stops that make the loop halt (Ch 13)."""

    max_iter: int = Field(default=30, ge=1)
    no_progress_after: int = Field(default=3, ge=1)


class SafetyConfig(BaseModel):
    """Blast-radius containment (Ch 16) and the protected-path guard (Ch 9 + 16)."""

    protected_paths: list[str] = Field(default_factory=list)
    require_clean_tree: bool = True
    allow_branches: list[str] = Field(default_factory=lambda: ["loopkit/*"])
    forbid_branches: list[str] = Field(default_factory=lambda: ["main", "master"])


class Config(BaseModel):
    """The whole loop as one object. `goal` and `gate.iteration` are the only required fields."""

    goal: str
    repo: str = "."
    branch: str = "loopkit/run"
    agent: AgentConfig = Field(default_factory=AgentConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    gate: GateConfig
    stops: StopsConfig = Field(default_factory=StopsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

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
