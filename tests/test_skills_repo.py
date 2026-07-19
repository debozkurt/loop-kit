"""Phase 5b — the `loopkit-skills` git-repo home for the cross-run flywheel.

`FileSkillRegistry` makes learning durable across processes on one filesystem; `GitSkillRegistry`
makes it durable across *machines* by backing that directory with a git repo. These tests prove the
whole flywheel with **no tokens and no network**: the real `_SubprocessGitTransport` runs against a
**local bare repo** that stands in for the remote `loopkit-skills` repo. The headline acceptance —
"a solved run writes a skill back that a later run reads" — is `test_run_loop_flywheel_across_clones`.

The transport is also injectable, so the registry's *logic* (push only on mint, pull on construct) is
unit-tested against a fake in isolation, and the cloud wiring (`worker_command`/`RunSpec`) is asserted
without a cluster.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from loopkit.agent import AgentResult, MockAgent
from loopkit.config import Config, GateConfig, StopsConfig
from loopkit.extensions.cloudrun import RunSpec, coordinator_command, worker_command
from loopkit.extensions.skills import GitSkillRegistry, Skill, _SubprocessGitTransport
from loopkit.gate import CallableGate
from loopkit.loop import RunResult, run_loop
from loopkit.stops import StopReason


# --- helpers -------------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_bare(tmp_path: Path, *, name: str = "skills.git", seed: bool = True,
              branch: str = "main") -> Path:
    """A bare git repo standing in for the remote `loopkit-skills`. `seed=True` lands one commit on
    `branch` so the branch exists (the non-empty-remote case); `seed=False` is a brand-new repo."""
    bare = tmp_path / name
    subprocess.run(["git", "init", "--bare", "-q", "-b", branch, str(bare)], check=True)
    if seed:
        work = tmp_path / f"{name}-seed"
        subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
        _git(work, "config", "user.email", "seed@loopkit")
        _git(work, "config", "user.name", "seed")
        (work / "README.md").write_text("loopkit-skills\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "seed")
        _git(work, "push", "-q", "origin", branch)
    return bare


def push_skill_directly(tmp_path: Path, bare: Path, *, name: str, guidance: str,
                        branch: str = "main", clone_name: str = "concurrent") -> None:
    """Simulate a *different* worker minting a skill: clone the bare repo, add one skill file, push.
    Used to set up the read fixtures and the concurrent-push race."""
    work = tmp_path / clone_name
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    _git(work, "config", "user.email", "other@loopkit")
    _git(work, "config", "user.name", "other-worker")
    (work / "skills").mkdir(exist_ok=True)
    (work / "skills" / f"{name}.md").write_text(f"## {name}\n{guidance}\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", f"skill: {name}")
    _git(work, "push", "-q", "origin", branch)


def fixed(name: str, guidance: str):
    """A distiller that always mints one named skill — so a test controls the skill identity."""
    return lambda run_result, workspace, goal: Skill(name=name, guidance=guidance, source_goal=goal)


def remote_skill_names(tmp_path: Path, bare: Path, *, branch: str = "main") -> set[str]:
    """The skill file stems currently on the bare repo's branch (a fresh read-only clone)."""
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "verify", branch=branch)
    return {p.stem for p in sorted((reg.local_dir / "skills").glob("*.md"))}


def _always_pass() -> CallableGate:
    return CallableGate(lambda ws: True)


def _done() -> RunResult:
    return RunResult(StopReason.DONE, 1, 0.0)


# --- the read/write edges over real git ----------------------------------------------------
def test_git_registry_renders_a_prior_lesson(tmp_path: Path):
    # A skill already on the remote (a past run) is rendered by a fresh clone — the read edge.
    bare = make_bare(tmp_path)
    push_skill_directly(tmp_path, bare, name="use-x", guidance="prefer X over Y")
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "clone", branch="main")
    rendered = reg.render()
    assert rendered.startswith("# Skills")
    assert "use-x" in rendered and "prefer X over Y" in rendered


def test_write_back_pushes_skill_to_remote(tmp_path: Path):
    bare = make_bare(tmp_path)
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "A", branch="main",
                           write_back_gate=_always_pass(), distill=fixed("alpha", "alpha lesson"))
    minted = reg.write_back(_done(), tmp_path, goal="do the alpha thing")
    assert minted is not None and minted.name == "alpha"
    # A different clone (a later run / another machine) sees the freshly-pushed skill.
    assert "alpha" in remote_skill_names(tmp_path, bare)


def test_write_back_gated_out_pushes_nothing(tmp_path: Path):
    bare = make_bare(tmp_path)
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "A", branch="main",
                           write_back_gate=CallableGate(lambda ws: False), distill=fixed("nope", "x"))
    assert reg.write_back(_done(), tmp_path, goal="should not learn") is None
    assert remote_skill_names(tmp_path, bare) == set()   # a failed write-back gate propagates nothing


def test_write_back_idempotent_no_second_push(tmp_path: Path):
    bare = make_bare(tmp_path)
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "A", branch="main",
                           write_back_gate=_always_pass(), distill=fixed("once", "the lesson"))
    assert reg.write_back(_done(), tmp_path, goal="g") is not None
    head_after_first = _git(bare, "rev-parse", "main").stdout.strip()
    assert reg.write_back(_done(), tmp_path, goal="g") is None       # same name -> not re-minted
    assert _git(bare, "rev-parse", "main").stdout.strip() == head_after_first   # no second commit


def test_bootstrap_empty_remote(tmp_path: Path):
    # A brand-new skills repo (no commits, no branch): construction must not crash, render is empty,
    # and the first write-back creates the branch + lands the first skill.
    bare = make_bare(tmp_path, seed=False)
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "A", branch="main",
                           write_back_gate=_always_pass(), distill=fixed("first", "first lesson"))
    assert reg.render() == ""
    assert reg.write_back(_done(), tmp_path, goal="seed the flywheel") is not None
    assert "first" in remote_skill_names(tmp_path, bare)


def test_concurrent_push_rebases_and_keeps_both(tmp_path: Path):
    # Two workers, one shared repo. Worker A clones, then worker B lands a *different* skill; A's push
    # is now non-fast-forward. The transport fetches + rebases (skills are file-disjoint) and retries,
    # so BOTH skills survive — the many-pods-one-repo race is handled, not lost.
    bare = make_bare(tmp_path)
    reg_a = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "A", branch="main",
                             write_back_gate=_always_pass(), distill=fixed("alpha", "alpha lesson"))
    push_skill_directly(tmp_path, bare, name="beta", guidance="beta lesson")   # B races in first
    minted = reg_a.write_back(_done(), tmp_path, goal="alpha goal")            # A pushes after B
    assert minted is not None
    assert {"alpha", "beta"} <= remote_skill_names(tmp_path, bare)


# --- bounded growth (Finding G): shallow clone + a render budget -----------------------------
def test_clone_is_shallow_but_renders_the_whole_tip(tmp_path: Path):
    # The remote accumulates history (seed + two skills = 3 commits); a per-task clone only ever needs
    # the current tip, so it is `--depth 1` (one local commit) yet renders every skill at that tip.
    bare = make_bare(tmp_path)
    push_skill_directly(tmp_path, bare, name="one", guidance="lesson one", clone_name="c1")
    push_skill_directly(tmp_path, bare, name="two", guidance="lesson two", clone_name="c2")
    assert _git(bare, "rev-list", "--count", "main").stdout.strip() == "3"   # full history on the remote
    reg = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "clone", branch="main")
    assert _git(reg.local_dir, "rev-list", "--count", "HEAD").stdout.strip() == "1"   # shallow clone
    rendered = reg.render()
    assert "one" in rendered and "two" in rendered          # the tip's full tree is present


def test_render_is_budget_bounded_and_honest(tmp_path: Path):
    # `render()` injects skills into every prompt, so an ever-growing repo must not grow the prompt
    # without limit: skills past the budget are dropped, but with a visible note (not silently).
    from loopkit.extensions.skills import FileSkillRegistry, _MAX_RENDER
    reg = FileSkillRegistry(tmp_path / "skills")
    for i in range(20):                                      # ~30k of content vs a 12k budget
        (reg.directory / f"skill-{i:02d}.md").write_text(f"## skill-{i:02d}\n" + ("x" * 1500) + "\n")
    rendered = reg.render()
    assert len(rendered) <= _MAX_RENDER + 500               # bounded — not the full ~30k
    assert "omitted to bound prompt size" in rendered       # the omission is honest, not silent
    assert "skill-00" in rendered                           # at least the first name-sorted skill kept
    assert "skill-19" not in rendered                       # the tail past the budget is dropped


# --- the full flywheel through run_loop (the acceptance) -----------------------------------
class AlwaysSolve:
    """Writes the solution every tick — a run that reaches DONE with no help (mints the lesson)."""

    def act(self, prompt: str, workspace: Path, *, observer=None) -> AgentResult:
        (workspace / "solution.txt").write_text("done")
        return AgentResult(ok=True, cost_usd=0.1, summary="solved")


class NeedsMagic:
    """Solves only when the prompt carries the MAGIC marker — i.e. only once the skill is rendered."""

    def act(self, prompt: str, workspace: Path, *, observer=None) -> AgentResult:
        if "MAGIC" in prompt:
            (workspace / "solution.txt").write_text("done")
        return AgentResult(ok=True, cost_usd=0.1, summary="acted")


def _cfg(repo: Path, goal: str) -> Config:
    return Config(goal=goal, repo=str(repo), branch="loopkit/test", gate=GateConfig(iteration="true"),
                  stops=StopsConfig(max_iter=6, no_progress_after=2))


def test_run_loop_flywheel_across_clones(tmp_path: Path, git_repo: Path):
    # Run A solves and writes back a MAGIC skill to the shared repo. Run B — a *fresh clone* (a later
    # run / another machine) — reads that skill from its prompt and solves on tick 1. This is the
    # Phase-5b acceptance: a solved run teaches the next one across a git repo, end to end, no tokens.
    bare = make_bare(tmp_path)
    gate = CallableGate(lambda ws: (ws / "solution.txt").exists())

    skills_a = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "A", branch="main",
                                write_back_gate=gate, distill=fixed("magic", "use MAGIC to solve"))
    result_a = run_loop(_cfg(git_repo, "implement the widget"), AlwaysSolve(),
                        iteration_gate=gate, acceptance_gate=gate, skills=skills_a)
    assert result_a.reason is StopReason.DONE
    assert "magic" in remote_skill_names(tmp_path, bare)             # the lesson reached the remote

    # A second repo + a fresh registry pointed at the SAME skills repo: the marker is rendered, so the
    # marker-dependent agent gets it right immediately.
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    for args in (["init", "-q"], ["branch", "-m", "main"], ["config", "user.email", "b@b"],
                 ["config", "user.name", "b"]):
        _git(repo_b, *args)
    (repo_b / "README.md").write_text("seed\n")
    _git(repo_b, "add", "-A")
    _git(repo_b, "commit", "-qm", "seed")

    skills_b = GitSkillRegistry(repo=str(bare), local_dir=tmp_path / "B", branch="main")
    assert "MAGIC" in skills_b.render()
    result_b = run_loop(_cfg(repo_b, "implement the widget"), NeedsMagic(),
                        iteration_gate=gate, acceptance_gate=gate, skills=skills_b)
    assert result_b.reason is StopReason.DONE
    assert result_b.iterations == 1                                 # handed the lesson, no detour


# --- transport is an injectable seam -------------------------------------------------------
class _FakeTransport:
    def __init__(self) -> None:
        self.pulls: list[tuple] = []
        self.pushes: list[tuple] = []

    def pull(self, repo_url, local_dir, branch):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        self.pulls.append((repo_url, str(local_dir), branch))
        return True

    def push(self, local_dir, branch, *, message):
        self.pushes.append((str(local_dir), branch, message))
        return True


def test_transport_pulls_on_construct_and_pushes_only_on_mint(tmp_path: Path):
    fake = _FakeTransport()
    reg = GitSkillRegistry(repo="git@example:loopkit-skills.git", local_dir=tmp_path / "s",
                           branch="main", write_back_gate=_always_pass(),
                           distill=fixed("learned", "the lesson"), transport=fake)
    assert len(fake.pulls) == 1 and fake.pulls[0][2] == "main"      # read edge fired on construct
    assert fake.pushes == []                                        # nothing pushed yet
    assert reg.write_back(_done(), tmp_path, goal="g") is not None
    assert len(fake.pushes) == 1 and "learned" in fake.pushes[0][2]  # write edge fired on mint
    assert reg.write_back(_done(), tmp_path, goal="g") is None       # dup -> not re-minted...
    assert len(fake.pushes) == 1                                     # ...and not re-pushed


def test_default_transport_never_raises_on_bad_remote(tmp_path: Path):
    # An unreachable/garbage remote must degrade (a run must never die because skills couldn't sync).
    transport = _SubprocessGitTransport()
    assert transport.pull("/nonexistent/skills.git", tmp_path / "x", "main") in (True, False)
    # construction tolerates it; render is empty; a write-back returns the skill but push is best-effort.
    reg = GitSkillRegistry(repo="/nonexistent/skills.git", local_dir=tmp_path / "x", branch="main",
                           write_back_gate=_always_pass(), distill=fixed("k", "v"))
    assert reg.render() == ""
    assert reg.write_back(_done(), tmp_path, goal="g") is not None   # skill still minted locally


# --- cloud wiring: the skills repo reaches the worker, never the coordinator ----------------
def _spec(**kw) -> RunSpec:
    base = dict(run_id="r1", image="ghcr.io/o/loopkit-worker:t", target="https://x/repo.git",
                goal="do a thing")
    base.update(kw)
    return RunSpec(**base)


def test_worker_command_carries_skills_repo_when_set():
    cmd = worker_command(_spec(skills_repo="https://github.com/o/loopkit-skills.git",
                               skills_branch="prod"))
    assert "--skills-repo" in cmd
    i = cmd.index("--skills-repo")
    assert cmd[i + 1] == "https://github.com/o/loopkit-skills.git"
    assert cmd[cmd.index("--skills-branch") + 1] == "prod"


def test_worker_command_omits_skills_when_unset():
    assert "--skills-repo" not in worker_command(_spec())


def test_coordinator_command_never_carries_skills():
    # The coordinator does no write-back, so it must never get the skills repo.
    cmd = coordinator_command(_spec(skills_repo="https://github.com/o/loopkit-skills.git"))
    assert "--skills-repo" not in cmd
