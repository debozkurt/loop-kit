"""Plan-driven backlog mode (shape #2): the loop grinds a markdown checklist, one item per tick, and
is DONE only when every item is checked AND the acceptance gate passes.

Driven by MockAgent + CallableGate — the whole tick lifecycle runs deterministically, no agent binary,
no tokens (Ch 9 discipline). None-safe: with no `[plan]` file the loop behaves exactly as before.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from loopkit._templates import _PLAN_CONFIG_TEMPLATE, _PLAN_IMPLEMENTATION_TEMPLATE, _PLAN_PROMPT_TEMPLATE
from loopkit.agent import MockAgent
from loopkit.config import Config, GateConfig, PlanConfig, StopsConfig
from loopkit.gate import CallableGate
from loopkit.loop import run_loop
from loopkit.plan import PlanState, read_plan
from loopkit.stops import LoopState, PlanStall, StopReason


def _config(repo: Path, **overrides) -> Config:
    base = dict(goal="work the checklist", repo=str(repo), branch="loopkit/test",
                gate=GateConfig(iteration="true"))
    base.update(overrides)
    return Config(**base)


def _writes(name: str, content: str = "ok"):
    def behavior(workspace: Path) -> str:
        (workspace / name).write_text(content)
        return f"wrote {name}"
    return behavior


def _complete_one_item(plan: str = "IMPLEMENTATION_PLAN.md"):
    """A tick that marks the first open (`- [ ]`) checklist item done (`- [x]`)."""
    def behavior(workspace: Path) -> str:
        path = workspace / plan
        path.write_text(path.read_text().replace("- [ ]", "- [x]", 1))
        return "completed one item"
    return behavior


# --- read_plan parsing -------------------------------------------------------------------------

def test_read_plan_counts_open_and_done(tmp_path: Path):
    (tmp_path / "P.md").write_text(
        "# plan\n"
        "- [ ] a\n"
        "- [x] b\n"
        "  - [ ] nested c\n"      # indented sub-item still counts
        "* [X] star, capital X\n"
        "+ [ ] plus bullet\n"
        "not an item at all\n"
    )
    st = read_plan(tmp_path, "P.md")
    assert (st.open, st.done, st.total) == (3, 2, 5)
    assert st.blocks_done is True


def test_read_plan_missing_or_no_items_tracks_nothing(tmp_path: Path):
    # A missing file, or one with no checkboxes, reports nothing to track — so it never blocks DONE.
    assert read_plan(tmp_path, "nope.md") == PlanState(0, 0)
    (tmp_path / "prose.md").write_text("# just prose\nno checkboxes here\n")
    st = read_plan(tmp_path, "prose.md")
    assert st.total == 0 and st.blocks_done is False


# --- the loop's plan-aware terminal ------------------------------------------------------------

def test_done_only_when_checklist_empty_and_acceptance_passes(git_repo: Path):
    # Two open items; both gates would pass every tick. The loop must NOT declare done on tick 1 (an
    # item is still open) — it is DONE only once the checklist is empty AND acceptance passes.
    (git_repo / "IMPLEMENTATION_PLAN.md").write_text("# plan\n- [ ] item one\n- [ ] item two\n")
    cfg = _config(git_repo, plan=PlanConfig(file="IMPLEMENTATION_PLAN.md"),
                  stops=StopsConfig(max_iter=10, no_progress_after=99))
    agent = MockAgent(behaviors=[_complete_one_item(), _complete_one_item()])
    always = CallableGate(lambda ws: True)
    result = run_loop(cfg, agent, iteration_gate=always, acceptance_gate=always)
    assert result.reason is StopReason.DONE
    assert result.iterations == 2                      # not 1 — item two had to be cleared first
    assert (result.plan_open, result.plan_total) == (0, 2)


def test_open_item_blocks_done_even_when_both_gates_pass(git_repo: Path):
    # The whole point: an open checklist item overrides green gates. The agent makes real git progress
    # every tick (so no-progress can't fire) but never marks the item, so the run only hits the cap.
    (git_repo / "IMPLEMENTATION_PLAN.md").write_text("# plan\n- [ ] never marked done\n")
    cfg = _config(git_repo, plan=PlanConfig(file="IMPLEMENTATION_PLAN.md"),
                  stops=StopsConfig(max_iter=4, no_progress_after=99))
    agent = MockAgent(behaviors=[_writes(f"f{i}.txt", str(i)) for i in range(4)])
    always = CallableGate(lambda ws: True)
    result = run_loop(cfg, agent, iteration_gate=always, acceptance_gate=always)
    assert result.reason is StopReason.ITERATION_CAP   # never DONE — the open item held it open
    assert (result.plan_open, result.plan_total) == (1, 1)


def test_no_plan_config_is_exact_prior_behavior(git_repo: Path):
    # Without a [plan] file the loop never consults the plan module: gates alone certify DONE on tick 1,
    # and the RunResult carries no plan progress (None).
    cfg = _config(git_repo)
    agent = MockAgent(behaviors=[_writes("solution.txt")])
    always = CallableGate(lambda ws: True)
    result = run_loop(cfg, agent, iteration_gate=always, acceptance_gate=always)
    assert result.reason is StopReason.DONE
    assert result.iterations == 1
    assert result.plan_open is None and result.plan_total is None


def test_empty_plan_falls_back_to_gates(git_repo: Path):
    # A plan file with no open items (all prose / all done) must not wedge the loop — the gates decide.
    (git_repo / "IMPLEMENTATION_PLAN.md").write_text("# plan\n- [x] already done\n")
    cfg = _config(git_repo, plan=PlanConfig(file="IMPLEMENTATION_PLAN.md"),
                  stops=StopsConfig(max_iter=5, no_progress_after=99))
    agent = MockAgent(behaviors=[_writes("solution.txt")])
    always = CallableGate(lambda ws: True)
    result = run_loop(cfg, agent, iteration_gate=always, acceptance_gate=always)
    assert result.reason is StopReason.DONE
    assert result.iterations == 1
    assert (result.plan_open, result.plan_total) == (0, 1)


# --- plan-stall detection (the plan-mode NoProgress) -------------------------------------------

def _state(dones):
    return LoopState(iteration=1, cost_usd=0.0, signature="s", signatures=["s"], plan_dones=dones)


def test_plan_stall_policy_watches_the_done_count():
    stall = PlanStall(window=3)                       # fires once window+1 ticks show no gain
    assert stall.triggered(_state(None)) is False     # off plan mode: never fires
    assert stall.triggered(_state([0, 0, 0])) is False   # too few samples (len <= window)
    assert stall.triggered(_state([0, 0, 0, 0])) is True    # 4 ticks, nothing completed -> stalled
    assert stall.triggered(_state([2, 3, 4, 5])) is False   # one item a tick -> healthy
    assert stall.triggered(_state([2, 2, 2, 3])) is False   # completed at the end of the window -> ok
    assert stall.triggered(_state([4, 4, 4, 3])) is True     # an item got UN-checked -> stalled/regressed


def test_plan_stall_halts_churn_even_when_no_progress_is_blind(git_repo: Path):
    # The whole reason PlanStall exists: the agent writes a NEW file every tick (the git signature
    # changes, so NoProgress — disabled here anyway — could never fire) but never marks the item.
    # PlanStall must halt it EARLY, well before the iteration cap, on the done-count.
    (git_repo / "IMPLEMENTATION_PLAN.md").write_text("# plan\n- [ ] never gets done\n")
    cfg = _config(git_repo, plan=PlanConfig(file="IMPLEMENTATION_PLAN.md"),
                  stops=StopsConfig(max_iter=20, no_progress_after=99, plan_stall_after=3))
    agent = MockAgent(behaviors=[_writes(f"f{i}.txt", str(i)) for i in range(20)])
    always = CallableGate(lambda ws: True)
    result = run_loop(cfg, agent, iteration_gate=always, acceptance_gate=always)
    assert result.reason is StopReason.PLAN_STALL
    assert result.iterations == 4                      # window 3 -> fires on the 4th no-gain tick
    assert result.iterations < 20                      # the point: not the cap
    assert (result.plan_open, result.plan_total) == (1, 1)
    assert "no checklist item completed" in result.detail


def test_plan_stall_tolerates_a_slow_item_that_eventually_completes(git_repo: Path):
    # A legitimately hard item may span a few ticks before it lands. As long as an item completes
    # inside the window, PlanStall must NOT fire and the run reaches DONE.
    (git_repo / "IMPLEMENTATION_PLAN.md").write_text("# plan\n- [ ] one slow item\n")
    cfg = _config(git_repo, plan=PlanConfig(file="IMPLEMENTATION_PLAN.md"),
                  stops=StopsConfig(max_iter=20, no_progress_after=99, plan_stall_after=4))
    agent = MockAgent(behaviors=[_writes("f1.txt"), _writes("f2.txt"), _complete_one_item()])
    always = CallableGate(lambda ws: True)
    result = run_loop(cfg, agent, iteration_gate=always, acceptance_gate=always)
    assert result.reason is StopReason.DONE            # completed on tick 3, inside the 4-tick window
    assert result.iterations == 3
    assert (result.plan_open, result.plan_total) == (0, 1)


# --- the `init --plan` scaffold ----------------------------------------------------------------

def test_plan_scaffold_config_is_valid_and_wired(tmp_path: Path):
    cfg = Config.model_validate(tomllib.loads(_PLAN_CONFIG_TEMPLATE))
    assert cfg.plan.file == "IMPLEMENTATION_PLAN.md"                 # plan mode on
    assert "IMPLEMENTATION_PLAN.md" in cfg.prompt.anchors           # the agent can read/maintain it
    assert cfg.stops.max_iter >= 30                                 # a backlog needs headroom
    assert 1 <= cfg.stops.plan_stall_after < cfg.stops.max_iter     # stall stop can fire before the cap
    assert "- [ ]" in _PLAN_IMPLEMENTATION_TEMPLATE                 # starter checklist has open items
    assert "one item" in _PLAN_PROMPT_TEMPLATE.lower()             # prompt is the one-item-a-tick discipline
