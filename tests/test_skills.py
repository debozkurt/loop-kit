"""Skills + write-back flywheel tests (Ch 17): rendered skills reach the agent; write-back is gated.

The two halves of the flywheel under test. Read: a skill in the registry shows up in the prompt
and actually changes what the agent does. Write: a DONE run mints a skill *only* through the
write-back gate — an ungated or failed run learns nothing. MockAgent + CallableGate keep it
deterministic and token-free.
"""
from __future__ import annotations

from pathlib import Path

from loopkit.agent import AgentResult, MockAgent
from loopkit.config import Config, GateConfig, StopsConfig
from loopkit.extensions.skills import (
    FileSkillRegistry,
    InMemorySkillRegistry,
    Skill,
)
from loopkit.gate import CallableGate
from loopkit.loop import RunResult, run_loop
from loopkit.prompt import build_prompt
from loopkit.stops import StopReason


def _config(repo: Path, **overrides) -> Config:
    base = dict(goal="make it pass", repo=str(repo), branch="loopkit/test",
                gate=GateConfig(iteration="true"))
    base.update(overrides)
    return Config(**base)


def _done_result() -> RunResult:
    return RunResult(StopReason.DONE, 1, 0.0)


# --- render / read side --------------------------------------------------------------------

def test_empty_registry_renders_nothing():
    assert InMemorySkillRegistry().render() == ""


def test_registry_renders_skills_with_header():
    reg = InMemorySkillRegistry([Skill(name="use-x", guidance="prefer X over Y")])
    rendered = reg.render()
    assert "use-x" in rendered and "prefer X over Y" in rendered
    assert rendered.startswith("# Skills")


def test_build_prompt_includes_rendered_skills(git_repo: Path):
    reg = InMemorySkillRegistry([Skill(name="magic", guidance="the answer is MAGIC")])
    prompt = build_prompt(_config(git_repo), feedback=None, skills=reg.render())
    assert "MAGIC" in prompt


def test_rendered_skill_changes_agent_behaviour(git_repo: Path):
    # An agent that only solves when its prompt carries the skill marker. Without the skill it
    # never writes the file (no progress); with it rendered, the marker reaches the prompt and
    # the run completes. Proves the read edge is wired end to end through run_loop.
    class PromptAwareAgent:
        def act(self, prompt: str, workspace: Path, *, observer=None) -> AgentResult:
            if "MAGIC" in prompt:
                (workspace / "solution.txt").write_text("done")
            return AgentResult(ok=True, cost_usd=0.1, summary="acted")

    gate = CallableGate(lambda ws: (ws / "solution.txt").exists())
    cfg = _config(git_repo, stops=StopsConfig(max_iter=6, no_progress_after=2))

    without = run_loop(cfg, PromptAwareAgent(), iteration_gate=gate, acceptance_gate=gate,
                       skills=InMemorySkillRegistry())
    assert without.reason is StopReason.NO_PROGRESS         # the marker never reached the agent

    reg = InMemorySkillRegistry([Skill(name="m", guidance="use MAGIC")])
    with_skill = run_loop(cfg, PromptAwareAgent(), iteration_gate=gate, acceptance_gate=gate,
                          skills=reg)
    assert with_skill.reason is StopReason.DONE
    assert with_skill.iterations == 1


# --- write-back / gated learning ------------------------------------------------------------

def test_write_back_mints_on_done_when_gate_passes():
    reg = InMemorySkillRegistry(write_back_gate=CallableGate(lambda ws: True))
    minted = reg.write_back(_done_result(), Path("."), goal="solve the thing")
    assert minted is not None
    assert len(reg.skills) == 1


def test_write_back_is_gated_out_when_gate_fails():
    reg = InMemorySkillRegistry(write_back_gate=CallableGate(lambda ws: False))
    minted = reg.write_back(_done_result(), Path("."), goal="solve the thing")
    assert minted is None
    assert reg.skills == []                                 # a failed write-back gate learns nothing


def test_write_back_is_idempotent_on_repeat():
    reg = InMemorySkillRegistry()
    first = reg.write_back(_done_result(), Path("."), goal="same goal")
    second = reg.write_back(_done_result(), Path("."), goal="same goal")
    assert first is not None and second is None             # same name -> not re-added
    assert len(reg.skills) == 1


def test_run_loop_writes_back_only_on_done(git_repo: Path):
    # A run that never reaches DONE must not mint a skill — write-back lives on the done path.
    reg = InMemorySkillRegistry()
    cfg = _config(git_repo, stops=StopsConfig(max_iter=3, no_progress_after=99))
    # A no-op agent never satisfies the gate -> runs to the iteration cap, never DONE.
    result = run_loop(cfg, MockAgent(behaviors=[]),
                      iteration_gate=CallableGate(lambda ws: False),
                      acceptance_gate=CallableGate(lambda ws: False), skills=reg)
    assert result.reason is StopReason.ITERATION_CAP
    assert reg.skills == []


def test_run_loop_writes_back_a_skill_on_done(git_repo: Path):
    reg = InMemorySkillRegistry()
    gate = CallableGate(lambda ws: (ws / "solution.txt").exists())
    cfg = _config(git_repo, goal="implement the widget")
    result = run_loop(cfg, MockAgent(behaviors=[lambda ws: (ws / "solution.txt").write_text("x")]),
                      iteration_gate=gate, acceptance_gate=gate, skills=reg)
    assert result.reason is StopReason.DONE
    assert len(reg.skills) == 1
    assert "widget" in reg.skills[0].source_goal


def test_file_registry_persists_across_instances(tmp_path: Path):
    directory = tmp_path / "skills"
    first = FileSkillRegistry(directory, write_back_gate=CallableGate(lambda ws: True))
    minted = first.write_back(_done_result(), tmp_path, goal="persist this lesson")
    assert minted is not None
    # A brand-new registry pointed at the same directory inherits the lesson (the durable flywheel).
    reborn = FileSkillRegistry(directory)
    assert "persist this lesson" in reborn.render()


def test_shell_distiller_produces_skill_from_command_output(git_repo: Path):
    # A solved run's diff is distilled into a reusable lesson via a shell command's stdout.
    from loopkit.extensions.skills import ShellDistiller

    skill = ShellDistiller("echo 'prefer the comma-ok form for type assertions'")(
        object(), git_repo, "fix a Go type-assertion panic")
    assert skill is not None
    assert "comma-ok" in skill.guidance
    assert skill.name.startswith("skill-")
    assert skill.source_goal == "fix a Go type-assertion panic"


def test_shell_distiller_returns_none_on_failure_empty_or_blank_goal(git_repo: Path):
    # The flywheel declines to learn rather than learn noise: non-zero exit, empty output, blank goal.
    from loopkit.extensions.skills import ShellDistiller

    assert ShellDistiller("exit 1")(object(), git_repo, "a goal") is None
    assert ShellDistiller("true")(object(), git_repo, "a goal") is None      # empty stdout
    assert ShellDistiller("echo hi")(object(), git_repo, "   ") is None      # blank goal
