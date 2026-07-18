"""Batch tests: the manifest, the conflict-aware scheduler, the driver, and the CLI — no tokens.

The no-infra parallel batch reuses the fleet's queue/worker/outcome machinery, so what's new — and
what's tested here — is the layer on top: manifest validation (dup ids, dangling/cyclic `after`),
the scheduling rules (`group` = serialize in manifest order; `after` = gate on DONE, skip on
failure, cascade the skips), the driver's accounting (every task ends with an outcome), and the
runner's isolation (each task works a scratch clone; the source checkout is never touched). Fake
runners drive the scheduler deterministically; `MockAgent` + throwaway git repos drive the real
`run_loop` path; the CLI contract runs through Typer's `CliRunner`.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loopkit.agent import MockAgent
from loopkit.extensions.batch import (
    BatchDefaults,
    TaskSpec,
    VALIDATE_ABORT,
    SKIPPED,
    branch_for,
    load_manifest,
    make_batch_runner,
    plan_waves,
    ready_tasks,
    run_batch,
    skippable_tasks,
)
from loopkit.extensions.fleet import WorkerOutcome
from loopkit.stops import StopReason


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------
def _spec(id: str, **kw) -> TaskSpec:
    kw.setdefault("goal", f"solve {id}")
    return TaskSpec(id=id, **kw)


def _ok(task_id: str, branch: str = "b") -> WorkerOutcome:
    return WorkerOutcome(task_id=task_id, branch=branch, reason="done")


def _fail(task_id: str, branch: str = "b") -> WorkerOutcome:
    return WorkerOutcome(task_id=task_id, branch=branch, reason="no_progress")


def _manifest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "batch.toml"
    path.write_text(text)
    return path


def _seed_repo(path: Path) -> Path:
    """A fresh git repo on `main` with one commit — the clone source a batch task works from."""
    path.mkdir(parents=True)
    for args in (("init", "-q"), ("branch", "-m", "main"),
                 ("config", "user.email", "t@loopkit"), ("config", "user.name", "loopkit-test")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True, capture_output=True)
    return path


def _config_toml(path: Path, repo: Path, *, acceptance: str = "true") -> Path:
    path.write_text(
        f'goal = "placeholder"\nrepo = "{repo}"\n\n'
        f'[gate]\niteration = "true"\nacceptance = "{acceptance}"\n'
    )
    return path


# --------------------------------------------------------------------------------------------
# Manifest validation
# --------------------------------------------------------------------------------------------
def test_manifest_parses_defaults_and_tasks(tmp_path):
    mf = _manifest(tmp_path, """
[defaults]
config = "base.toml"
review = "judge.sh"

[[task]]
id = "a"
goal = "fix a"

[[task]]
id = "b"
issue = 42
group = "handlers"
after = ["a"]
validate = "repro.sh"
""")
    m = load_manifest(mf)
    assert m.defaults.config == "base.toml" and m.defaults.review == "judge.sh"
    assert [t.id for t in m.task] == ["a", "b"]
    b = m.task[1]
    assert b.issue == 42 and b.group == "handlers" and b.after == ["a"]
    assert b.validate_cmd == "repro.sh"          # `validate` in TOML maps to validate_cmd (alias)


@pytest.mark.parametrize("body, fragment", [
    ('[[task]]\nid = "a"\ngoal = "x"\n\n[[task]]\nid = "a"\ngoal = "y"\n', "duplicate task ids"),
    ('[[task]]\nid = "a"\ngoal = "x"\nafter = ["ghost"]\n', "unknown task"),
    ('[[task]]\nid = "a"\ngoal = "x"\nafter = ["a"]\n', "depends on itself"),
    ('[[task]]\nid = "a"\ngoal = "x"\nafter = ["b"]\n\n'
     '[[task]]\nid = "b"\ngoal = "y"\nafter = ["a"]\n', "cycle"),
    ('[[task]]\nid = "a"\n', "goal or issue"),
])
def test_manifest_rejects_incoherent_batches(tmp_path, body, fragment):
    with pytest.raises(ValueError, match=fragment):
        load_manifest(_manifest(tmp_path, body))


def test_branch_for_prefers_explicit_then_issue_then_id():
    assert branch_for(_spec("a", branch="loopkit/custom")) == "loopkit/custom"
    assert branch_for(_spec("a", issue=7)) == "loopkit/issue-7"
    assert branch_for(_spec("a")) == "loopkit/a"


# --------------------------------------------------------------------------------------------
# Scheduler rules (pure — no threads, no repos)
# --------------------------------------------------------------------------------------------
def test_ready_tasks_serializes_groups_in_manifest_order():
    specs = [_spec("g1", group="db"), _spec("g2", group="db"), _spec("free")]
    # Nothing finished: only the group's first member and the ungrouped task may start.
    assert [s.id for s in ready_tasks(specs, {}, set())] == ["g1", "free"]
    # g1 pushed but unfinished: g2 still blocked.
    assert [s.id for s in ready_tasks(specs, {}, {"g1", "free"})] == []
    # g1 finished (even unsuccessfully — a group is mutual exclusion, not a dependency): g2 unblocks.
    assert [s.id for s in ready_tasks(specs, {"g1": _fail("g1")}, {"g1", "free"})] == ["g2"]


def test_ready_tasks_gates_on_after_reaching_done():
    specs = [_spec("a"), _spec("b", after=["a"])]
    assert [s.id for s in ready_tasks(specs, {}, set())] == ["a"]
    # a finished but NOT done: b is not ready (it becomes skippable instead).
    assert [s.id for s in ready_tasks(specs, {"a": _fail("a")}, {"a"})] == []
    assert [s.id for s in ready_tasks(specs, {"a": _ok("a")}, {"a"})] == ["b"]


def test_skippable_cascades_through_skipped_dependencies():
    specs = [_spec("a"), _spec("b", after=["a"]), _spec("c", after=["b"])]
    finished = {"a": _fail("a")}
    assert [(s.id, dep) for s, dep in skippable_tasks(specs, finished, {"a"})] == [("b", "a")]
    # b's synthetic skip outcome is finished-not-done, so c skips on the next pass.
    finished["b"] = WorkerOutcome(task_id="b", branch="-", reason=SKIPPED)
    assert [(s.id, dep) for s, dep in skippable_tasks(specs, finished, {"a", "b"})] == [("c", "b")]


def test_plan_waves_layers_deps_and_groups():
    specs = [_spec("a"), _spec("b", after=["a"]), _spec("c", after=["b"]),
             _spec("g1", group="db"), _spec("g2", group="db"), _spec("free")]
    waves = plan_waves(specs)
    assert waves == {"a": 1, "b": 2, "c": 3, "g1": 1, "g2": 2, "free": 1}


# --------------------------------------------------------------------------------------------
# The driver — fake runners prove concurrency, serialization, skips, and the stall/timeout guards.
# --------------------------------------------------------------------------------------------
def test_run_batch_runs_independent_tasks_concurrently():
    # All three workers must be inside the runner at once to pass the barrier — with any less
    # concurrency the barrier times out, the outcomes are errors, and the assertion fails.
    barrier = threading.Barrier(3, timeout=10)

    def runner(task: dict) -> WorkerOutcome:
        barrier.wait()
        return _ok(task["id"], task["branch"])

    result = run_batch([_spec("a"), _spec("b"), _spec("c")], runner, jobs=3)
    assert len(result.done) == 3 and not result.failed


def test_run_batch_serializes_group_members_and_orders_them():
    intervals: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()

    def runner(task: dict) -> WorkerOutcome:
        start = time.monotonic()
        time.sleep(0.05)
        with lock:
            intervals[task["id"]] = (start, time.monotonic())
        return _ok(task["id"], task["branch"])

    specs = [_spec("g1", group="db"), _spec("g2", group="db"), _spec("g3", group="db")]
    result = run_batch(specs, runner, jobs=3)
    assert len(result.done) == 3
    # Manifest order, and no overlap between consecutive members.
    assert intervals["g1"][1] <= intervals["g2"][0]
    assert intervals["g2"][1] <= intervals["g3"][0]


def test_run_batch_orders_after_edges_and_skips_on_failure():
    order: list[str] = []
    lock = threading.Lock()

    def runner(task: dict) -> WorkerOutcome:
        with lock:
            order.append(task["id"])
        if task["id"] == "expand":
            return _ok(task["id"], task["branch"])
        if task["id"] == "flaky":
            return _fail(task["id"], task["branch"])
        return _ok(task["id"], task["branch"])

    specs = [_spec("expand"), _spec("contract", after=["expand"]),
             _spec("flaky"), _spec("dependent", after=["flaky"]),
             _spec("transitive", after=["dependent"])]
    result = run_batch(specs, runner, jobs=3)
    # The chain ordered; the failed task's dependents never ran — and the skip cascaded.
    assert order.index("expand") < order.index("contract")
    assert "dependent" not in order and "transitive" not in order
    by_id = {r.spec.id: r.outcome for r in result.rows}
    assert by_id["contract"].done
    assert by_id["dependent"].reason == SKIPPED and "flaky" in by_id["dependent"].error
    assert by_id["transitive"].reason == SKIPPED and "dependent" in by_id["transitive"].error
    assert [r.spec.id for r in result.skipped] == ["dependent", "transitive"]
    assert [r.spec.id for r in result.failed] == ["flaky"]


def test_run_batch_timeout_accounts_for_unfinished_tasks():
    release = threading.Event()

    def runner(task: dict) -> WorkerOutcome:
        release.wait(timeout=10)             # wedged until released — the batch must not wait
        return _ok(task["id"], task["branch"])

    try:
        result = run_batch([_spec("slow")], runner, jobs=1, timeout=0.3)
    finally:
        release.set()                        # let the daemon worker thread finish
    assert result.rows[0].outcome.reason == "error"
    assert "timeout" in result.rows[0].outcome.error


def test_run_batch_inherits_defaults_for_review_and_validate():
    seen: dict[str, dict] = {}

    def runner(task: dict) -> WorkerOutcome:
        seen[task["id"]] = task
        return _ok(task["id"], task["branch"])

    defaults = BatchDefaults(review="judge.sh", validate_cmd="repro.sh")
    specs = [_spec("inherits"), _spec("overrides", review="other.sh", validate_cmd="mine.sh")]
    run_batch(specs, runner, jobs=1, defaults=defaults)
    assert seen["inherits"]["review"] == "judge.sh" and seen["inherits"]["validate"] == "repro.sh"
    assert seen["overrides"]["review"] == "other.sh" and seen["overrides"]["validate"] == "mine.sh"


# --------------------------------------------------------------------------------------------
# The runner — real run_loop in a scratch clone; the source checkout stays untouched.
# --------------------------------------------------------------------------------------------
def test_make_batch_runner_solves_in_a_clone_and_leaves_the_source_alone(tmp_path):
    src = _seed_repo(tmp_path / "src")
    cfg_path = _config_toml(tmp_path / "task.toml", src, acceptance="test -f solved.txt")

    def agent_factory(task: dict) -> MockAgent:
        def solve(workspace: Path) -> str:
            (workspace / "solved.txt").write_text("ok")
            return "wrote solved.txt"
        return MockAgent(behaviors=[solve])

    runner = make_batch_runner(agent_factory=agent_factory)
    outcome = runner({"id": "t1", "goal": "write solved.txt", "config": str(cfg_path),
                      "branch": "loopkit/t1"})
    assert outcome.done and outcome.reason == StopReason.DONE.value
    assert outcome.pr_url is None                         # [remote] off — nothing left the machine
    # Physical isolation: the fix landed in the scratch clone, never in the source checkout.
    assert not (src / "solved.txt").exists()
    branches = subprocess.run(["git", "branch", "--list", "loopkit/t1"], cwd=src,
                              capture_output=True, text=True).stdout
    assert "loopkit/t1" not in branches


def test_make_batch_runner_validate_abort_spends_nothing(tmp_path):
    src = _seed_repo(tmp_path / "src")
    cfg_path = _config_toml(tmp_path / "task.toml", src)

    def agent_factory(task: dict) -> MockAgent:
        raise AssertionError("the agent must never be built when validate aborts")

    runner = make_batch_runner(agent_factory=agent_factory)
    outcome = runner({"id": "t1", "goal": "g", "config": str(cfg_path),
                      "branch": "loopkit/t1", "validate": "false"})
    assert outcome.reason == VALIDATE_ABORT and not outcome.done


def test_make_batch_runner_requires_some_config():
    runner = make_batch_runner()                          # no base config
    with pytest.raises(RuntimeError, match="no config"):
        runner({"id": "t1", "goal": "g", "branch": "loopkit/t1"})


# --------------------------------------------------------------------------------------------
# Wire format — the additive pr_url field + non-terminal reasons.
# --------------------------------------------------------------------------------------------
def test_worker_outcome_pr_url_survives_json_round_trip():
    out = WorkerOutcome(task_id="t", branch="b", reason="done", pr_url="https://x/pr/1")
    assert WorkerOutcome.from_json(out.to_json()).pr_url == "https://x/pr/1"
    # Older payloads without the field still parse (additive change).
    assert WorkerOutcome.from_json('{"task_id": "t", "branch": "b", "reason": "done"}').pr_url is None


def test_worker_outcome_to_run_result_none_for_batch_reasons():
    for reason in (SKIPPED, VALIDATE_ABORT, "error"):
        assert WorkerOutcome(task_id="t", branch="b", reason=reason).to_run_result() is None
    assert WorkerOutcome(task_id="t", branch="b", reason="done").to_run_result() is not None


# --------------------------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------------------------
def test_journal_records_outcomes_and_resume_skips_done(tmp_path):
    from loopkit.extensions.batch import load_journal

    calls: list[str] = []

    def runner(task: dict) -> WorkerOutcome:
        calls.append(task["id"])
        return _ok(task["id"]) if task["id"] == "a" else _fail(task["id"])

    progress: list[tuple[str, int, int]] = []
    journal = tmp_path / "batch.journal.jsonl"
    result = run_batch([_spec("a"), _spec("b")], runner, jobs=2, journal=journal,
                       on_finish=lambda o, n, total: progress.append((o.task_id, n, total)))
    assert len(result.rows) == 2
    assert len(journal.read_text().splitlines()) == 2     # appended as each outcome landed
    assert len(progress) == 2 and progress[-1][1:] == (2, 2)
    entries = load_journal(journal)
    assert entries["a"].done and not entries["b"].done

    # Resume: preload the journal's DONE entries — 'a' never re-runs, 'b' (failed) retries.
    calls.clear()
    result = run_batch([_spec("a"), _spec("b")], runner, jobs=2,
                       preloaded={"a": entries["a"]})
    assert calls == ["b"]
    assert {r.spec.id for r in result.rows} == {"a", "b"}  # preloaded still in the accounting


def test_journal_survives_torn_last_line(tmp_path):
    from loopkit.extensions.batch import load_journal

    journal = tmp_path / "j.jsonl"
    journal.write_text('{"task_id": "a", "branch": "b", "reason": "done"}\n{"task_id": "b", "bra')
    entries = load_journal(journal)                       # the crash-torn line is skipped, not fatal
    assert list(entries) == ["a"] and entries["a"].done


def test_preloaded_dependency_unblocks_dependent():
    calls: list[str] = []

    def runner(task: dict) -> WorkerOutcome:
        calls.append(task["id"])
        return _ok(task["id"])

    result = run_batch([_spec("a"), _spec("b", after=["a"])], runner,
                       preloaded={"a": _ok("a")})
    assert calls == ["b"]                                 # 'a' satisfied its dependents from the journal
    assert all(r.done for r in result.rows)


def _invoke(monkeypatch, tmp_path: Path, *args: str):
    from loopkit.cli import app
    nocreds = tmp_path / "nocreds"
    nocreds.mkdir(exist_ok=True)
    monkeypatch.setenv("LOOPKIT_CREDS_DIR", str(nocreds))
    return CliRunner().invoke(app, ["batch", *args])


def test_cli_dry_run_prints_schedule_and_runs_nothing(monkeypatch, tmp_path):
    src = _seed_repo(tmp_path / "src")
    _config_toml(tmp_path / "base.toml", src)
    mf = _manifest(tmp_path, """
[defaults]
config = "base.toml"

[[task]]
id = "a"
goal = "fix a"

[[task]]
id = "b"
goal = "fix b"
group = "db"

[[task]]
id = "c"
goal = "fix c"
group = "db"
after = ["a"]
""")
    result = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output and "schedule" in result.output


def test_cli_runs_a_mock_batch_to_done(monkeypatch, tmp_path):
    src = _seed_repo(tmp_path / "src")
    # adapter defaults to mock; both gates `true` ⇒ instant DONE with zero tokens.
    _config_toml(tmp_path / "base.toml", src)
    mf = _manifest(tmp_path, """
[defaults]
config = "base.toml"

[[task]]
id = "a"
goal = "trivially done"
""")
    out = tmp_path / "results.json"
    result = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--jobs", "1", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert "done" in result.output
    assert '"reason": "done"' in out.read_text()


def test_cli_rejects_invalid_manifest_and_bad_only(monkeypatch, tmp_path):
    src = _seed_repo(tmp_path / "src")
    _config_toml(tmp_path / "base.toml", src)
    bad = _manifest(tmp_path, '[[task]]\nid = "a"\ngoal = "x"\nafter = ["ghost"]\n')
    assert _invoke(monkeypatch, tmp_path, "--tasks", str(bad), "--dry-run").exit_code == 1

    ok = _manifest(tmp_path, """
[defaults]
config = "base.toml"

[[task]]
id = "a"
goal = "x"

[[task]]
id = "b"
goal = "y"
after = ["a"]
""")
    assert _invoke(monkeypatch, tmp_path, "--tasks", str(ok), "--only", "ghost",
                   "--dry-run").exit_code == 1
    # --only that drops a dependency is refused, not silently run against a missing base.
    assert _invoke(monkeypatch, tmp_path, "--tasks", str(ok), "--only", "b",
                   "--dry-run").exit_code == 1
    assert _invoke(monkeypatch, tmp_path, "--tasks", str(ok), "--only", "a",
                   "--dry-run").exit_code == 0


# --- --open-pr forge-token pre-flight warning (advisory; never blocks) ------------------------
def _gitlab_manifest(tmp_path: Path) -> Path:
    _config_toml(tmp_path / "base.toml", _seed_repo(tmp_path / "src"))
    return _manifest(tmp_path, '[defaults]\nconfig = "base.toml"\nprovider = "gitlab"\n\n'
                               '[[task]]\nid = "a"\ngoal = "fix a"\n')


def test_open_pr_without_forge_token_warns(monkeypatch, tmp_path):
    mf = _gitlab_manifest(tmp_path)
    for k in ("GITLAB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    res = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--open-pr", "--dry-run")
    assert res.exit_code == 0, res.output
    out = "".join(res.output.split())              # defeat rich line-wrapping
    assert "noforgetoken" in out and "GITLAB_TOKEN" in out


def test_open_pr_with_forge_token_is_quiet(monkeypatch, tmp_path):
    mf = _gitlab_manifest(tmp_path)
    monkeypatch.setenv("GITLAB_TOKEN", "x-token")
    res = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--open-pr", "--dry-run")
    assert res.exit_code == 0, res.output
    assert "noforgetoken" not in "".join(res.output.split())


def test_no_open_pr_never_warns_about_token(monkeypatch, tmp_path):
    mf = _gitlab_manifest(tmp_path)
    for k in ("GITLAB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    res = _invoke(monkeypatch, tmp_path, "--tasks", str(mf), "--dry-run")   # no --open-pr
    assert res.exit_code == 0 and "noforgetoken" not in "".join(res.output.split())
