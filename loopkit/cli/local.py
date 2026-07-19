"""Local commands: init, doctor, run, measure, synth-gate, detect, route, demo, learn, executor.

The CI deployment tier rides `run` (--from-event/--from-issue/--open-pr); `executor` is the keyless
agent-isolation sidecar; `synth-gate` + `detect` + `route` are the Part IV molding primitives (verify a
proposed oracle · introspect a repo → a proposed loopkit.toml · measure pass^k → single-vs-evolve).
Extension imports stay function-local so importing the CLI pulls no optional dep.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .. import pricing, safety, scenarios, secrets, trace
from ..agent import build_agent
from ..config import Config, load_config
from ..loop import run_loop
from ..pricing import DEFAULT_MODELS
from ..stops import StopReason
from .._templates import (_CI_TEMPLATES, _CONFIG_TEMPLATE, _PLAN_CONFIG_TEMPLATE,
                          _PLAN_IMPLEMENTATION_TEMPLATE, _PLAN_PROMPT_TEMPLATE, _PROMPT_TEMPLATE)
from ._support import DEFAULT_CONFIG, _load, _render, app, console, err, fail


@app.command()
def init(path: Path = typer.Argument(Path("."), help="Repository to set up."),
         plan: bool = typer.Option(False, "--plan",
                                   help="Plan-driven backlog mode: scaffold a checklist the loop grinds "
                                        "through, one item per tick (instead of a single task)."),
         ci: str | None = typer.Option(None, "--ci",
                                        help="Also scaffold a CI workflow: github | gitlab (Phase 5c).")) -> None:
    """Scaffold a starter loopkit.toml and PROMPT.md in PATH (never overwrites).

    With `--plan`, scaffold plan-driven backlog mode instead: a loopkit.toml wired to a checklist, a
    plan-driven PROMPT.md, and a starter IMPLEMENTATION_PLAN.md. One loop works through the checklist
    item by item — the run is DONE when every item is checked and the acceptance gate passes.

    With `--ci github|gitlab`, also scaffold a CI workflow that runs the loop on a labelled issue with
    no cluster (the CI deployment tier) — see docs/part-iii-ci-mode.md.
    """
    path = path.expanduser().resolve()
    if plan:
        files = [("loopkit.toml", _PLAN_CONFIG_TEMPLATE), ("PROMPT.md", _PLAN_PROMPT_TEMPLATE),
                 ("IMPLEMENTATION_PLAN.md", _PLAN_IMPLEMENTATION_TEMPLATE)]
    else:
        files = [("loopkit.toml", _CONFIG_TEMPLATE), ("PROMPT.md", _PROMPT_TEMPLATE)]
    if ci is not None:
        if ci not in _CI_TEMPLATES:
            fail("init", f"unknown --ci value {ci!r} (expected: github | gitlab).")
        files.append(_CI_TEMPLATES[ci])
    wrote: list[str] = []
    for name, content in files:
        target = path / name
        if target.exists():
            err.print(f"[yellow]exists, skipped[/] {name}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)     # e.g. .github/workflows/
        target.write_text(content)
        wrote.append(name)
    body = "\n".join(f"[green]wrote[/] {w}" for w in wrote) or "nothing new to write"
    console.print(Panel.fit(body, title="loopkit init"))
    if plan:
        console.print("Next: fill [bold]IMPLEMENTATION_PLAN.md[/] with your requirements checklist, set "
                      "the gates in loopkit.toml, then [bold]loopkit doctor[/] to validate.")
    else:
        console.print("Next: edit the goal + gates, then [bold]loopkit doctor[/] to validate.")


@app.command()
def doctor(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
           gate: bool = typer.Option(True, "--gate/--no-gate",
                                     help="Run the iteration gate once on the current tree and report its "
                                          "verdict — the highest-signal readiness check (default on; pass "
                                          "--no-gate to skip, e.g. when the gate is slow).")) -> None:
    """Pre-flight checks: is this repo safe to point the loop at, and is the gate set up to actually work?

    Beyond the static checks (branch, agent, budget), `doctor` runs the iteration gate once on the
    unchanged tree — the single highest-signal readiness check. A gate that already *passes* means the
    loop may declare DONE immediately (or is too weak); a gate that *fails* is the healthy precondition
    (the loop has something to drive toward); a broken command is flagged. It also warns when the
    acceptance gate is identical to the iteration gate (which defeats the held-out check, Ch 9).
    """
    cfg = _load(config)
    table = Table(title="loopkit doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")

    pf = safety.preflight(cfg)
    if pf.ok:
        table.add_row("safety preflight", "[green]ok[/]", f"branch {cfg.branch}")
    else:
        for problem in pf.problems:
            table.add_row("safety preflight", "[red]fail[/]", problem)

    _doctor_agent(table, cfg)
    _doctor_budget(table, cfg)

    table.add_row("iteration gate", "[green]set[/]", cfg.gate.iteration)
    if cfg.gate.acceptance:
        guarded = bool(cfg.safety.protected_paths)
        table.add_row("acceptance gate", "[green]set[/]" if guarded else "[yellow]unguarded[/]",
                      cfg.gate.acceptance)
        # The held-out gate must be a DIFFERENT check than iteration, or the loop optimizes against the
        # very thing meant to catch its overfitting (Ch 9) — a common, silent setup mistake.
        if cfg.gate.acceptance.strip() == cfg.gate.iteration.strip():
            table.add_row("held-out check", "[red]not held-out[/]",
                          "the acceptance gate is identical to the iteration gate — the loop can overfit "
                          "it. Make acceptance a broader/different check it never optimizes against.")
    else:
        table.add_row("acceptance gate", "[yellow]none[/]", "no held-out check (Ch 9)")

    # The single highest-signal readiness check: run the iteration gate once on the unchanged tree.
    # Informational — it never changes doctor's exit code (which tracks the safety preflight).
    if gate:
        _doctor_gate_verdict(table, cfg)

    # Continuous review (Ch 8): make the decision apparent here — WHICH judge gates DONE (custom
    # command or the built-in default and its backend/model), or WHY review is off. On-by-default
    # means a review model call per plausibly-done tick; doctor is where that spend is visible
    # before a run starts.
    review = cfg.review.decide()
    if review.kind == "default":
        table.add_row("review", "[green]on[/]",
                      escape(f"{_review_detail(review, cfg)} — a review model call per "
                             "plausibly-done tick (--no-review opts out)"))
        _doctor_judge(table, cfg)          # is the judge runnable here, and will its spend price?
    elif review.on:
        table.add_row("review", "[green]on[/]", escape(review.command))
    else:
        table.add_row("review", "[yellow]off[/]", escape(review.reason))

    # Tracing (Ch 14-15): full-tree LangSmith observability, auto-on when langsmith + a key present.
    trace.configure(cfg.trace)
    if trace.active():
        table.add_row("tracing", "[green]on[/]", f"LangSmith → project {trace.project()}")
    elif cfg.trace.enabled:
        table.add_row("tracing", "[yellow]unavailable[/]",
                      escape("enabled but langsmith not importable (pip install 'loopkit[trace]')"))
    else:
        table.add_row("tracing", "[dim]off[/]",
                      escape("auto: install loopkit[trace] + set LANGSMITH_API_KEY"))

    console.print(table)
    if not pf.ok:
        raise typer.Exit(1)


def _review_detail(decision, cfg: Config) -> str:
    """The human-readable judge identity for a review decision: the shell command for
    kind=="command", the resolved backend/model for the built-in default judge — so the run line
    and doctor always say WHICH judge gates DONE, not just that one does."""
    if decision.kind == "command":
        return decision.command
    from ..extensions.judge import resolve_judge
    target = resolve_judge(cfg.review, cfg.agent)
    return f"built-in judge: {target.backend}/{target.model or 'backend default'}"


# Adapter binary / SDK / key for each adapter name, used by `doctor`.
_CLI_BINARIES = {"claude-code": "claude", "codex": "codex"}


_API_REQUIREMENTS = {"claude-api": ("anthropic", "ANTHROPIC_API_KEY", "claude"),
                     "openai-api": ("openai", "OPENAI_API_KEY", "openai")}


def _claude_code_auth_note(cfg: Config) -> str:
    """How `claude-code` will authenticate — surfaced so a run's BILLING is visible before it starts.
    doctor doesn't install/scrub creds, so os.environ still reflects the shell as the agent would see it."""
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if cfg.agent.use_api_key:
        return ("auth [yellow]ANTHROPIC_API_KEY → billed API[/]" if have_key
                else "[yellow]--api-key set but ANTHROPIC_API_KEY not in env[/]")
    note = "auth subscription (claude login / CLAUDE_CODE_OAUTH_TOKEN)"
    if have_key:
        note += " [dim]· ANTHROPIC_API_KEY present but withheld (--api-key to bill it)[/]"
    return note


def _doctor_agent(table: Table, cfg: Config) -> None:
    """Is the configured adapter actually runnable here — binary on PATH, or SDK + key present?"""
    adapter = cfg.agent.adapter
    if adapter == "mock":
        table.add_row("agent", "[green]ok[/]", "mock (no binary needed)")
    elif adapter in _CLI_BINARIES:
        binary = _CLI_BINARIES[adapter]
        found = shutil.which(binary)
        detail = found or f"{binary} not on PATH"
        if found and adapter == "claude-code":             # surface which credential will be billed
            detail = f"{found} · {_claude_code_auth_note(cfg)}"
        table.add_row("agent", "[green]ok[/]" if found else "[red]missing[/]", detail)
    elif adapter in _API_REQUIREMENTS:
        pkg, env, extra = _API_REQUIREMENTS[adapter]
        have_sdk = importlib.util.find_spec(pkg) is not None
        have_key = bool(os.environ.get(env))
        if have_sdk and have_key:
            table.add_row("agent", "[green]ok[/]", f"{adapter} (SDK + {env})")
        elif not have_sdk:
            table.add_row("agent", "[red]missing[/]",
                          escape(f"{pkg} SDK not installed (pip install 'loopkit[{extra}]')"))
        else:
            table.add_row("agent", "[yellow]no key[/]", f"{env} not set")
    else:
        table.add_row("agent", "[red]unknown[/]", f"unknown adapter {adapter!r}")


def _doctor_judge(table: Table, cfg: Config) -> None:
    """Is the built-in judge actually runnable here — and will its spend be priced?

    Mirrors `_doctor_agent`, but for the JUDGE's resolved backend, which a `[review] backend`
    override can point at a different binary and a different credential than the agent's. Also
    flags an unpriced judge model: `estimate_cost` returns 0.0 for models missing from the price
    table, so an unpriced judge spends invisibly to the budget ceiling (warn-and-run, by design).
    """
    from ..extensions.judge import resolve_judge
    target = resolve_judge(cfg.review, cfg.agent)
    if target.backend == "mock":
        table.add_row("judge", "[green]ok[/]", "mock (auto-approve, no model call)")
        return
    if target.backend in _CLI_BINARIES:
        binary = _CLI_BINARIES[target.backend]
        found = shutil.which(binary)
        status = "[green]ok[/]" if found else "[red]missing[/]"
        detail = found or (f"{binary} not on PATH — the run halts REVIEW_UNAVAILABLE at the "
                           f"first verdict")
        if found and target.backend == "claude-code":
            detail = f"{found} · {_claude_code_auth_note(cfg)}"
    elif target.backend in _API_REQUIREMENTS:
        pkg, env, extra = _API_REQUIREMENTS[target.backend]
        have_sdk = importlib.util.find_spec(pkg) is not None
        status = "[green]ok[/]" if have_sdk else "[red]missing[/]"
        detail = (f"{target.backend} SDK present" if have_sdk
                  else f"{pkg} SDK not installed (pip install 'loopkit[{extra}]')")
    else:
        table.add_row("judge", "[red]unknown[/]", f"unknown judge backend {target.backend!r}")
        return
    model = target.model or DEFAULT_MODELS.get(target.backend)
    if pricing.known_model(model):
        detail += f" · model {model}"
    else:
        detail += " · [yellow]model unpriced — judge spend won't count toward max_cost_usd[/]"
    table.add_row("judge", status, detail)


def _doctor_budget(table: Table, cfg: Config) -> None:
    """Will the budget ceiling actually bite? It only fires if the adapter reports a real cost."""
    adapter = cfg.agent.adapter
    ceiling = f"ceiling ${cfg.agent.max_cost_usd}"
    model = cfg.agent.model or DEFAULT_MODELS.get(adapter)
    if adapter == "mock":
        table.add_row("budget", "[green]ok[/]", f"mock charges per tick · {ceiling}")
    elif adapter == "claude-code":
        table.add_row("budget", "[green]ok[/]", f"cost parsed from claude JSON · {ceiling}")
    elif adapter in _API_REQUIREMENTS:
        if pricing.known_model(model):
            table.add_row("budget", "[green]ok[/]", f"priced model {model} · {ceiling}")
        else:
            table.add_row("budget", "[yellow]no price[/]",
                          f"unknown model {model!r} → cost 0.0; budget stop can't fire")
    elif adapter == "codex":
        if pricing.known_model(model):
            table.add_row("budget", "[green]ok[/]", f"token cost for {model} · {ceiling}")
        else:
            table.add_row("budget", "[yellow]no price[/]",
                          "codex cost needs a priced --model, else 0.0")


# Output signatures that mean the gate COMMAND is broken (a typo, a missing tool/path) rather than a
# legitimate test failure — the most common beginner gate mistake. Heuristic, so the row is hedged.
# ": not found" (with the colon) covers dash/ash/busybox `sh: 1: <cmd>: not found` — the default
# /bin/sh on Debian/Ubuntu (and thus most CI) — where bash says the fuller "command not found"; the
# colon keeps it from matching prose like "element not found" in a real test failure.
_GATE_BROKEN_HINTS = ("command not found", ": not found", "no such file or directory",
                      "not recognized", "can't open file", "cannot find", "no tests ran")


def _doctor_gate_verdict(table: Table, cfg: Config) -> None:
    """Run the iteration gate once on the current tree and translate the verdict into a readiness signal.

    The gate runs the project's own test command in `repo_path()`; the result tells a beginner the one
    thing the static checks can't — whether the loop has real work to do, will instantly (falsely)
    succeed against a too-weak gate, or is pointed at a gate command that doesn't even run.
    """
    from ..gate import ShellGate
    try:
        result = ShellGate(cfg.gate.iteration).check(cfg.repo_path())
    except Exception as exc:                          # noqa: BLE001 — can't even launch it → a misconfig
        table.add_row("gate verdict", "[red]error[/]", escape(f"couldn't run `{cfg.gate.iteration}`: {exc}"))
        return
    if result.passed:
        table.add_row("gate verdict", "[yellow]already passes[/]",
                      "green on the unchanged tree → the loop may finish immediately. Make sure the gate "
                      "captures the goal (a too-weak gate = an instant false DONE).")
        return
    feedback = result.feedback or ""
    last = next((ln for ln in reversed(feedback.splitlines()) if ln.strip()), "")
    if any(hint in feedback.lower() for hint in _GATE_BROKEN_HINTS):
        table.add_row("gate verdict", "[red]gate looks broken[/]",
                      escape(f"the command failed to run cleanly (not a test failure) — check "
                             f"`gate.iteration`: {last[:120]}"))
    else:
        detail = "fails on the unchanged tree → the loop has something to drive toward (the healthy start)."
        if last:
            detail += f" Last line: {escape(last[:120])}"
        table.add_row("gate verdict", "[green]fails → has work[/]", detail)


@app.command()
def run(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
        repo: str | None = typer.Option(None, "--repo", help="Override the target repo (config `repo`)."),
        branch: str | None = typer.Option(None, "--branch",
                                          help="Override the configured branch for this run — per-run isolation, "
                                               "e.g. loopkit/issue-42 so concurrent issue→PR runs don't collide "
                                               "on one branch. Still safety-checked against allow/forbid_branches."),
        adapter: str | None = typer.Option(None, "--adapter",
                                            help="Override the configured agent adapter (e.g. claude-api in CI)."),
        from_event: Path | None = typer.Option(None, "--from-event",
                                                help="Set the goal from a forge event JSON (Actions "
                                                     "$GITHUB_EVENT_PATH / GitLab CI): an issue, or a "
                                                     "changes-requested review on a loopkit PR (a revise "
                                                     "run that resumes the PR's branch). CI tier."),
        from_issue: int | None = typer.Option(None, "--from-issue",
                                              help="Set the goal by fetching one issue by number via gh/glab. CI tier."),
        provider: str = typer.Option("auto", "--provider",
                                     help="Forge for --from-issue: auto | github | gitlab."),
        open_pr: bool = typer.Option(False, "--open-pr",
                                     help="Enable push + draft PR for this run (overrides [remote]). CI tier."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Run the control flow, skip the agent."),
        max_iter: int | None = typer.Option(None, "--max-iter", help="Override stops.max_iter."),
        check_gate: int | None = typer.Option(None, "--check-gate",
                                              help="Run the iteration gate N times on the initial tree "
                                                   "and refuse to start unless every run agrees — a flaky "
                                                   "gate corrupts the stop oracle (Ch 9). Overrides "
                                                   "safety.gate_stability_runs."),
        force: bool = typer.Option(False, "--force", help="Run even if preflight fails."),
        api_key: bool = typer.Option(False, "--api-key",
                                     help="claude-code: bill ANTHROPIC_API_KEY instead of the subscription "
                                          "(default). Sets [agent] use_api_key for this run."),
        sandbox: bool = typer.Option(False, "--sandbox",
                                     help="Run the loop inside the loopkit Docker container (Ch 16)."),
        review: str | None = typer.Option(None, "--review",
                                          help="A review command run after each tick's commit "
                                               "(ShellReviewHook). Exit 0 = clean; non-zero blocks "
                                               "DONE and its output is fed back as the next tick's "
                                               "review feedback. E.g. an adversarial LLM judge. "
                                               "Overrides [review] command from config."),
        no_review: bool = typer.Option(False, "--no-review",
                                       help="Disable the review gate for this run, even when "
                                            "[review] command is configured (the opt-out)."),
        skills: str | None = typer.Option(None, "--skills",
                                          help="Directory for the skills flywheel (FileSkillRegistry): "
                                               "learned lessons are rendered into every prompt and a "
                                               "new one is written back on DONE. Persists across runs "
                                               "— point successive runs at the same dir to compound."),
        skills_distiller: str | None = typer.Option(None, "--skills-distiller",
                                                    help="Command that distils a solved run's diff into "
                                                         "a reusable lesson (ShellDistiller); its stdout "
                                                         "is the skill guidance. Omit for provenance-only "
                                                         "default distillation."),
        validate: str | None = typer.Option(None, "--validate",
                                            help="A pre-loop check run BEFORE the agent. Exit 0 = "
                                                 "proceed; non-zero = abort without running the loop "
                                                 "(its output is the reason). Use to confirm the goal "
                                                 "still reproduces / is still accurate — so a stale or "
                                                 "already-fixed task never spends a run.")) -> None:
    """Run the loop until it reaches a terminal. Point it at any repo via `repo` (or `--repo`).

    The CI deployment tier (Phase 5c) rides this same single-loop path: `--from-event`/`--from-issue`
    source the goal from a forge issue (so an Actions/GitLab job is an issue→PR worker with no
    cluster), and `--open-pr` flips on push + a draft PR for that one invocation without editing the
    repo's `loopkit.toml`. A `pull_request_review` event with changes requested makes it a **revise
    run**: the goal is the review feedback and the run resumes the PR's own head branch, so pushing
    updates the existing PR — the loop follows through on its PRs instead of stopping at "opened".
    Everything else — the gates, the protected-path guard, the budget stop — applies unchanged; in
    CI the ephemeral runner supplies the sandbox the cloud tier hand-builds.
    """
    # FIRST: load creds into memory + scrub them out of os.environ, before any subprocess (git, the
    # agent, the gates) can inherit them (Phase 5a credential hygiene). On a laptop with no creds dir
    # this reads from env; with none set it is a no-op.
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    cfg = _load(config)
    if repo is not None:
        cfg.repo = repo
    if branch is not None:
        cfg.branch = branch                     # per-run branch isolation; validated by preflight below
    if adapter is not None:
        cfg.agent.adapter = adapter
    if api_key:
        cfg.agent.use_api_key = True            # opt into the billed API key for claude-code this run
    # CI tier: source the goal from a forge event (issue or changes-requested review) or an issue
    # number. Mutually exclusive — two routes to the same thing. The issue number is captured so a
    # `Closes #N` lands in the PR; a revise event instead carries the PR branch the run must resume.
    issue_number: int | None = None
    if from_event is not None and from_issue is not None:
        fail("run", "pass only one of --from-event or --from-issue.")
    if from_event is not None:
        cfg.goal, issue_number, event_branch = _goal_from_event(from_event)
        if event_branch and branch is None:
            cfg.branch = event_branch           # revise: resume the PR's head branch (explicit --branch wins)
    elif from_issue is not None:
        cfg.goal, issue_number = _goal_from_issue(cfg.repo_path(), from_issue, provider)
    # `--open-pr` is a per-invocation override so the CI template is turnkey on a repo whose
    # loopkit.toml leaves [remote] off (the safe default). draft stays on (a human merges).
    if open_pr:
        cfg.remote.enabled = True
        cfg.remote.push = True
        cfg.remote.open_pr = True
    if max_iter is not None:
        cfg.stops.max_iter = max_iter
    trace.configure(cfg.trace)            # full-tree LangSmith tracing, auto-on (Ch 14-15)

    if sandbox:
        _run_sandboxed(cfg, config, dry_run=dry_run, max_iter=max_iter, force=force, branch=branch)
        return

    pf = safety.preflight(cfg)
    if not pf.ok and not force:
        for problem in pf.problems:
            err.print(f"[red]preflight[/] {problem}")
        err.print("Fix these or pass [bold]--force[/]  (see [bold]loopkit doctor[/]).")
        raise typer.Exit(1)

    # Pre-loop validation (opt-in): confirm the goal is worth running BEFORE charging the agent — that
    # it still reproduces and is still accurate against the current tree. A non-zero exit aborts the
    # run (exit 3, distinct from a loop failure) so a stale or already-fixed task spends nothing.
    if validate:
        import subprocess
        env = {**secrets.current().child_env(), "PYTHONDONTWRITEBYTECODE": "1"}
        vproc = subprocess.run(validate, cwd=cfg.repo_path(), shell=True, env=env,
                               capture_output=True, text=True)
        if vproc.stdout:
            console.print(vproc.stdout.rstrip())
        if vproc.returncode != 0:
            fail_out = ((vproc.stdout or "") + (vproc.stderr or "")).strip()[-500:]
            err.print(f"[yellow]validate[/] aborted before the loop: {escape(fail_out) or 'non-zero exit'}")
            raise typer.Exit(3)
        console.print("[green]validate[/] goal reproduces + is current — proceeding")

    # Gate-determinism preflight (opt-in): a gate that flips verdict on an unchanged tree corrupts
    # every stop decision the loop makes (Ch 9). Run it N times on the initial tree before charging
    # the agent; refuse on disagreement. 0/1 = skip = exact prior behavior.
    runs = check_gate if check_gate is not None else cfg.safety.gate_stability_runs
    if runs and runs >= 2:
        from ..gate import ShellGate
        stab = safety.gate_stability(ShellGate(cfg.gate.iteration), cfg.repo_path(), runs)
        if not stab.deterministic and not force:
            fail("preflight", f"iteration gate is non-deterministic: {runs} runs on an "
                              f"unchanged tree gave {stab.passes} pass / {runs - stab.passes} fail. A flaky "
                              f"gate corrupts every stop decision — fix the gate, or pass [bold]--force[/].")
        console.print(f"[green]gate[/] deterministic over {runs} runs")

    try:
        agent = build_agent(cfg.agent)
    except ValueError as exc:
        fail("run", escape(str(exc)))
    console.print(Panel.fit(
        f"[bold]{cfg.goal}[/]\nrepo {cfg.repo} · branch {cfg.branch} · adapter {cfg.agent.adapter} · "
        f"budget ${cfg.agent.max_cost_usd}"
        + (f" · issue #{issue_number}" if issue_number is not None else ""),
        title="loopkit run"))
    # Review is opt-out: an explicit --review wins, else the configured [review] command, else the
    # BUILT-IN default judge (unless --no-review). Announce the decision up front so an
    # accidentally-off review (the failure mode that let review fire in zero of 28 runs) is
    # impossible to miss — and name the judge identity when the default runs, so on-by-default is
    # never an anonymous spend.
    review_hook = None
    decision = cfg.review.decide(override=review, disabled=no_review)
    console.print(
        f"[bold]review:[/] {'[green]on[/]' if decision.on else '[yellow]off[/]'} — {decision.reason}"
        + (f" [dim]· {escape(_review_detail(decision, cfg))}[/]" if decision.on else ""))
    if decision.kind == "command":
        from ..extensions.review import ShellReviewHook
        review_hook = ShellReviewHook(decision.command)
    elif decision.kind == "default":
        from ..extensions.judge import DefaultReviewHook, resolve_judge
        # Probe the judge binary NOW: with review behind the green gate, a missing backend would
        # otherwise surface only at the first plausibly-done tick — after the agent has already
        # billed real work — as a REVIEW_UNAVAILABLE halt.
        target = resolve_judge(cfg.review, cfg.agent)
        if target.backend in _CLI_BINARIES and shutil.which(_CLI_BINARIES[target.backend]) is None:
            fail("review", f"judge backend '{_CLI_BINARIES[target.backend]}' not on PATH and review "
                           "is on by default — fix PATH, set [review] backend/command, or pass "
                           "--no-review.")
        # Construct BEFORE run_loop: the hook captures the repo's pre-run HEAD as the diff fork point.
        review_hook = DefaultReviewHook(cfg.review, cfg.agent, cfg.repo_path(), cfg.goal,
                                        plan_file=cfg.plan.file)
    skills_registry = None
    if skills:
        from ..extensions.skills import FileSkillRegistry, ShellDistiller
        distiller = ShellDistiller(skills_distiller) if skills_distiller else None
        skills_registry = FileSkillRegistry(skills, distill=distiller)
    result = run_loop(cfg, agent, dry_run=dry_run, review_hook=review_hook, skills=skills_registry)
    _render(result)
    if result.plan_total:      # plan-driven backlog: show how much of the checklist landed
        console.print(f"[dim]checklist[/] {result.plan_total - result.plan_open}/{result.plan_total} "
                      f"items done")
    # Outward edge (Ch 16): push the solved branch + open a PR, only if [remote] is enabled (which
    # --open-pr turns on). When the run was issue-sourced, the issue number rides into the PR body so
    # the forge auto-closes it on merge.
    if not dry_run and result.reason is StopReason.DONE and cfg.remote.enabled:
        from ..extensions.remote import sync_done
        sync = sync_done(cfg, cfg.repo_path(), title=_pr_title(cfg.goal), issue=issue_number)
        if sync["pushed"]:
            console.print(f"[green]pushed[/] {cfg.branch} → {cfg.remote.name}")
        if sync["pr_url"]:
            console.print(f"[green]opened PR[/] {sync['pr_url']}")
    raise typer.Exit(0 if result.reason is StopReason.DONE else 2)


def _pr_title(goal: str) -> str:
    """A single-line PR title from a goal (which may be a multi-line issue title+body)."""
    first = next((line for line in goal.splitlines() if line.strip()), "")
    return f"loopkit: {first.strip()[:72]}" if first else "loopkit: automated change"


def _goal_from_event(path: Path) -> tuple[str, int | None, str | None]:
    """Read a forge event JSON (Actions $GITHUB_EVENT_PATH / GitLab CI) → (goal, issue number, branch).

    Reuses the webhook path's parsers via `triggers.parse_event_payload` (forge auto-detected from the
    body), so the CI goal-building is identical to a webhook-triggered run. Two event kinds:

    - an **issue** → (issue goal, its number for `Closes #N`, no branch — the run makes its own);
    - a **changes-requested review** → (revise goal, no issue number — the PR already links its
      issue, and closing it from a revise would be wrong — and the PR's head branch to resume).
      The `loopkit/` branch-prefix guard applies here exactly as in `should_trigger`: the loop
      revises only PRs it authored, regardless of which tier delivered the event.

    Exits cleanly when the file is unreadable or holds no actionable event (a `workflow_dispatch`,
    a closed issue, an approving review).
    """
    from ..extensions import triggers
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail("run", f"could not read --from-event {path}: {escape(str(exc))}")
    event = triggers.parse_event_payload(payload)
    if event is None:
        fail("run", f"--from-event {path} carries no actionable issue or changes-requested review "
                    "(not an issue/review event, or a closed/approved/edited action).")
    if event.kind == "revise":
        if not event.branch.startswith(triggers.REVISE_BRANCH_PREFIX):
            fail("run", f"revise refused: PR branch '{escape(event.branch)}' is not a loopkit branch "
                        f"(expected prefix '{triggers.REVISE_BRANCH_PREFIX}') — the loop only revises "
                        "PRs it authored.")
        return triggers.revise_goal(event), None, event.branch
    goal = f"{event.title}\n\n{event.body}".strip() if event.body else event.title
    return goal or f"Resolve issue #{event.issue_number}", event.issue_number, None


def _goal_from_issue(repo: Path, number: int, provider: str) -> tuple[str, int]:
    """Fetch one issue by number via gh/glab and build (goal, issue number). CI tier / local convenience."""
    from ..extensions import issues
    issue = issues.fetch_issue(repo, number, provider=provider)
    if issue is None:
        fail("run", f"could not fetch issue #{number} (provider {provider}) — "
                    "is gh/glab installed + authenticated, and is the repo a github/gitlab remote?")
    task = issues.issue_to_task(issue)                    # reuse the shared goal builder
    return task["goal"], number


@app.command()
def review(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c",
                                       help="Config to derive the judge from; when the file is "
                                            "missing, ad-hoc defaults apply (claude-code)."),
           backend: str | None = typer.Option(None, "--backend",
                                              help="Judge backend override (claude-code | codex | "
                                                   "claude-api | openai-api)."),
           model: str | None = typer.Option(None, "--model", help="Judge model override."),
           criteria: list[Path] = typer.Option([], "--criteria",
                                               help="Extra rubric file(s) layered onto the bundled "
                                                    "checklist (repeatable)."),
           base: str | None = typer.Option(None, "--base",
                                           help="Diff base ref (default: the last commit)."),
           goal: str | None = typer.Option(None, "--goal",
                                           help="What the change was supposed to accomplish."),
           repo: Path = typer.Option(Path("."), "--repo", help="Repository to review.")) -> None:
    """Run the built-in judge ONCE on the repo's current change (exit 0 APPROVE · 1 REJECT ·
    2 judge unavailable).

    The same implementation the loop runs on every plausibly-done tick, standalone — for judging a
    diff ad hoc, debugging a judge decision by hand, or wiring as an explicit `[review] command`
    (`loopkit review` exits non-zero on problems, which is the ShellReviewHook contract).
    """
    from ..config import AgentConfig, ReviewConfig
    from ..extensions.judge import resolve_judge, run_judge
    from ..gate import ReviewUnavailable

    if Path(config).is_file():
        cfg = load_config(config)
        review_cfg, agent_cfg, goal_text = cfg.review, cfg.agent, goal or cfg.goal
    else:
        # Ad hoc, no config: default the judge to claude-code — NOT AgentConfig's `mock` default,
        # which would auto-approve everything and make the verb a rubber stamp.
        review_cfg, agent_cfg = ReviewConfig(), AgentConfig(adapter="claude-code")
        goal_text = goal or "Review this change for real defects."
    overrides = {k: v for k, v in (("backend", backend), ("model", model)) if v is not None}
    if criteria:
        overrides["criteria"] = [str(f) for f in criteria]
    if overrides:
        review_cfg = review_cfg.model_copy(update=overrides)
    target = resolve_judge(review_cfg, agent_cfg)
    texts = []
    for name in review_cfg.criteria:
        path = Path(name) if Path(name).is_absolute() else Path(repo) / name
        if not path.is_file():
            err.print(f"[red]review[/] criteria file missing: {name} (fail-closed)")
            raise typer.Exit(2)
        texts.append(path.read_text(encoding="utf-8", errors="replace"))
    console.print(f"[bold]judge:[/] {target.backend}/{target.model or 'backend default'} "
                  f"· base {base or 'HEAD~1'}")
    try:
        verdict = run_judge(Path(repo), target=target, goal=goal_text,
                            commit_message="(ad-hoc `loopkit review`)", base=base,
                            extra_criteria=tuple(texts))
    except ReviewUnavailable as exc:
        err.print(f"[red]review unavailable[/] {escape(secrets.redact(str(exc)))}")
        raise typer.Exit(2)
    if verdict.raw:
        console.print(escape(secrets.redact(verdict.raw)))
    if verdict.passed:
        console.print(f"[green]VERDICT: APPROVE[/] [dim]· cost ${verdict.cost_usd:.4f}[/]")
        return
    console.print(f"[red]VERDICT: REJECT[/] — {escape(secrets.redact(verdict.reason))} "
                  f"[dim]· cost ${verdict.cost_usd:.4f}[/]")
    raise typer.Exit(1)


@app.command()
def measure(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
            repo: str | None = typer.Option(None, "--repo", help="Override the target repo (config `repo`)."),
            adapter: str | None = typer.Option(None, "--adapter", help="Override the configured agent adapter."),
            trials: int = typer.Option(5, "--trials", "-n", min=1,
                                       help="How many independent trials of the goal to run."),
            k: int | None = typer.Option(None, "--k", help="Largest k to report (default: trials)."),
            mode: str = typer.Option("clone", "--mode",
                                     help="How each trial materialises the repo: clone | copy."),
            max_iter: int | None = typer.Option(None, "--max-iter", help="Override stops.max_iter per trial."),
            from_issue: int | None = typer.Option(None, "--from-issue",
                                                  help="Source the goal from a forge issue (gh/glab), "
                                                       "same as `run` — so calibration measures the "
                                                       "REAL task, not the config's placeholder goal."),
            provider: str = typer.Option("auto", "--provider",
                                         help="Forge for --from-issue: auto | github | gitlab."),
            out: Path | None = typer.Option(None, "--out",
                                            help="Write the full JSON ReliabilityReport here.")) -> None:
    """Measure how *reliably* the loop solves a goal: run it N times, report pass^k and pass@k.

    `pass@k` (discovery, rises with k) is what `evolve` optimizes — *can* the loop do it. `pass^k`
    (reliability, falls with k) is the production question — does it succeed on *every* one of k
    independent attempts. Each trial is a full isolated `run_loop` graded by the held-out acceptance
    gate, so a trial counts as a pass only when that gate certifies DONE. The report carries the
    loopkit version + a harness signature + a timestamp — a number without its harness isn't a
    measurement.
    """
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    cfg = _load(config)
    if repo is not None:
        cfg.repo = repo
    if adapter is not None:
        cfg.agent.adapter = adapter
    if max_iter is not None:
        cfg.stops.max_iter = max_iter
    if from_issue is not None:
        # Parity with `run`: calibrate reliability against the real issue-sourced goal, not the
        # config's placeholder. Uses the (possibly --repo-overridden) repo so glab/gh runs there.
        cfg.goal, _ = _goal_from_issue(cfg.repo_path(), from_issue, provider)
    if not cfg.gate.acceptance:
        # pass^k is defined by the held-out oracle: "pass" == the acceptance gate certified DONE.
        # Without it there is nothing to measure reliability against.
        fail("measure", "needs a held-out [bold]gate.acceptance[/] — pass^k is the rate at "
                        "which that gate certifies the goal. Set it in loopkit.toml.")
    trace.configure(cfg.trace)

    from ..extensions.fleet import make_repo_runner
    from ..extensions.measure import measure_reliability
    # Resolve the target to an absolute path before handing it to the runner: each trial clones into
    # its own temp scratch (a different cwd), so a relative `repo` (the default `repo = "."`) would
    # `git clone .` from the empty scratch dir and fail every trial. `run`/`doctor` use repo_path()
    # for the same reason — measure must match, or it silently reports pass^k=0 for a solvable goal.
    repo_src = str(cfg.repo_path())
    runner = make_repo_runner(
        repo_src, mode=mode, adapter=cfg.agent.adapter, max_iter=cfg.stops.max_iter,
        gate_iteration=cfg.gate.iteration, gate_acceptance=cfg.gate.acceptance,
        protected_paths=tuple(cfg.safety.protected_paths))   # remote stays off: trials never push
    harness_params = {"adapter": cfg.agent.adapter, "model": cfg.agent.model,
                      "gate_iteration": cfg.gate.iteration, "gate_acceptance": cfg.gate.acceptance,
                      "gate_regression": cfg.gate.regression, "max_iter": cfg.stops.max_iter,
                      "protected_paths": sorted(cfg.safety.protected_paths)}
    console.print(Panel.fit(
        f"[bold]{_pr_title(cfg.goal).removeprefix('loopkit: ')}[/]\nrepo {cfg.repo} · adapter "
        f"{cfg.agent.adapter} · model {cfg.agent.model} · [bold]{trials}[/] trials",
        title="loopkit measure"))

    with trace.span("loopkit measure", run_type="chain", tags=["loopkit", "measure"],
                    inputs={"goal": cfg.goal, "repo": cfg.repo},
                    metadata={"trials": trials, "adapter": cfg.agent.adapter}) as span:
        report = measure_reliability(
            runner, {"id": "measure", "goal": cfg.goal}, trials=trials, k_max=k,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            adapter=cfg.agent.adapter, model=cfg.agent.model, target=cfg.repo,
            harness_params=harness_params)
        span.outputs(successes=report.successes, pass_hat_1=report.success_rate,
                     pass_hat_k=report.pass_hat_k.get(trials), signature=report.harness.signature)

    console.print(_reliability_table(report))
    cpa = report.cost_per_accepted
    cpa_str = f"${cpa:.2f}/accepted" if cpa is not None else "—/accepted (none accepted)"
    console.print(f"[dim]harness loopkit {report.harness.loopkit_version} · sig "
                  f"{report.harness.signature} · {report.timestamp} · cost ${report.total_cost_usd:.2f} · "
                  f"{cpa_str}[/]")
    if out is not None:
        out.write_text(report.to_json())
        console.print(f"[green]wrote[/] {out}")
    raise typer.Exit(0)


def _reliability_table(report) -> Table:
    """The pass^k / pass@k curve — reliability falling, discovery rising, side by side."""
    table = Table(title=f"reliability — {report.trials} trials, {report.successes} passed "
                        f"(pass^1 = {report.success_rate:.0%})")
    table.add_column("k", justify="right")
    table.add_column("pass^k  (reliability ↓)", justify="right")
    table.add_column("pass@k  (discovery ↑)", justify="right")
    for kk in sorted(report.pass_hat_k):
        table.add_row(str(kk), f"{report.pass_hat_k[kk]:.3f}", f"{report.pass_at_k[kk]:.3f}")
    return table


@app.command("synth-gate")
def synth_gate(oracle: str | None = typer.Argument(None,
                    help="The proposed held-out acceptance command to verify. Omit to verify the "
                         "gate.acceptance already declared in --config."),
               config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c",
                    help="loopkit.toml — sources the default oracle + target when they aren't passed."),
               repo: Path | None = typer.Option(None, "--repo", "-C",
                    help="Repo/tree to verify against (default: the config's repo, else the CWD)."),
               fix: str | None = typer.Option(None, "--fix",
                    help="A reference fix: a command that transitions a COPY of the tree from "
                         "buggy→fixed (e.g. `git apply fix.patch`, `git checkout fixed -- .`). When "
                         "given, ALSO assert the oracle PASSES after it — the gold fail→pass check that "
                         "proves the oracle discriminates buggy-from-fixed, run in an isolated copy."),
               isolate: bool = typer.Option(False, "--isolate",
                    help="Run the fail-first check in a throwaway copy too (default: in place). Use for "
                         "an untrusted/goal-derived oracle (CI) that must not touch or litter the real "
                         "tree. A --fix already isolates."),
               mode: str = typer.Option("copy", "--mode",
                    help="How the isolated copy is materialized: copy (working tree, incl. uncommitted) "
                         "| clone (committed state)."),
               probe: str | None = typer.Option(None, "--probe",
                    help="Env-liveness probe: a trivial GUARANTEED-PASS command through the oracle's "
                         "own runner (e.g. its test runner on an always-green selection). Runs in the "
                         "same tree as fail-first (inside the isolated copy when isolating); if even "
                         "the probe can't pass, the env is broken — the verdict records env-broken and "
                         "fail-first is skipped, because an env failure exits non-zero exactly like a "
                         "genuine reproduction. Omit ⇒ env_live: null (unprobed)."),
               out: Path | None = typer.Option(None, "--out",
                    help="Write the full JSON OracleVerdict here — the auditable provenance record.")) -> None:
    """Verify a proposed held-out oracle is *real*: fail-first, and (with --fix) fail→pass.

    Proposing an acceptance test is easy; proving it certifies anything is the job. This runs the
    oracle against the current (buggy) tree and asserts it **FAILS** — an oracle that already passes
    reproduces nothing and would certify DONE on tick zero. With `--fix`, it also applies a reference
    fix to an isolated copy and asserts the oracle **PASSES** (SWE-bench's FAIL_TO_PASS validation):
    a gate that never flips certifies as little as one that always passes. Only a gate that clears
    every check is **blessed** — with a signature + version + timestamp, so the blessing is auditable.

    Generalizes the `run --validate` pre-loop seam into a first-class "is this oracle real?" check —
    the load-bearing half of oracle synthesis (Part IV, Layer 2). Exit 0 = blessed; exit 3 = not real.
    """
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    cfg = load_config(config) if config.exists() else None
    the_oracle = oracle or (cfg.gate.acceptance if cfg else None)
    if not the_oracle:
        fail("synth-gate", "no oracle to verify — pass one as an argument, or set a held-out "
                           "[bold]gate.acceptance[/] in loopkit.toml.")
    if repo is not None:
        target = repo.expanduser().resolve()
    elif cfg is not None:
        target = cfg.repo_path()
    else:
        target = Path.cwd()
    if cfg is not None:
        trace.configure(cfg.trace)

    from ..extensions.synth_gate import verify_oracle

    console.print(Panel.fit(
        f"[bold]{escape(the_oracle)}[/]\ntree {escape(str(target))} · "
        f"{'fail→pass (isolated)' if fix else ('fail-first, isolated' if isolate else 'fail-first, in place')}",
        title="loopkit synth-gate"))

    with trace.span("loopkit synth-gate", run_type="chain", tags=["loopkit", "synth-gate"],
                    inputs={"oracle": the_oracle, "target": str(target)},
                    metadata={"has_fix": fix is not None, "isolate": isolate}) as span:
        verdict = verify_oracle(
            the_oracle, target, timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            fix=fix, mode=mode, isolate=isolate, probe=probe)
        span.outputs(blessed=verdict.blessed, checks=len(verdict.checks), signature=verdict.signature,
                     env_live=verdict.env_live)

    console.print(_oracle_verdict_table(verdict))
    for check in verdict.checks:
        mark = "[green]✓[/]" if check.ok else "[red]✗[/]"
        console.print(f"{mark} [bold]{check.name}[/] — {escape(check.detail)}")
        # Always surface the fail-first output — even when it passes, the molder must eyeball *why* it
        # fails (a real assertion about the target, not a broken import/path that fails for free). For
        # pass-on-fix the output only matters on a surprise (it still failed / the fix errored).
        show = check.evidence and (check.name == "fail-first" or not check.ok)
        if show:
            note = "confirm this is the target failing, not a broken oracle" if check.ok else "the surprise"
            console.print(Panel(escape(check.evidence), title=f"{check.name} output — {note}",
                                border_style="dim"))
    if verdict.blessed:
        console.print(f"[green]blessed[/] — the oracle is real. [dim]sig {verdict.signature} · loopkit "
                      f"{verdict.loopkit_version} · {verdict.timestamp}[/]")
    else:
        console.print("[red]not blessed[/] — this oracle certifies nothing yet; fix it above before "
                      "wiring it as [bold]gate.acceptance[/].")
    if out is not None:
        out.write_text(verdict.to_json())
        console.print(f"[green]wrote[/] {out}")
    raise typer.Exit(0 if verdict.blessed else 3)


def _oracle_verdict_table(verdict) -> Table:
    """The verification checks side by side: what a real oracle does at each stage vs. what happened."""
    table = Table(title=f"oracle verification — {'blessed' if verdict.blessed else 'NOT blessed'}")
    table.add_column("check", justify="left")
    table.add_column("a real oracle…", justify="left")
    table.add_column("this oracle", justify="left")
    for check in verdict.checks:
        got = "[green]met[/]" if check.ok else "[red]did not[/]"
        table.add_row(check.name, f"must {check.expected}", got)
    return table


@app.command()
def detect(repo: Path = typer.Argument(Path("."), help="Repository to introspect."),
           write: bool = typer.Option(False, "--write",
                    help="Write the proposed loopkit.toml into REPO (default: print it, decide "
                         "nothing). Never overwrites an existing config without --force."),
           force: bool = typer.Option(False, "--force",
                    help="With --write, overwrite an existing loopkit.toml."),
           out: Path | None = typer.Option(None, "--out",
                    help="Write the JSON RepoProfile here — the auditable detection record (for the "
                         "unattended tier or a copilot to consume).")) -> None:
    """Deterministically read a repo's mechanical, safety-critical config → a proposed loopkit.toml.

    Molding is a copilot's judgment job — *except* the parts where a guess is dangerous: the test
    command (a wrong one wastes a run) and which paths are protected (a wrong one lets the loop weaken
    its own held-out gate or churn a migration). Those are readable off file markers, at zero tokens,
    so `detect` reads them: the test runner (pytest/npm/go/cargo/make) → `[gate].iteration`, the
    protected-path candidates (test dir, CI, charts, migrations, lockfiles) → `[safety]`, the default
    branch → `forbid_branches`, and the agent on PATH → `[agent].adapter`. Each fact carries its
    evidence, so the proposal is auditable.

    It **proposes, it does not decide.** By default it prints the config for the molder (copilot or
    human) to refine; `--write` is the quick no-copilot path. It deliberately leaves the two things no
    marker can tell it — the goal and the held-out acceptance oracle (author + `synth-gate` it) — as
    annotated placeholders. The load-bearing determinism of Part IV Layer 3; the judgment stays yours.
    """
    from ..extensions.detect import detect_repo

    target = repo.expanduser().resolve()
    if not target.exists():
        fail("detect", f"no such path: {escape(str(target))}")
    profile = detect_repo(target)

    console.print(Panel.fit(f"[bold]{escape(str(target))}[/]\n"
                            f"test runner {escape(profile.test_command or '— none found')} · adapter "
                            f"{profile.adapter} · default branch {profile.default_branch or '—'} · "
                            f"{len(profile.protected_paths)} protected path(s)",
                            title="loopkit detect"))
    console.print(_detect_table(profile))
    toml = profile.to_toml()
    console.print(Panel(escape(toml), title="proposed loopkit.toml", border_style="dim"))

    if write:
        dest = target / "loopkit.toml"
        if dest.exists() and not force:
            fail("detect", f"{escape(str(dest))} already exists — pass [bold]--force[/] to overwrite, "
                           "or drop --write to just print the proposal.")
        dest.write_text(toml)
        console.print(f"[green]wrote[/] {escape(str(dest))} — refine the goal + acceptance oracle, then "
                      "[bold]loopkit doctor[/].")
    else:
        console.print("[dim]proposal only — refine it, or re-run with [bold]--write[/] to save it.[/]")
    if out is not None:
        out.write_text(profile.to_json())
        console.print(f"[green]wrote[/] {escape(str(out))}")
    raise typer.Exit(0)


def _detect_table(profile) -> Table:
    """The audit trail: every detected fact, its value, the evidence that decided it, and confidence."""
    colors = {"high": "green", "medium": "yellow", "low": "yellow", "none": "red"}
    table = Table(title="detected", show_header=True, header_style="bold")
    table.add_column("fact")
    table.add_column("value", overflow="fold")
    table.add_column("confidence")
    table.add_column("evidence", overflow="fold")
    for d in profile.detections:
        color = colors.get(d.confidence, "dim")
        table.add_row(d.key, escape(d.value or "—"), f"[{color}]{d.confidence}[/]", escape(d.evidence))
    return table


@app.command()
def route(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c"),
          repo: str | None = typer.Option(None, "--repo", help="Override the target repo (config `repo`)."),
          adapter: str | None = typer.Option(None, "--adapter", help="Override the configured agent adapter."),
          trials: int = typer.Option(5, "--trials", "-n", min=1,
                                     help="Trials to calibrate with (ignored with --from-report)."),
          k: int | None = typer.Option(None, "--k",
                                       help="Reliability bar: pass^k. Default 1 = the single-run success "
                                            "rate c/n (graded). Raise it to demand k independent runs all "
                                            "pass (the production bar); k = trials is degenerate (all-or-none)."),
          threshold: float = typer.Option(0.9, "--threshold", min=0.0, max=1.0,
                                          help="Route single iff pass^k ≥ this; below it, escalate to evolve."),
          mode: str = typer.Option("clone", "--mode", help="How each trial materialises the repo: clone | copy."),
          max_iter: int | None = typer.Option(None, "--max-iter", help="Override stops.max_iter per trial."),
          from_issue: int | None = typer.Option(None, "--from-issue",
                                                help="Calibrate against a forge issue's goal (gh/glab), as `measure`."),
          provider: str = typer.Option("auto", "--provider", help="Forge for --from-issue: auto | github | gitlab."),
          from_report: Path | None = typer.Option(None, "--from-report",
                                                  help="Decide over a saved `measure --out` JSON report "
                                                       "instead of calibrating — the free, no-run path "
                                                       "(re-route under a new --threshold for nothing)."),
          out: Path | None = typer.Option(None, "--out",
                                          help="Write the full JSON RouteDecision here (the provenance record).")) -> None:
    """Turn a reliability measurement into a run strategy: single run, or escalate to evolve.

    The mechanical half of feature-routing (Part IV, Layer 4). It reads how *reliably* the loop solves a
    goal (`pass^k`, via `measure`) and applies the rule the `loopkit-mold` skill routes through: **at or
    above the threshold, run once**; **below it, escalate to `evolve`** (best-of-N + held-out
    re-validation), with the population sized from the single-shot rate so a fan-out is only as big as the
    task needs. A `pass^1` of 0 is flagged honestly — escalation can't manufacture a capability the loop
    has never once shown; fix the goal/gates/oracle or the model instead.

    Calibrating runs `--trials` real trials (it costs what `measure` costs); pass `--from-report` to decide
    over a report you already have for free. **Advisory** — it prints the strategy and the exact command,
    never launching an (expensive) evolve itself. Emits an auditable `RouteDecision` tied to the
    measurement's harness signature.
    """
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from ..extensions.route import decide_route, route_from_report

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if from_report is not None:
        # The free path: decide over an existing measurement, no runs.
        try:
            report = json.loads(from_report.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            fail("route", f"could not read --from-report {from_report}: {escape(str(exc))}")
        try:
            decision = route_from_report(report, timestamp=ts, threshold=threshold, k=k)
        except ValueError as exc:
            fail("route", escape(str(exc)))
        goal = report.get("goal", "")
        console.print(Panel.fit(f"[bold]{escape(_pr_title(goal).removeprefix('loopkit: '))}[/]\n"
                                f"from report {escape(str(from_report))} · {report.get('trials', '?')} "
                                f"trials · threshold {threshold}", title="loopkit route"))
    else:
        # Calibrate inline, then decide — the turnkey path (reuses the `measure` machinery exactly).
        cfg = _load(config)
        if repo is not None:
            cfg.repo = repo
        if adapter is not None:
            cfg.agent.adapter = adapter
        if max_iter is not None:
            cfg.stops.max_iter = max_iter
        if from_issue is not None:
            cfg.goal, _ = _goal_from_issue(cfg.repo_path(), from_issue, provider)
        if not cfg.gate.acceptance:
            fail("route", "needs a held-out [bold]gate.acceptance[/] to calibrate against — pass^k is "
                          "the rate at which that gate certifies the goal. Set it, or pass --from-report.")
        trace.configure(cfg.trace)
        report = _calibrate_report(cfg, trials=trials, mode=mode, k=k, timestamp=ts)
        console.print(_reliability_table(report))
        decision = route_from_report(report.to_dict(), timestamp=ts, threshold=threshold, k=k)

    console.print(_route_panel(decision))
    if out is not None:
        out.write_text(decision.to_json())
        console.print(f"[green]wrote[/] {escape(str(out))}")
    # Exit 0 either way — a decision is not a failure; the strategy is the signal. (Mirrors `detect`.)
    raise typer.Exit(0)


def _calibrate_report(cfg: Config, *, trials: int, mode: str, k: int | None, timestamp: str):
    """Run `trials` isolated trials of the goal → a ReliabilityReport (the same setup `measure` uses)."""
    from ..extensions.fleet import make_repo_runner
    from ..extensions.measure import measure_reliability
    repo_src = str(cfg.repo_path())                       # absolute — each trial clones into its own scratch
    runner = make_repo_runner(
        repo_src, mode=mode, adapter=cfg.agent.adapter, max_iter=cfg.stops.max_iter,
        gate_iteration=cfg.gate.iteration, gate_acceptance=cfg.gate.acceptance,
        protected_paths=tuple(cfg.safety.protected_paths))
    harness_params = {"adapter": cfg.agent.adapter, "model": cfg.agent.model,
                      "gate_iteration": cfg.gate.iteration, "gate_acceptance": cfg.gate.acceptance,
                      "gate_regression": cfg.gate.regression, "max_iter": cfg.stops.max_iter,
                      "protected_paths": sorted(cfg.safety.protected_paths)}
    return measure_reliability(runner, {"id": "route", "goal": cfg.goal}, trials=trials, k_max=k,
                               timestamp=timestamp, adapter=cfg.agent.adapter, model=cfg.agent.model,
                               target=cfg.repo, harness_params=harness_params)


def _route_panel(decision) -> Panel:
    """The routing verdict: the strategy, the reason, and the exact command to run."""
    color = "yellow" if decision.escalated else "green"
    head = "[yellow]escalate → evolve[/]" if decision.escalated else "[green]single run[/]"
    body = [f"strategy: {head}",
            f"pass^{decision.k} = {decision.pass_hat_k:.2f}  (threshold {decision.threshold:.2f}) · "
            f"{decision.successes}/{decision.trials} trials passed",
            "",
            decision.reason,
            "",
            f"run: [bold]{escape(decision.command)}[/]"]
    return Panel("\n".join(body), title=f"loopkit route — {decision.strategy}", border_style=color)


@app.command()
def demo(chapter: int | None = typer.Argument(None, help="Course chapter number, e.g. 9. Omit to list."),
         live: bool = typer.Option(False, "--live", help="Use the real claude-code agent where supported.")) -> None:
    """Run a chapter's scenario straight through (the loop's logs are the play-by-play)."""
    if chapter is None:
        _list_scenarios()
        return
    scenarios.play(chapter, console, live=live, pause=False)


@app.command()
def learn(chapter: int | None = typer.Argument(None, help="Course chapter number. Omit to list."),
          live: bool = typer.Option(False, "--live", help="Use the real claude-code agent where supported.")) -> None:
    """Walk a chapter's scenario with narration and a pause between beats."""
    if chapter is None:
        _list_scenarios()
        return
    scenarios.play(chapter, console, live=live, pause=True)


@app.command()
def executor(
        socket_path: str = typer.Option(..., "--socket", envvar="LOOPKIT_EXECUTOR_SOCKET",
                                         help="Unix socket to listen on (shared emptyDir with loopkit-core)."),
) -> None:
    """Run the keyless tool-execution sidecar (Part III, Phase 6 — agent isolation).

    This is the untrusted half of the cloud worker pod: it serves the agent's `run_bash`/`read`/`write`
    tool calls and the held-out gate over the socket, against the **shared workspace** — but in a
    container running as a **different uid / PID namespace with no credential mount**. loopkit-core (the
    `fleet worker` process) holds the key for the LLM call + git and dispatches every model-chosen
    command here, so there is no key in this process to read or exfiltrate.

    Deliberately does **not** call `secrets.install` — the executor must never load a credential.
    """
    import signal

    from ..executor import serve
    console.print(Panel.fit(f"keyless executor · socket {socket_path}\n"
                            "serving run_bash / read / write / gate for loopkit-core (no credentials)",
                            title="loopkit executor"))
    # The kubelet terminates a native sidecar with SIGTERM when loopkit-core exits — translate it to
    # the clean-shutdown path so the socket is unlinked and a stop is logged (graceful termination).
    def _terminate(*_args) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        serve(socket_path)
    except KeyboardInterrupt:
        console.print("[yellow]executor stopped[/]")


def _list_scenarios() -> None:
    table = Table(title="loopkit scenarios", header_style="bold")
    table.add_column("chapter")
    table.add_column("topic")
    table.add_column("teaches", overflow="fold")
    table.add_column("mode")
    for scenario in scenarios.available():
        table.add_row(str(scenario.chapter), scenario.title, scenario.teaches,
                      "live or scripted" if scenario.live_supported else "scripted")
    console.print(table)
    console.print("Run one with [bold]loopkit demo <chapter>[/] or [bold]loopkit learn <chapter>[/].")


def _run_sandboxed(cfg: Config, config_path: Path, *, dry_run: bool, max_iter: int | None,
                   force: bool, branch: str | None = None) -> None:
    """Re-invoke `loopkit run` inside the container, with the repo bind-mounted at /work (Ch 16)."""
    if shutil.which("docker") is None:
        fail("sandbox", "docker not found on PATH (build the image: docker build -t loopkit .)")
    repo = cfg.repo_path()
    inner = ["loopkit", "run", "-c", config_path.name]
    if dry_run:
        inner.append("--dry-run")
    if max_iter is not None:
        inner += ["--max-iter", str(max_iter)]
    if branch is not None:
        inner += ["--branch", branch]           # honor the per-run branch override inside the container too
    if force:
        inner.append("--force")
    cmd = ["docker", "run", "--rm", "-v", f"{repo}:/work", "-w", "/work", "loopkit", *inner[1:]]
    console.print(Panel.fit(" ".join(cmd), title="loopkit run --sandbox"))
    raise typer.Exit(subprocess.call(cmd))

