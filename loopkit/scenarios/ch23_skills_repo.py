"""Ch 23 — the skills repo: the write-back flywheel made durable across machines (Part III, Phase 5b).

Ch 17 showed the flywheel with an *in-memory* registry: run A and run B shared one Python process, so
the lesson lived in RAM between them. The cloud fleet breaks that — every worker is its own ephemeral
pod with its own filesystem, sharing nothing. So the durable flywheel needs a network home: a dedicated
**`loopkit-skills` git repo**. Each run clones it at start (the read edge sees every prior lesson) and
pushes a **gated** write-back on DONE (the write edge propagates it). Git-native, versioned, reviewable,
zero new infra — and it reuses the forge auth the fleet already has.

This lab makes the two runs share *nothing but a git repo*: a local bare repo stands in for the remote,
and each run gets its own clone in its own directory — exactly two different pods. Scripted, token-free:
the point is the durable, cross-machine plumbing, on top of Ch 17's gated-distillation rule.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..extensions.skills import GitSkillRegistry
from . import Scenario, Stage, demo_config, pytest_gates
from .ch17_skills import SkillSeekingAgent, _distill_boundary


def _make_bare(parent: Path) -> Path:
    """A bare git repo standing in for the remote `loopkit-skills` (seeded so `main` exists)."""
    bare = parent / "loopkit-skills.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    seed = parent / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    for args in (["config", "user.email", "seed@loopkit"], ["config", "user.name", "seed"]):
        subprocess.run(["git", "-C", str(seed), *args], check=True)
    (seed / "README.md").write_text("# loopkit-skills\nCross-run learned state, one .md per skill.\n")
    for args in (["add", "-A"], ["commit", "-qm", "seed"], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", "-C", str(seed), *args], check=True)
    return bare


def run(stage: Stage) -> None:
    iteration, acceptance = pytest_gates()
    home = Path(tempfile.mkdtemp(prefix="loopkit-skills-"))
    bare = _make_bare(home)
    stage.beat("Ch 17's flywheel kept the lesson in [italic]memory[/] between two runs in one process. "
               "On the cloud fleet that's gone: every worker is its own [bold]pod with its own "
               "filesystem[/]. So the durable home is a dedicated [bold]`loopkit-skills` git repo[/] — "
               "clone it at start, push a [italic]gated[/] write-back on DONE. Here a local bare repo "
               "stands in for the remote; each run gets its [bold]own clone[/] — two different machines.")

    try:
        # --- Run A: a worker that learns the boundary, then writes the lesson back to the repo. ----
        stage.beat("[bold]Run A — pod A, fresh clone, empty repo.[/] The agent writes the naive "
                   "version, the held-out gate rejects it, it learns the boundary rule and fixes it "
                   "(two ticks). On DONE the lesson is distilled and — because the run cleared the "
                   "[italic]write-back gate[/] — committed and [bold]pushed to the skills repo[/].")
        repo_a = stage.fixture()
        reg_a = GitSkillRegistry(repo=str(bare), local_dir=home / "podA", branch="main",
                                 write_back_gate=acceptance, distill=_distill_boundary)
        result_a = stage.run(demo_config(repo_a, max_iter=6, no_progress_after=5), SkillSeekingAgent(),
                             iteration_gate=iteration, acceptance_gate=acceptance, skills=reg_a)

        on_remote = _remote_skills(home, bare)
        stage.beat(f"The skills repo now holds: [green]{', '.join(on_remote) or 'nothing'}[/] — a real "
                   "commit, versioned and reviewable in git. Pod A's filesystem is about to be thrown "
                   "away; the lesson is [bold]not[/] — it lives in the repo.")

        # --- Run B: a DIFFERENT pod (different clone dir) that shares only the git repo. -----------
        stage.beat("[bold]Run B — pod B, a different machine.[/] It shares [bold]nothing[/] with pod A "
                   "but the git repo: a fresh clone into its own directory. The boundary skill is "
                   "rendered into its prompt from the start, so it gets it right on [green]tick 1[/] — "
                   "no overfit detour. That is the flywheel surviving across pods.")
        repo_b = stage.fixture()
        reg_b = GitSkillRegistry(repo=str(bare), local_dir=home / "podB", branch="main",
                                 write_back_gate=acceptance, distill=_distill_boundary)
        result_b = stage.run(demo_config(repo_b, max_iter=6, no_progress_after=5), SkillSeekingAgent(),
                             iteration_gate=iteration, acceptance_gate=acceptance, skills=reg_b)

        stage.beat(f"Pod A took [yellow]{result_a.iterations}[/] ticks learning the rule and pushed it; "
                   f"pod B cloned the repo and took [green]{result_b.iterations}[/]. The gains compound "
                   "across [bold]machines[/], not just across a process — and the [italic]gated[/] "
                   "write-back is still what keeps the shared repo from learning junk. In the cloud "
                   "worker this is one flag: `loopkit fleet worker --skills-repo …`, pushing with "
                   "loopkit-core's own git token (never the agent's reach).")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _remote_skills(home: Path, bare: Path) -> list[str]:
    """The skill names currently on the bare repo (a throwaway read-only clone) — proves it's in git."""
    reg = GitSkillRegistry(repo=str(bare), local_dir=home / "verify", branch="main")
    return [p.stem for p in sorted((reg.local_dir / "skills").glob("*.md"))]


SCENARIO = Scenario(chapter=23, slug="skills-repo", title="The skills repo (durable flywheel)",
                    teaches="The Ch 17 write-back flywheel made durable across machines: a dedicated "
                            "loopkit-skills git repo each worker clones at start and pushes a gated "
                            "write-back to on DONE — gains compound across pods, not just one process.",
                    live_supported=False, run=run)
