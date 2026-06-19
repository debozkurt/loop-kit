"""Skills and the write-back flywheel (Chapter 17). [Part II]

The flywheel: a successful run is distilled into a named **skill** — a reusable lesson — which
is rendered back into the prompt of future runs. Past runs make future runs better, and the
gains compound. Two attach points in the core: `prompt.build_prompt` renders the registry into
each tick's prompt (the read edge), and `run_loop`'s DONE path calls `write_back` (the write
edge).

The load-bearing rule is **gated, never ungated** (Ch 17/19). Reaching DONE means a run passed
the held-out acceptance gate — good enough to *accept*. It is not automatically good enough to
*learn from*: a barely-passing or narrowly-scoped run can distill into a misleading skill that
then poisons every future prompt. So write-back runs through its own gate; only a run that
clears it mints a skill. Without that guard the flywheel accelerates the accumulation of junk.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..gate import Gate
from ..log import get_logger

_HEADER = "# Skills (learned from past runs — apply when relevant)"


@dataclass
class Skill:
    """A named, reusable lesson distilled from a successful run."""

    name: str
    guidance: str                  # the instruction injected into future prompts
    source_goal: str = ""          # provenance: the goal whose run produced it

    def render(self) -> str:
        return f"## {self.name}\n{self.guidance}"


# A distiller turns a finished run into a candidate skill (or None when there's nothing worth
# keeping). Deterministic default below; a real one would ask the agent to summarise *how* it
# solved the goal, which is why it gets the run result, the workspace, and the goal.
Distiller = Callable[["object", Path, str], "Skill | None"]


def _default_distiller(run_result: object, workspace: Path, goal: str) -> "Skill | None":
    """Minimal distillation: name a skill after the goal and record that it was solved.

    Captures provenance only — enough for the flywheel's mechanics. Pass a custom `distill` to
    capture real guidance (e.g. the agent's own summary of the approach it found).
    """
    if not goal.strip():
        return None
    slug = "-".join(goal.lower().split()[:4]) or "run"
    return Skill(name=f"skill-{slug}", guidance=f"A previous run solved: {goal}", source_goal=goal)


class SkillRegistry(Protocol):
    def render(self) -> str: ...
    def write_back(self, run_result: object, workspace: Path, goal: str = "") -> "Skill | None": ...


class _BaseRegistry:
    """Shared write-back policy: gate first, distill, then dedupe by name. Storage is the subclass."""

    def __init__(self, *, write_back_gate: Gate | None, distill: Distiller | None) -> None:
        self._gate = write_back_gate
        self._distill = distill or _default_distiller
        self._log = get_logger("skills")

    def _vet(self, run_result: object, workspace: Path, goal: str) -> "Skill | None":
        # Gated, never ungated: a run can be acceptable yet unfit to learn from.
        if self._gate is not None and not self._gate.check(workspace).passed:
            self._log.info("write_back.gated_out", reason="write_back_gate_failed", goalLen=len(goal))
            return None
        skill = self._distill(run_result, workspace, goal)
        if skill is None:
            self._log.info("write_back.nothing_to_distill", goalLen=len(goal))
        return skill


class InMemorySkillRegistry(_BaseRegistry):
    """Skills held in memory — the registry for tests, demos, and a single multi-run session."""

    def __init__(self, skills: "list[Skill] | None" = None, *, write_back_gate: Gate | None = None,
                 distill: Distiller | None = None) -> None:
        super().__init__(write_back_gate=write_back_gate, distill=distill)
        self.skills: list[Skill] = list(skills or [])

    def render(self) -> str:
        if not self.skills:
            return ""
        return _HEADER + "\n" + "\n\n".join(s.render() for s in self.skills)

    def write_back(self, run_result: object, workspace: Path, goal: str = "") -> "Skill | None":
        skill = self._vet(run_result, workspace, goal)
        if skill is None:
            return None
        if any(s.name == skill.name for s in self.skills):
            self._log.debug("write_back.dup", name=skill.name)   # idempotent: don't relearn
            return None
        self.skills.append(skill)
        self._log.info("write_back.minted", name=skill.name, total=len(self.skills))
        return skill


class FileSkillRegistry(_BaseRegistry):
    """Skills persisted as one markdown file each — the durable flywheel across runs/processes.

    State lives on disk, not in memory, so learning accumulates the same way durability keeps
    run state in git: a new process pointed at the same directory inherits every prior lesson.
    """

    def __init__(self, directory: "str | Path", *, write_back_gate: Gate | None = None,
                 distill: Distiller | None = None) -> None:
        super().__init__(write_back_gate=write_back_gate, distill=distill)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def render(self) -> str:
        files = sorted(self.directory.glob("*.md"))
        if not files:
            return ""
        body = "\n\n".join(f.read_text(encoding="utf-8", errors="replace").rstrip() for f in files)
        return _HEADER + "\n" + body

    def write_back(self, run_result: object, workspace: Path, goal: str = "") -> "Skill | None":
        skill = self._vet(run_result, workspace, goal)
        if skill is None:
            return None
        path = self.directory / f"{skill.name}.md"
        if path.exists():
            self._log.debug("write_back.dup", name=skill.name)
            return None
        path.write_text(skill.render() + "\n", encoding="utf-8")
        self._log.info("write_back.minted", name=skill.name, path=str(path))
        return skill


def default_registry() -> InMemorySkillRegistry:
    """An empty in-memory registry — the no-skills starting point of the flywheel."""
    return InMemorySkillRegistry()
