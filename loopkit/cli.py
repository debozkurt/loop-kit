"""loopkit command line — set up a loop (init), check it (doctor), run it (run).

Thin by design: the CLI validates and renders; all behaviour lives in the library modules so
the same loop is drivable from Python, a cron trigger, or the orchestration supervisor
(`extensions/orchestrate.py`) without going through argv. The `fleet` sub-app is the coordinator
+ worker entrypoints for the deployable fleet (Ch 12): `fleet worker` is the container entrypoint,
`fleet run`/`fleet evolve` drive the fleet over Redis. The `redis` import is deferred into those
commands, so the core CLI loads without the optional `[fleet]` dependency.
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
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import pricing, safety, scenarios, secrets, trace
from .agent import build_agent
from .config import Config, load_config
from .log import get_logger
from .loop import RunResult, run_loop
from .pricing import DEFAULT_MODELS
from .stops import StopReason

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="A self-governed coding loop you can point at any repository.")
fleet_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Run the loop as a queue-driven fleet of workers (Chapter 12).")
app.add_typer(fleet_app, name="fleet")
cloud_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Drive the loop on a managed Kubernetes cluster (Part III). "
                             "Pins the expected DOKS context and refuses any other.")
app.add_typer(cloud_app, name="cloud")
creds_app = typer.Typer(add_completion=False, no_args_is_help=True,
                        help="Register per-submitter agent/git credentials (Phase 5a). Keys are read "
                             "from the environment — never passed as an argument.")
cloud_app.add_typer(creds_app, name="creds")
console = Console()
err = Console(stderr=True)

DEFAULT_REDIS_URL = "redis://localhost:6379"

DEFAULT_CONFIG = Path("loopkit.toml")

_CONFIG_TEMPLATE = """\
# loopkit.toml — the whole loop as one object. Validate with `loopkit doctor`.
goal = "Describe exactly what 'done' means — the condition the loop drives toward."
repo = "."
branch = "loopkit/run"           # never main/master (Ch 16)

[agent]
adapter = "claude-code"          # mock | claude-code | codex | claude-api | openai-api
max_cost_usd = 5.0               # budget ceiling (Ch 14) — bites on real cost (see `doctor`)

[prompt]
anchors = ["PROMPT.md"]          # fixed context reloaded each tick (Ch 4-5)

[gate]
iteration = "python -m pytest tests/seen -q"      # fast, in-sample (Ch 6-7)
acceptance = "python -m pytest tests/holdout -q"  # held-out, run once before done (Ch 9)

[stops]
max_iter = 20                    # Ch 13
no_progress_after = 3

[safety]
protected_paths = ["tests/"]     # the loop may not touch these (Ch 9 + 16)
require_clean_tree = true
allow_branches = ["loopkit/*"]
"""

_PROMPT_TEMPLATE = """\
# Task

<Describe the goal and state the spec precisely.>

The visible tests are an incomplete check — passing them is necessary but not sufficient.
Make the behaviour correct. Do not weaken, delete, or skip any test.
"""

# CI deployment tier (Phase 5c): run the single loop from the forge's CI on a labelled issue, no
# cluster. The forge is the trigger, the secret store, the identity, and the per-job sandbox; loopkit
# is just the loop. These are the canonical templates `loopkit init --ci <forge>` scaffolds and
# `examples/ci/` mirrors — see docs/part-iii-ci-mode.md. Requires a loopkit.toml in the repo.
_CI_GITHUB_TEMPLATE = """\
# loopkit CI tier — turn a labelled issue into a draft PR, no cluster required.
# Setup: drop this at .github/workflows/loopkit.yml, add the repo secret ANTHROPIC_API_KEY, and keep
# a loopkit.toml in the repo (run `loopkit init`). Label an issue `loopkit` to dispatch the loop.
name: loopkit
on:
  issues:
    types: [opened, labeled]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number to run loopkit on
        required: true
permissions:
  contents: write          # push the loop's branch
  pull-requests: write     # open the draft PR
  issues: read             # read the issue (manual-dispatch path)
jobs:
  loopkit:
    # Act on issues carrying the `loopkit` label (the opt-in switch); always act on a manual run.
    if: github.event_name == 'workflow_dispatch' || contains(github.event.issue.labels.*.name, 'loopkit')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # full history so the loop can branch from + PR against the base
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: pip install 'loopkit[claude,remote]'
      - name: loopkit run (issue event)
        if: github.event_name == 'issues'
        run: loopkit run --from-event "$GITHUB_EVENT_PATH" --adapter claude-api --open-pr
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}   # a repo/org secret (per-repo keying)
          GH_TOKEN: ${{ github.token }}                          # scoped, ephemeral — pushes + opens the PR
      - name: loopkit run (manual dispatch)
        if: github.event_name == 'workflow_dispatch'
        run: loopkit run --from-issue "${{ inputs.issue }}" --adapter claude-api --open-pr
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GH_TOKEN: ${{ github.token }}
"""

# Subscription variant: `claude-code` billed to a Claude Code OAuth token, not a metered API key.
# Shipped as examples/ci/github-actions-claude-code.yml (a drift test keeps them identical).
_CI_GITHUB_CLAUDE_CODE_TEMPLATE = """\
# loopkit CI tier (Claude Code subscription) — a labelled issue → a draft PR, no cluster required.
# This variant bills your Claude Code SUBSCRIPTION via an OAuth token, not a metered API key.
# Setup:
#   1. Create the token:    claude setup-token
#   2. Add the repo secret: gh secret set CLAUDE_CODE_OAUTH_TOKEN   (do NOT set ANTHROPIC_API_KEY —
#      claude-code defaults to the subscription and withholds an API key)
#   3. Keep a loopkit.toml in the repo (run `loopkit init`). Label an issue `loopkit` to dispatch.
name: loopkit
on:
  issues:
    types: [opened, labeled]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number to run loopkit on
        required: true
permissions:
  contents: write          # push the loop's branch
  pull-requests: write     # open the draft PR
  issues: read             # read the issue (manual-dispatch path)
jobs:
  loopkit:
    # Act on issues carrying the `loopkit` label (the opt-in switch); always act on a manual run.
    if: github.event_name == 'workflow_dispatch' || contains(github.event.issue.labels.*.name, 'loopkit')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0     # full history so the loop can branch from + PR against the base
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: npm install -g @anthropic-ai/claude-code      # the agent binary (claude-code adapter)
      - run: pip install 'loopkit[remote]'                 # the CLI adapter needs no provider SDK
      - name: loopkit run (issue event)
        if: github.event_name == 'issues'
        run: loopkit run --from-event "$GITHUB_EVENT_PATH" --adapter claude-code --open-pr
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}   # subscription, not a billed key
          GH_TOKEN: ${{ github.token }}                                     # scoped, ephemeral — push + PR
      - name: loopkit run (manual dispatch)
        if: github.event_name == 'workflow_dispatch'
        run: loopkit run --from-issue "${{ inputs.issue }}" --adapter claude-code --open-pr
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          GH_TOKEN: ${{ github.token }}
"""

_CI_GITLAB_TEMPLATE = """\
# loopkit CI tier (GitLab) — run the loop on one issue, open a draft MR. No cluster required.
# GitLab has no native issue->pipeline trigger, so this fires on: a manual "Run pipeline" with an
# ISSUE_IID variable, a webhook -> trigger token, or a pipeline schedule. Add ANTHROPIC_API_KEY and a
# GITLAB_TOKEN (PAT, api scope) as masked CI/CD variables, and keep a loopkit.toml in the repo.
loopkit:
  image: python:3.13-slim
  rules:
    - if: '$CI_PIPELINE_SOURCE == "web" && $ISSUE_IID'         # manual run, pass ISSUE_IID
    - if: '$CI_PIPELINE_SOURCE == "trigger" && $ISSUE_IID'     # webhook -> trigger token
    - if: '$CI_PIPELINE_SOURCE == "schedule" && $ISSUE_IID'    # scheduled run of one issue
  script:
    - pip install 'loopkit[claude,remote]'                     # claude-api needs no binary in CI
    - loopkit run --from-issue "$ISSUE_IID" --provider gitlab --adapter claude-api --open-pr
  # GITLAB_TOKEN authenticates glab (issue fetch + MR) and the git push; ANTHROPIC_API_KEY pays.
"""

_CI_TEMPLATES = {"github": (".github/workflows/loopkit.yml", _CI_GITHUB_TEMPLATE),
                 "gitlab": (".gitlab-ci.yml", _CI_GITLAB_TEMPLATE)}


@app.command()
def init(path: Path = typer.Argument(Path("."), help="Repository to set up."),
         ci: str | None = typer.Option(None, "--ci",
                                        help="Also scaffold a CI workflow: github | gitlab (Phase 5c).")) -> None:
    """Scaffold a starter loopkit.toml and PROMPT.md in PATH (never overwrites).

    With `--ci github|gitlab`, also scaffold a CI workflow that runs the loop on a labelled issue with
    no cluster (the CI deployment tier) — see docs/part-iii-ci-mode.md.
    """
    path = path.expanduser().resolve()
    files = [("loopkit.toml", _CONFIG_TEMPLATE), ("PROMPT.md", _PROMPT_TEMPLATE)]
    if ci is not None:
        if ci not in _CI_TEMPLATES:
            err.print(f"[red]init[/] unknown --ci value {ci!r} (expected: github | gitlab).")
            raise typer.Exit(1)
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
    console.print("Next: edit the goal + gates, then [bold]loopkit doctor[/] to validate.")


@app.command()
def doctor(config: Path = typer.Option(DEFAULT_CONFIG, "--config", "-c")) -> None:
    """Pre-flight checks: is this repo safe to point the loop at? (Ch 16)"""
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
    else:
        table.add_row("acceptance gate", "[yellow]none[/]", "no held-out check (Ch 9)")

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
                                                help="Set the goal from a forge issue-event JSON "
                                                     "(Actions $GITHUB_EVENT_PATH / GitLab CI). CI tier."),
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
                                     help="Run the loop inside the loopkit Docker container (Ch 16).")) -> None:
    """Run the loop until it reaches a terminal. Point it at any repo via `repo` (or `--repo`).

    The CI deployment tier (Phase 5c) rides this same single-loop path: `--from-event`/`--from-issue`
    source the goal from a forge issue (so an Actions/GitLab job is an issue→PR worker with no
    cluster), and `--open-pr` flips on push + a draft PR for that one invocation without editing the
    repo's `loopkit.toml`. Everything else — the gates, the protected-path guard, the budget stop —
    applies unchanged; in CI the ephemeral runner supplies the sandbox the cloud tier hand-builds.
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
    # CI tier: source the goal from a forge issue (event JSON or a number). Mutually exclusive — they
    # are two routes to the same thing. The issue number is captured so a `Closes #N` lands in the PR.
    issue_number: int | None = None
    if from_event is not None and from_issue is not None:
        err.print("[red]run[/] pass only one of --from-event or --from-issue.")
        raise typer.Exit(1)
    if from_event is not None:
        cfg.goal, issue_number = _goal_from_event(from_event)
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

    # Gate-determinism preflight (opt-in): a gate that flips verdict on an unchanged tree corrupts
    # every stop decision the loop makes (Ch 9). Run it N times on the initial tree before charging
    # the agent; refuse on disagreement. 0/1 = skip = exact prior behavior.
    runs = check_gate if check_gate is not None else cfg.safety.gate_stability_runs
    if runs and runs >= 2:
        from .gate import ShellGate
        stab = safety.gate_stability(ShellGate(cfg.gate.iteration), cfg.repo_path(), runs)
        if not stab.deterministic and not force:
            err.print(f"[red]preflight[/] iteration gate is non-deterministic: {runs} runs on an "
                      f"unchanged tree gave {stab.passes} pass / {runs - stab.passes} fail. A flaky "
                      f"gate corrupts every stop decision — fix the gate, or pass [bold]--force[/].")
            raise typer.Exit(1)
        console.print(f"[green]gate[/] deterministic over {runs} runs")

    try:
        agent = build_agent(cfg.agent)
    except ValueError as exc:
        err.print(f"[red]run[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"[bold]{cfg.goal}[/]\nrepo {cfg.repo} · branch {cfg.branch} · adapter {cfg.agent.adapter} · "
        f"budget ${cfg.agent.max_cost_usd}"
        + (f" · issue #{issue_number}" if issue_number is not None else ""),
        title="loopkit run"))
    result = run_loop(cfg, agent, dry_run=dry_run)
    _render(result)
    # Outward edge (Ch 16): push the solved branch + open a PR, only if [remote] is enabled (which
    # --open-pr turns on). When the run was issue-sourced, the issue number rides into the PR body so
    # the forge auto-closes it on merge.
    if not dry_run and result.reason is StopReason.DONE and cfg.remote.enabled:
        from .extensions.remote import sync_done
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


def _goal_from_event(path: Path) -> tuple[str, int | None]:
    """Read a forge issue-event JSON (Actions $GITHUB_EVENT_PATH / GitLab CI) → (goal, issue number).

    Reuses the webhook path's parsers via `triggers.parse_event_payload` (forge auto-detected from the
    body), so the CI goal-building is identical to a webhook-triggered run. Exits cleanly when the file
    is unreadable or holds no actionable issue (a `workflow_dispatch`, a closed issue).
    """
    from .extensions import triggers
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        err.print(f"[red]run[/] could not read --from-event {path}: {escape(str(exc))}")
        raise typer.Exit(1)
    event = triggers.parse_event_payload(payload)
    if event is None:
        err.print(f"[red]run[/] --from-event {path} carries no actionable issue "
                  "(not an issue event, or a closed/edited action).")
        raise typer.Exit(1)
    goal = f"{event.title}\n\n{event.body}".strip() if event.body else event.title
    return goal or f"Resolve issue #{event.issue_number}", event.issue_number


def _goal_from_issue(repo: Path, number: int, provider: str) -> tuple[str, int]:
    """Fetch one issue by number via gh/glab and build (goal, issue number). CI tier / local convenience."""
    from .extensions import issues
    issue = issues.fetch_issue(repo, number, provider=provider)
    if issue is None:
        err.print(f"[red]run[/] could not fetch issue #{number} (provider {provider}) — "
                  "is gh/glab installed + authenticated, and is the repo a github/gitlab remote?")
        raise typer.Exit(1)
    task = issues.issue_to_task(issue)                    # reuse the shared goal builder
    return task["goal"], number


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
    if not cfg.gate.acceptance:
        # pass^k is defined by the held-out oracle: "pass" == the acceptance gate certified DONE.
        # Without it there is nothing to measure reliability against.
        err.print("[red]measure[/] needs a held-out [bold]gate.acceptance[/] — pass^k is the rate at "
                  "which that gate certifies the goal. Set it in loopkit.toml.")
        raise typer.Exit(1)
    trace.configure(cfg.trace)

    from .extensions.fleet import make_repo_runner
    from .extensions.measure import measure_reliability
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

    from .executor import serve
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


@fleet_app.command("worker")
def fleet_worker(
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL",
                                      help="Redis the worker drains tasks from."),
        adapter: str = typer.Option("mock", "--adapter",
                                    help="Agent the worker runs: mock (no tokens) | claude-code."),
        max_iter: int = typer.Option(6, "--max-iter", help="Per-task iteration cap."),
        target: str | None = typer.Option(None, "--target", envvar="LOOPKIT_TARGET",
                                          help="Target repo path/URL to operate on (default: bundled demo-repo)."),
        gate_iteration: str | None = typer.Option(None, "--gate-iteration", help="Override the iteration gate."),
        gate_acceptance: str | None = typer.Option(None, "--gate-acceptance", help="Override the acceptance gate."),
        redis_namespace: str = typer.Option("loopkit", "--redis-namespace", envvar="REDIS_NAMESPACE",
                                             help="Per-run Redis keyspace ({ns}:tasks/:results). Cloud sets this to the run id."),
        executor_socket: str | None = typer.Option(
            None, "--executor-socket", envvar="LOOPKIT_EXECUTOR_SOCKET",
            help="Dispatch the agent's tool calls + the held-out gate to a keyless executor sidecar "
                 "over this Unix socket (Phase 6). Unset = run them in-process (local/dev)."),
        skills_repo: str | None = typer.Option(
            None, "--skills-repo", envvar="LOOPKIT_SKILLS_REPO",
            help="A dedicated loopkit-skills git repo: clone its lessons into each prompt and push a "
                 "gated write-back on DONE (Phase 5b cross-run flywheel). Unset = no skills."),
        skills_branch: str = typer.Option("main", "--skills-branch", envvar="LOOPKIT_SKILLS_BRANCH",
                                          help="Branch of the skills repo to read/write (default: main)."),
        name: str = typer.Option("worker", "--name", envvar="WORKER_NAME",
                                 help="Worker name (rides logs as a tag; set from the pod name).")) -> None:
    """The executor: BRPOP a task, run the loop in an isolated clone of the target, HSET the result.

    Long-lived — runs as a pod or a host process. With no `--target` it runs the bundled demo-repo
    token-free (the `tilt up` smoke test). With `--target /path/or/url` it operates on YOUR repo:
    gates come from that repo's loopkit.toml unless overridden, and the repo's remote config there
    controls whether a solved branch is pushed + a PR opened. Use `--adapter claude-code` for real
    solves.

    `--executor-socket` is the Phase-6 agent-isolation seam: when set (the cloud worker pod), the
    agent's chosen commands + the held-out gate are dispatched to the keyless `loopkit executor`
    sidecar (a different uid / PID namespace, no credential) instead of running in this key-holding
    process. Unset, they run in-process (`LocalToolExecutor`) — exact prior behavior.
    """
    # FIRST: load the per-run creds into the in-process store and shred them out of os.environ, BEFORE
    # the first git clone inherits them (Phase 5a). The Phase-6 cloud pod delivers them via envFrom into
    # *this* (trusted) loopkit-core container — the untrusted run_bash/gate run in the keyless executor
    # sidecar, so there's nothing to shred there. A tmpfs dir is still honored if set (back-compat).
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    # Fail-closed (G7): in a cloud pod (LOOPKIT_CREDS_EXPECTED is set), an API adapter with no resolved
    # key is a misconfiguration — stop with a clear, attributable error rather than 401-ing deep in a tick.
    if (os.environ.get("LOOPKIT_CREDS_EXPECTED") and adapter in ("claude-api", "openai-api")
            and secrets.current().api_key(adapter) is None):
        err.print(f"[red]worker[/] no credential for adapter '{adapter}' — the per-run Secret had no "
                  f"{'/'.join(secrets.ADAPTER_KEYS.get(adapter, ()))}. Register: loopkit cloud creds set.")
        raise typer.Exit(1)
    from .extensions.fleet import RedisQueue, Worker, make_demo_runner, make_repo_runner
    trace.configure(None)                 # auto-on from env; each worker traces its own runs (Ch 12)
    queue = RedisQueue.from_url(redis_url, namespace=redis_namespace)
    # Phase 6: when a socket is configured, the agent's tool calls + the held-out gate run in the
    # keyless executor sidecar; loopkit-core (here) keeps the key for the LLM call + git. None = local.
    tool_executor = None
    if executor_socket:
        from .executor import RemoteToolExecutor
        # Group-writable umask so the executor sidecar (a different uid, same fsGroup) can edit files
        # loopkit-core clones — and loopkit-core can commit files the executor wrote (Phase 6).
        os.umask(0o002)
        tool_executor = RemoteToolExecutor(executor_socket)
        get_logger("worker").bind(task=name).info("executor.remote", socket=executor_socket)
    if target:
        tcfg = _maybe_target_config(target)
        if tcfg is not None:
            trace.configure(tcfg.trace)   # honor the target repo's [trace] project/toggle
        iteration = gate_iteration or (tcfg.gate.iteration if tcfg else "python -m pytest -q")
        acceptance = gate_acceptance or (tcfg.gate.acceptance if tcfg and tcfg.gate.acceptance else "true")
        runner = make_repo_runner(
            target, mode="clone", adapter=adapter, max_iter=max_iter,
            gate_iteration=iteration, gate_acceptance=acceptance,
            protected_paths=tuple(tcfg.safety.protected_paths) if tcfg else ("tests/",),
            remote=tcfg.remote if tcfg else None, executor=tool_executor,
            skills_repo=skills_repo, skills_branch=skills_branch)
        console.print(Panel.fit(
            f"worker [bold]{name}[/] · target {target} · adapter {adapter} · {redis_url}\n"
            f"gates: {iteration!r} / {acceptance!r} · remote {'on' if tcfg and tcfg.remote.enabled else 'off'}"
            + (f" · executor {executor_socket}" if executor_socket else "")
            + (f" · skills {skills_repo}" if skills_repo else ""),
            title="loopkit fleet worker"))
    else:
        runner = make_demo_runner(adapter=adapter, max_iter=max_iter)
        console.print(Panel.fit(f"worker [bold]{name}[/] · demo-repo · adapter {adapter} · {redis_url}",
                                title="loopkit fleet worker"))
    try:
        Worker(queue, runner, name=name).run_forever()
    except KeyboardInterrupt:
        console.print("[yellow]worker stopped[/]")


@fleet_app.command("run")
def fleet_run(
        tasks: int = typer.Option(3, "--tasks", "-n", help="How many independent tasks to fan out."),
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL"),
        goal: str | None = typer.Option(None, "--goal", help="Override the per-task goal."),
        from_issues: bool = typer.Option(False, "--from-issues",
                                         help="Source tasks from open GitHub/GitLab issues."),
        target: str | None = typer.Option(None, "--target",
                                          help="Repo to read issues from (default: cwd). Used with --from-issues."),
        label: str | None = typer.Option(None, "--label", help="Only issues with this label become tasks."),
        provider: str = typer.Option("auto", "--provider", help="auto | github | gitlab."),
        redis_namespace: str = typer.Option("loopkit", "--redis-namespace", envvar="REDIS_NAMESPACE",
                                             help="Per-run Redis keyspace. Cloud sets this to the run id."),
        drain_workers: int | None = typer.Option(None, "--drain-workers",
                                                  help="On completion, enqueue N sentinels so N ephemeral worker pods exit (cloud).")) -> None:
    """Coordinator — enqueue tasks (N goals, or one per open issue) and collect a FleetResult.

    The queue decouples *what to do* (here) from *how to do it* (the workers). Make sure your
    workers were started with a matching `--target` so they operate on the same repo the issues
    came from.
    """
    # FIRST: the coordinator pod gets git-only creds via the memory-tmpfs mount (Phase 5a) — load +
    # shred them so `--from-issues` (`gh issue list`) can authenticate; without this the token (now a
    # file, not an env var) is invisible to the scrubbed subprocess env.
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from .extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url, namespace=redis_namespace)
    if from_issues:
        from .extensions.issues import fetch_issues, issues_to_tasks
        src = Path(target or ".").expanduser()
        issues = fetch_issues(src, provider=provider, label=label)
        task_list = issues_to_tasks(issues)
        if not task_list:
            err.print(f"[yellow]no open issues found[/]"
                      f"{f' with label {label}' if label else ''} in {src}")
            raise typer.Exit(1)
        console.print(Panel.fit(
            f"enqueue [bold]{len(task_list)}[/] issue task(s) from {src}"
            f"{f' (label {label})' if label else ''} · {redis_url}",
            title="loopkit fleet run --from-issues"))
    else:
        task_list = [{"slug": f"t{i}", "branch": f"loopkit/run-t{i}", "goal": goal or PRICING_GOAL}
                     for i in range(tasks)]
        console.print(Panel.fit(f"fan out [bold]{tasks}[/] tasks · {redis_url}", title="loopkit fleet run"))
    result = Coordinator(queue).run_fleet(task_list, drain_workers=drain_workers)
    console.print(_fleet_table(result))
    raise typer.Exit(0 if result.workers and not result.failed else 2)


def _maybe_target_config(target: str) -> Config | None:
    """Load a target repo's loopkit.toml for its gates/safety/remote, or None if it has none."""
    toml = Path(target).expanduser() / "loopkit.toml"
    if not toml.exists():
        return None
    try:
        return load_config(toml)
    except Exception:                              # noqa: BLE001 — a malformed target toml -> use defaults
        return None


@fleet_app.command("evolve")
def fleet_evolve(
        generations: int = typer.Option(2, "--generations", "-g"),
        population: int = typer.Option(4, "--population", "-p"),
        keep: int = typer.Option(2, "--keep", "-k"),
        redis_url: str = typer.Option(DEFAULT_REDIS_URL, "--redis-url", envvar="REDIS_URL"),
        redis_namespace: str = typer.Option("loopkit", "--redis-namespace", envvar="REDIS_NAMESPACE",
                                             help="Per-run Redis keyspace. Cloud sets this to the run id."),
        drain_workers: int | None = typer.Option(None, "--drain-workers",
                                                  help="After the final generation, enqueue N sentinels so N worker pods exit (cloud).")) -> None:
    """Coordinator — evolutionary search: keep top-k, re-validate survivors, reseed the winner.

    The Ch 9 selection-inflation guard runs at fleet scale: a candidate only reseeds the next
    generation after passing a held-out gate (computed in the worker) it never competed on.
    """
    # FIRST: load + shred any tmpfs-mounted creds before doing work (uniform with the other entrypoints).
    secrets.install(secrets.CredentialStore.load(os.environ.get("LOOPKIT_CREDS_DIR")))
    from .extensions.fleet import PRICING_GOAL, Coordinator, RedisQueue
    queue = RedisQueue.from_url(redis_url, namespace=redis_namespace)
    console.print(Panel.fit(
        f"evolve · {generations} gen × {population} pop, keep {keep} · {redis_url}",
        title="loopkit fleet evolve"))
    result = Coordinator(queue).evolve({"goal": PRICING_GOAL}, generations=generations,
                                       population=population, keep=keep, drain_workers=drain_workers)
    console.print(_evolution_table(result))
    winner = result.winner
    console.print(f"winner: [green]{winner.branch}[/]" if winner else "[yellow]no validated winner[/]")
    raise typer.Exit(0 if winner else 2)


# --------------------------------------------------------------------------------------------
# loopkit cloud — the Part III control plane (Phase 2: context guard + bootstrap).
# --------------------------------------------------------------------------------------------
# `kubernetes` is the [cloud] extra. The cloud extension module imports it lazily, but the *read*
# commands here still need it present to talk to a cluster — so guard with a clear install hint
# rather than letting an ImportError surface raw.
def _require_cloud_extra() -> None:
    if importlib.util.find_spec("kubernetes") is None:
        err.print("[red]cloud[/] the kubernetes client is not installed "
                  r"(pip install 'loopkit\[cloud]').")
        raise typer.Exit(1)


@cloud_app.command("context")
def cloud_context(
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT",
                                            help="Expected cluster context to pin (allowlist; comma-separated)."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG",
                                                help="Kubeconfig file (default: KUBECONFIG / ~/.kube/config).")) -> None:
    """Show the active kube context and whether the guard would allow a mutating command (read-only).

    Safe to run against any cluster — it only *reads* the current context. Use it before `bootstrap`
    to confirm you're pointed at the intended DOKS cluster.
    """
    _require_cloud_extra()
    from .extensions import cloud
    current = cloud.current_context(kubeconfig)
    expected = cloud.resolve_expected(context)
    try:
        cloud.check_context(current, context)
        status, detail = "[green]allowed[/]", f"context {current} is pinned"
    except cloud.ContextError as exc:
        status, detail = "[red]refused[/]", str(exc)
    table = Table(title="loopkit cloud context", header_style="bold")
    table.add_column("field")
    table.add_column("value", overflow="fold")
    table.add_row("current context", escape(str(current or "—")))
    table.add_row("expected (pinned)", escape(", ".join(expected) or "— (nothing pinned)"))
    table.add_row("guard", f"{status} {escape(detail)}")
    console.print(table)


@cloud_app.command("doctor")
def cloud_doctor(
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT",
                                            help="Expected cluster context to pin (allowlist; comma-separated)."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """Pre-flight the cloud control plane: extra installed, kubeconfig readable, context pinned + matching."""
    table = Table(title="loopkit cloud doctor", show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")

    have_extra = importlib.util.find_spec("kubernetes") is not None
    table.add_row("kubernetes client", "[green]ok[/]" if have_extra else "[red]missing[/]",
                  r"loopkit\[cloud] installed" if have_extra else r"pip install 'loopkit\[cloud]'")
    if not have_extra:
        console.print(table)
        raise typer.Exit(1)

    from .extensions import cloud
    expected = cloud.resolve_expected(context)
    table.add_row("expected context", "[green]pinned[/]" if expected else "[red]unpinned[/]",
                  ", ".join(expected) or f"set --context or ${cloud.ENV_CONTEXT} (fail-closed)")
    try:
        current = cloud.current_context(kubeconfig)
        table.add_row("kubeconfig", "[green]ok[/]", f"active context {current or '—'}")
    except cloud.ContextError as exc:
        table.add_row("kubeconfig", "[red]fail[/]", escape(str(exc)))
        console.print(table)
        raise typer.Exit(1)
    try:
        cloud.check_context(current, context)
        table.add_row("context guard", "[green]ok[/]", f"{current} is pinned — mutations allowed")
        ok = True
    except cloud.ContextError as exc:
        table.add_row("context guard", "[red]refused[/]", escape(str(exc)))
        ok = False

    manifests = sorted(p.name for p in cloud.DEFAULT_MANIFEST_DIR.glob("*.yaml"))
    table.add_row("system manifests", "[green]ok[/]" if manifests else "[yellow]none[/]",
                  f"{len(manifests)} in {cloud.DEFAULT_MANIFEST_DIR.name}/: {', '.join(manifests)}")
    if ok:
        # Per-submitter creds (Phase 5a): the most common misconfig is no `fleet` default → every
        # unregistered run silently runs creds-less. Read-only; tolerate an offline failure.
        try:
            from .extensions import creds as credmod
            regs = credmod.list_credentials(kubeconfig=str(kubeconfig) if kubeconfig else None)
            fleet = any(r.submitter == "fleet" for r in regs)
            table.add_row("credentials", "[green]ok[/]" if regs else "[yellow]none[/]",
                          f"{len(regs)} registered · fleet default {'present' if fleet else 'MISSING'}")
        except Exception as exc:   # noqa: BLE001 — a read failure must not break doctor
            table.add_row("credentials", "[yellow]?[/]", f"could not list ({type(exc).__name__})")
    console.print(table)
    raise typer.Exit(0 if ok else 1)


@cloud_app.command("bootstrap")
def cloud_bootstrap(
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT",
                                            help="Expected cluster context to pin (allowlist; comma-separated)."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """One-time: apply ns/loopkit-system (Redis, RBAC, NetworkPolicy) — guarded by the context pin.

    The context-safety guard runs first and refuses any context but the pinned one, so this can never
    mutate the wrong cluster. Idempotent: re-running converges (already-present objects are skipped).
    """
    _require_cloud_extra()
    from .extensions import cloud
    # Show the target + guard verdict before doing anything; mutating a cloud cluster needs intent.
    try:
        current = cloud.check_context(cloud.current_context(kubeconfig), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"apply [bold]ns/loopkit-system[/] (Redis · RBAC · NetworkPolicy)\n"
        f"context [bold]{current}[/] · manifests {cloud.DEFAULT_MANIFEST_DIR}",
        title="loopkit cloud bootstrap"))
    if not yes and not typer.confirm(f"Apply system manifests to '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    try:
        result = cloud.bootstrap(expected=context, kubeconfig=str(kubeconfig) if kubeconfig else None)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]bootstrapped[/] {result.context} · applied {len(result.applied)} manifest(s): "
                  f"{', '.join(result.applied)}")


# --------------------------------------------------------------------------------------------
# loopkit cloud — run mechanics (Phase 3): create_run + ls/status/logs/kill, all guarded.
# --------------------------------------------------------------------------------------------
def _run_creds_from_env() -> dict[str, str]:
    """Collect the agent + git credentials present in the environment (the `--from-env` escape hatch).

    Transitional: a one-off run from a machine that already has keys exported, or before any engineer
    has registered. The default `cloud run` path resolves per-submitter Secrets instead (`--as`). Not
    available on the webhook/cron paths — those are multi-tenant.
    """
    creds: dict[str, str] = {}
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            creds[var] = os.environ[var]
    return creds


def _resolve_submitter(explicit: str | None) -> str:
    """Who a run is for: explicit `--as` → `$LOOPKIT_SUBMITTER` → the `fleet` default."""
    return explicit or os.environ.get("LOOPKIT_SUBMITTER") or "fleet"


def _resolve_run_creds(spec, *, from_env: bool, allow_fleet_fallback: bool, in_cluster: bool,
                       yes: bool, kubeconfig: str | None) -> tuple[dict[str, str], str]:
    """Resolve a run's creds per the Phase-5a policy. Returns (projected creds, source). Exits on refusal.

    mock → none; `--from-env` → the env grab (projected); else the per-submitter resolver with the
    fail-closed fallback policy: the submitter's own key wins; absent it, the shared `fleet` key is
    used only if `--allow-fleet-fallback` (non-interactive) or the operator confirms (interactive);
    otherwise the run is refused (no key is silently borrowed).
    """
    from .extensions import creds as credmod
    if spec.adapter == "mock":
        return {}, "mock"
    if from_env:
        return credmod.project(_run_creds_from_env(), spec.adapter), "from-env"
    ident = credmod.Identity(spec.submitter, spec.env_name, spec.adapter)
    rc = credmod.resolve_for_run(ident, allow_fleet_fallback=False, kubeconfig=kubeconfig,
                                 in_cluster=in_cluster)
    if rc.source == "submitter":
        return rc.data, rc.source
    fleet = credmod.resolve_for_run(ident, allow_fleet_fallback=True, kubeconfig=kubeconfig,
                                    in_cluster=in_cluster)
    interactive = not in_cluster and not yes
    if fleet.source == "fleet":
        if allow_fleet_fallback or (interactive and typer.confirm(
                f"No key registered for '{spec.submitter}'. Use the shared 'fleet' key? "
                "Its budget is shared and a leak isn't attributable to you.")):
            err.print(f"[yellow]using the shared 'fleet' key[/] for '{spec.submitter}' (not attributable)")
            return fleet.data, "fleet-fallback"
        err.print(f"[red]run[/] no key for '{spec.submitter}' and fleet fallback not permitted "
                  "(pass --allow-fleet-fallback, or register: loopkit cloud creds set --as <you>).")
        raise typer.Exit(1)
    err.print(f"[red]run[/] no credentials for submitter '{spec.submitter}' and no fleet default. "
              "Register one: loopkit cloud creds set --as <you> --adapter <adapter>.")
    raise typer.Exit(1)


@cloud_app.command("run")
def cloud_run(
        target: str = typer.Option(..., "--target", help="Repo URL/path the workers clone + operate on."),
        goal: str | None = typer.Option(None, "--goal", help="The per-task goal (one of --goal | --from-issues)."),
        from_issues: bool = typer.Option(False, "--from-issues", help="Source tasks from the target's open issues."),
        label: str | None = typer.Option(None, "--label", help="Only issues with this label (with --from-issues)."),
        provider: str = typer.Option("auto", "--provider", help="Issue forge: auto | github | gitlab (with --from-issues)."),
        workers: int = typer.Option(1, "--workers", "-w", help="Worker pod parallelism (fan-out width)."),
        adapter: str = typer.Option("claude-code", "--adapter", help="mock | claude-code | claude-api | codex | openai-api."),
        evolve: bool = typer.Option(False, "--evolve", help="Generational search instead of blind fan-out."),
        generations: int = typer.Option(2, "--generations", "-g"),
        population: int = typer.Option(4, "--population", "-p"),
        keep: int = typer.Option(2, "--keep", "-k"),
        image: str | None = typer.Option(None, "--image", envvar="LOOPKIT_WORKER_IMAGE",
                                         help="Worker image (ghcr.io/<owner>/loopkit-worker:<tag>)."),
        skills_repo: str | None = typer.Option(None, "--skills-repo", envvar="LOOPKIT_SKILLS_REPO",
                                               help="loopkit-skills git repo for the cross-run flywheel (Phase 5b)."),
        skills_branch: str = typer.Option("main", "--skills-branch",
                                          help="Branch of the skills repo to read/write."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag (selects per-submitter creds)."),
        as_submitter: str | None = typer.Option(None, "--as", help="Submitter whose registered key this run spends."),
        from_env: bool = typer.Option(False, "--from-env",
                                      help="Transitional: take creds from THIS environment (not the resolver)."),
        allow_fleet_fallback: bool = typer.Option(False, "--allow-fleet-fallback",
                                                  help="Permit the shared 'fleet' key when the submitter has none."),
        name: str | None = typer.Option(None, "--name", help="Run id (default: a generated short id)."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        in_cluster: bool = typer.Option(False, "--in-cluster",
                                        help="Authenticate via the pod's ServiceAccount (the CronJob/webhook path)."),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Start a run: build ns/run-<id> + coordinator/worker Jobs and apply them (guarded).

    The context-safety guard runs first, so a run can never land on the wrong cluster. One of
    --goal | --from-issues is required (unless --evolve, which uses the built-in pricing goal).
    `--in-cluster` is how the CronJob (and any in-pod caller) submits — it skips the kubeconfig and
    the interactive confirm and uses the pod's ServiceAccount (the guard pins `in-cluster`).
    """
    _require_cloud_extra()
    import uuid
    from .extensions import cloud, cloudrun
    if not image:
        err.print("[red]run[/] no worker image — pass --image or set $LOOPKIT_WORKER_IMAGE "
                  "(ghcr.io/<owner>/loopkit-worker:<tag>).")
        raise typer.Exit(1)
    if not evolve and not goal and not from_issues:
        err.print("[red]run[/] need one of --goal, --from-issues, or --evolve.")
        raise typer.Exit(1)
    run_id = name or uuid.uuid4().hex[:8]
    submitter = _resolve_submitter(as_submitter)
    try:
        spec = cloudrun.RunSpec(
            run_id=run_id, image=image, target=target, workers=workers, adapter=adapter,
            goal=goal, from_issues=from_issues, label=label, provider=provider,
            mode="evolve" if evolve else "fanout",
            generations=generations, population=population, keep=keep, env_name=env_name,
            submitter=submitter, skills_repo=skills_repo, skills_branch=skills_branch)
    except ValueError as exc:
        err.print(f"[red]run[/] {escape(str(exc))}")
        raise typer.Exit(1)
    kc = str(kubeconfig) if kubeconfig else None
    # Show the plan + guard verdict before mutating a (paid) cloud cluster.
    try:
        current = cloud.check_context(cloud.current_context(kc, in_cluster=in_cluster), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    work = f"{population}×{generations} evolve" if evolve else (
        "issues" if from_issues else f"goal ×{spec.parallelism}")
    console.print(Panel.fit(
        f"run [bold]{spec.run_id}[/] → ns/{spec.namespace}\n"
        f"target {target} · {work} · adapter {adapter} · {spec.parallelism} worker(s)\n"
        f"submitter [bold]{submitter}[/] · context [bold]{current}[/] · image {image}",
        title="loopkit cloud run"))
    # In-cluster (cron/webhook) is non-interactive: there's no TTY to confirm at, so --in-cluster
    # implies --yes (the human already consented when they created the schedule, guarded).
    if not yes and not in_cluster and not typer.confirm(f"Start run '{spec.run_id}' on '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    # Resolve the submitter's key (fail-closed fallback policy) BEFORE creating the run.
    creds, source = _resolve_run_creds(spec, from_env=from_env, allow_fleet_fallback=allow_fleet_fallback,
                                       in_cluster=in_cluster, yes=yes, kubeconfig=kc)
    if source != "mock":
        spec.extra_labels["loopkit.dev/creds"] = source     # attribution: submitter | fleet-fallback | from-env
    try:
        namespace = cloudrun.create_run(spec, expected=context, kubeconfig=kc,
                                        in_cluster=in_cluster, creds=creds)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]started[/] run {spec.run_id} in ns/{namespace} "
                  f"(creds: {source}) · `loopkit cloud status {spec.run_id}`")


@cloud_app.command("ls")
def cloud_ls(
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """List runs across run-* namespaces with their phase + worker counts (read-only)."""
    _require_cloud_extra()
    from .extensions import cloudrun
    runs = cloudrun.list_runs(kubeconfig=str(kubeconfig) if kubeconfig else None)
    if not runs:
        console.print("[dim]no runs[/]")
        return
    table = Table(title="loopkit cloud runs", header_style="bold")
    for col in ("run", "namespace", "phase", "active", "ok", "failed"):
        table.add_column(col, justify="right" if col in ("active", "ok", "failed") else "left")
    for r in runs:
        color = {"complete": "green", "failed": "red", "running": "cyan"}.get(r.phase, "yellow")
        table.add_row(r.run_id, r.namespace, f"[{color}]{r.phase}[/]",
                      str(r.workers_active), str(r.workers_succeeded), str(r.workers_failed))
    console.print(table)


@cloud_app.command("status")
def cloud_status(
        run: str = typer.Argument(..., help="Run id."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """Show one run's phase + worker counts (read-only)."""
    _require_cloud_extra()
    from .extensions import cloudrun
    summary = cloudrun.run_status(run, kubeconfig=str(kubeconfig) if kubeconfig else None)
    if summary is None:
        err.print(f"[yellow]no such run[/] {run} (namespace gone — GC'd or never created)")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"run [bold]{summary.run_id}[/] · ns/{summary.namespace}\n"
        f"phase {summary.phase} · workers active {summary.workers_active} / "
        f"ok {summary.workers_succeeded} / failed {summary.workers_failed}",
        title="loopkit cloud status"))


@cloud_app.command("logs")
def cloud_logs(
        run: str = typer.Argument(..., help="Run id."),
        role: str = typer.Option("worker", "--role", help="worker | coordinator."),
        tail: int | None = typer.Option(None, "--tail", help="Tail only the last N lines per pod."),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """Print a run's pod logs (read-only; kubectl-logs under the hood)."""
    _require_cloud_extra()
    from .extensions import cloudrun
    out = cloudrun.run_logs(run, role=role, tail_lines=tail,
                            kubeconfig=str(kubeconfig) if kubeconfig else None)
    console.print(escape(out))


@cloud_app.command("kill")
def cloud_kill(
        run: str = typer.Argument(..., help="Run id."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Delete a run's namespace (and everything in it) — guarded by the context pin."""
    _require_cloud_extra()
    from .extensions import cloud, cloudrun
    try:
        current = cloud.check_context(cloud.current_context(kubeconfig), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Delete run '{run}' (ns/run-{run}) on '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    try:
        namespace = cloudrun.delete_run(run, expected=context,
                                        kubeconfig=str(kubeconfig) if kubeconfig else None)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]killed[/] run {run} (deleted ns/{namespace})")


# --------------------------------------------------------------------------------------------
# loopkit cloud — triggers (Phase 4): schedule (CronJob) + webhook listener, all → create_run().
# --------------------------------------------------------------------------------------------
@cloud_app.command("schedule")
def cloud_schedule(
        name: str = typer.Argument(..., help="Schedule name (becomes the CronJob name)."),
        target: str = typer.Option(..., "--target", help="Repo the scheduled run operates on."),
        cron: str = typer.Option(..., "--cron", help="Crontab schedule, e.g. \"0 9 * * *\"."),
        from_issues: bool = typer.Option(False, "--from-issues", help="Each firing sweeps open issues."),
        goal: str | None = typer.Option(None, "--goal", help="Fixed recurring goal (instead of --from-issues)."),
        label: str | None = typer.Option(None, "--label", help="Only issues with this label (with --from-issues)."),
        provider: str = typer.Option("auto", "--provider", help="Issue forge: auto | github | gitlab (with --from-issues)."),
        workers: int = typer.Option(1, "--workers", "-w", help="Worker pod parallelism per firing."),
        adapter: str = typer.Option("claude-api", "--adapter", help="API adapter only on this untrusted path (claude-api | openai-api)."),
        image: str | None = typer.Option(None, "--image", envvar="LOOPKIT_WORKER_IMAGE",
                                         help="Worker image (ghcr.io/<owner>/loopkit-worker:<tag>)."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        as_submitter: str | None = typer.Option(None, "--as", help="Submitter whose key each firing spends."),
        allow_fleet_fallback: bool = typer.Option(False, "--allow-fleet-fallback",
                                                  help="Permit the shared 'fleet' key (cron is operator-authored)."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Create a CronJob that fires `loopkit cloud run --in-cluster` on a schedule (guarded).

    The CronJob runs as the loopkit-control SA and reuses the exact `cloud run` path — one of
    --from-issues | --goal is required. It carries no static credentials; each firing resolves the
    `--as` submitter's key in-cluster. The context guard runs first, so a schedule can never be
    created on the wrong cluster.
    """
    _require_cloud_extra()
    from .extensions import cloud, triggers
    if not image:
        err.print("[red]schedule[/] no worker image — pass --image or set $LOOPKIT_WORKER_IMAGE.")
        raise typer.Exit(1)
    try:
        spec = triggers.ScheduleSpec(
            name=name, schedule=cron, target=target, image=image, from_issues=from_issues,
            goal=goal, label=label, provider=provider, adapter=adapter, workers=workers,
            env_name=env_name, submitter=_resolve_submitter(as_submitter),
            allow_fleet_fallback=allow_fleet_fallback)
    except ValueError as exc:
        err.print(f"[red]schedule[/] {escape(str(exc))}")
        raise typer.Exit(1)
    try:
        current = cloud.check_context(cloud.current_context(kubeconfig), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    work = "issues" + (f" (label {label})" if label else "") if from_issues else "fixed goal"
    console.print(Panel.fit(
        f"schedule [bold]{spec.name}[/] · cron \"{cron}\" → loopkit-system\n"
        f"target {target} · {work} · adapter {adapter} · {workers} worker(s)\n"
        f"context [bold]{current}[/] · image {image}",
        title="loopkit cloud schedule"))
    if not yes and not typer.confirm(f"Create schedule '{spec.name}' on '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    try:
        created = triggers.create_schedule(spec, expected=context,
                                           kubeconfig=str(kubeconfig) if kubeconfig else None)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]scheduled[/] {created} (\"{cron}\") · `loopkit cloud schedules`")


@cloud_app.command("schedules")
def cloud_schedules(
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """List loopkit CronJobs in loopkit-system (read-only)."""
    _require_cloud_extra()
    from .extensions import triggers
    schedules = triggers.list_schedules(kubeconfig=str(kubeconfig) if kubeconfig else None)
    if not schedules:
        console.print("[dim]no schedules[/]")
        return
    table = Table(title="loopkit cloud schedules", header_style="bold")
    for col in ("name", "cron", "suspended", "last run"):
        table.add_column(col)
    for s in schedules:
        table.add_row(s.name, s.schedule, "[yellow]yes[/]" if s.suspended else "no",
                      s.last_run or "—")
    console.print(table)


@cloud_app.command("unschedule")
def cloud_unschedule(
        name: str = typer.Argument(..., help="Schedule name to delete."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Delete a CronJob by name — guarded by the context pin."""
    _require_cloud_extra()
    from .extensions import cloud, triggers
    try:
        current = cloud.check_context(cloud.current_context(kubeconfig), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Delete schedule '{name}' on '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    try:
        removed = triggers.delete_schedule(name, expected=context,
                                           kubeconfig=str(kubeconfig) if kubeconfig else None)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]unscheduled[/] {removed}")


@cloud_app.command("webhook")
def cloud_webhook(
        image: str | None = typer.Option(None, "--image", envvar="LOOPKIT_WORKER_IMAGE",
                                         help="Worker image for the runs this listener starts."),
        secret: str | None = typer.Option(None, "--secret", envvar="LOOPKIT_WEBHOOK_SECRET",
                                          help="Webhook secret — GitHub HMAC or GitLab token (fail-closed: required)."),
        provider: str = typer.Option("github", "--provider", envvar="LOOPKIT_WEBHOOK_PROVIDER",
                                     help="Which forge this listener serves: github | gitlab."),
        label: str | None = typer.Option(None, "--label", envvar="LOOPKIT_TRIGGER_LABEL",
                                         help="Only issues bearing this label dispatch a run."),
        adapter: str = typer.Option("claude-api", "--adapter",
                                    help="API adapter only on this untrusted path (claude-api | openai-api)."),
        as_submitter: str | None = typer.Option(None, "--as", envvar="LOOPKIT_SUBMITTER",
                                                help="Pinned submitter identity (required for GitLab; a fallback for GitHub)."),
        allow_fleet_fallback: bool = typer.Option(False, "--allow-fleet-fallback",
                                                  help="Permit the shared 'fleet' key for an unregistered submitter."),
        workers: int = typer.Option(1, "--workers", "-w", help="Worker parallelism per triggered run."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        host: str = typer.Option("0.0.0.0", "--host"),
        port: int = typer.Option(8080, "--port"),
        redis_url: str | None = typer.Option(None, "--redis-url", envvar="REDIS_URL",
                                             help="Shared idempotency store (needed if replicas > 1)."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT")) -> None:
    """Serve the GitHub/GitLab webhook listener: signed issue events → one guarded in-cluster run.

    Runs in-cluster (the pod's ServiceAccount). Fail-closed: refuses to start without a secret or with
    a CLI adapter; every delivery is authenticated, the submitter (issue author / pinned identity) is
    resolved to a registered key BEFORE the dedupe reservation, and one run per issue. One listener
    serves one forge — pick it with --provider.
    """
    _require_cloud_extra()
    from .extensions import cloud, cloudrun
    from .extensions import creds as credmod
    from .extensions import triggers
    if not secret:
        err.print("[red]webhook[/] no secret — set --secret or $LOOPKIT_WEBHOOK_SECRET "
                  "(refusing to serve an unauthenticated endpoint).")
        raise typer.Exit(1)
    if not image:
        err.print("[red]webhook[/] no worker image — pass --image or set $LOOPKIT_WORKER_IMAGE.")
        raise typer.Exit(1)
    if adapter in triggers.CLI_ADAPTERS:
        err.print(f"[red]webhook[/] adapter '{adapter}' is refused on the untrusted webhook path "
                  "(a CLI adapter holds the key in its own loop). Use --adapter claude-api.")
        raise typer.Exit(1)
    try:
        forge = triggers.provider_for(provider)
    except ValueError as exc:
        err.print(f"[red]webhook[/] {escape(str(exc))}")
        raise typer.Exit(1)
    if forge.name == "gitlab" and not as_submitter:
        err.print("[red]webhook[/] GitLab requires a pinned identity (--as <submitter>): its token "
                  "isn't bound to the body, so the payload's author is not trusted.")
        raise typer.Exit(1)
    # The listener submits in-cluster; verify the guard would allow it before binding the socket.
    try:
        cloud.check_context(cloud.current_context(in_cluster=True), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)

    def resolve(spec: cloudrun.RunSpec):
        return credmod.resolve_for_run(
            credmod.Identity(spec.submitter, spec.env_name, spec.adapter),
            allow_fleet_fallback=allow_fleet_fallback, in_cluster=True)

    def start(spec: cloudrun.RunSpec, creds_data: dict) -> str:
        try:
            return cloudrun.create_run(spec, expected=context, in_cluster=True, creds=creds_data)
        finally:
            creds_data.clear()           # zero the tenant's key out of the listener heap after use (G13)

    store = (triggers.RedisIdempotencyStore.from_url(redis_url) if redis_url
             else triggers.InMemoryIdempotencyStore())
    app_obj = triggers.WebhookApp(
        secret=secret, image=image, create=start, resolve=resolve, store=store, provider=forge,
        adapter=adapter, workers=workers, env_name=env_name, trigger_label=label,
        listener_submitter=as_submitter)
    server = triggers.serve(app_obj, host=host, port=port)
    console.print(Panel.fit(
        f"listening on [bold]{host}:{port}[/] · forge {forge.name} · adapter {adapter} · "
        f"label {label or '—'} · dedupe {'redis' if redis_url else 'in-memory'}",
        title="loopkit cloud webhook"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("[yellow]webhook listener stopped[/]")
        server.shutdown()


# --------------------------------------------------------------------------------------------
# loopkit cloud creds — per-submitter credential registration (Phase 5a). Guard-first; env/stdin only.
# --------------------------------------------------------------------------------------------
@creds_app.command("set")
def creds_set(
        as_submitter: str = typer.Option(..., "--as", help="The engineer/identity to register."),
        adapter: str = typer.Option("claude-code", "--adapter",
                                    help="Which adapter's key to read from the environment."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Register a submitter's keys (read from THIS environment) into a Secret in loopkit-system.

    Reads the adapter's key + git token from the environment — NEVER from an argument, so a value
    can't land in shell history or `ps`. Merges into the submitter's Secret, so run it once per
    adapter to accumulate (e.g. `--adapter claude-api` then `--adapter openai-api`).
    """
    _require_cloud_extra()
    from .extensions import cloud
    from .extensions import creds as credmod
    data = credmod.project(dict(os.environ), adapter)
    if not data:
        wanted = ", ".join((*secrets.ADAPTER_KEYS.get(adapter, ()), *secrets.GIT_ENV))
        err.print(f"[red]creds set[/] no credentials in the environment for adapter '{adapter}' "
                  f"(expected one of: {wanted}). Export them, then re-run.")
        raise typer.Exit(1)
    kc = str(kubeconfig) if kubeconfig else None
    try:
        current = cloud.check_context(cloud.current_context(kc), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(Panel.fit(
        f"register [bold]{as_submitter}[/] ({env_name}) · keys {', '.join(sorted(data))}\n"
        f"→ ns/loopkit-system · context [bold]{current}[/]", title="loopkit cloud creds set"))
    if not yes and not typer.confirm(f"Store {as_submitter}'s credentials on '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    try:
        name = credmod.set_credential(as_submitter, data, env_name=env_name, expected=context, kubeconfig=kc)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]registered[/] {as_submitter} → {name} (keys: {', '.join(sorted(data))})")


@creds_app.command("ls")
def creds_ls(kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG")) -> None:
    """List registered submitters in loopkit-system (key NAMES only, never values)."""
    _require_cloud_extra()
    from .extensions import creds as credmod
    rows = credmod.list_credentials(kubeconfig=str(kubeconfig) if kubeconfig else None)
    if not rows:
        console.print("[dim]no registered credentials[/] — `loopkit cloud creds set --as <you>`")
        return
    table = Table(title="loopkit credentials", header_style="bold")
    for col in ("submitter", "env", "keys"):
        table.add_column(col)
    for r in sorted(rows, key=lambda r: (r.env_name, r.submitter)):
        table.add_row(r.submitter, r.env_name, ", ".join(r.keys) or "—")
    console.print(table)


@creds_app.command("rm")
def creds_rm(
        as_submitter: str = typer.Option(..., "--as", help="The submitter to remove."),
        env_name: str = typer.Option("prod", "--env", help="Logical env tag."),
        context: str | None = typer.Option(None, "--context", envvar="LOOPKIT_CLOUD_CONTEXT"),
        kubeconfig: Path | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")) -> None:
    """Delete a submitter's credential Secret — guarded by the context pin."""
    _require_cloud_extra()
    from .extensions import cloud
    from .extensions import creds as credmod
    kc = str(kubeconfig) if kubeconfig else None
    try:
        current = cloud.check_context(cloud.current_context(kc), context)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Delete {as_submitter}'s credentials ({env_name}) on '{current}'?"):
        err.print("[yellow]aborted[/]")
        raise typer.Exit(1)
    try:
        name = credmod.delete_credential(as_submitter, env_name=env_name, expected=context, kubeconfig=kc)
    except cloud.ContextError as exc:
        err.print(f"[red]refused[/] {escape(str(exc))}")
        raise typer.Exit(1)
    console.print(f"[green]removed[/] {name}")


def _fleet_table(fleet) -> Table:
    table = Table(title="fleet result", header_style="bold")
    table.add_column("task")
    table.add_column("branch")
    table.add_column("terminal")
    table.add_column("iters", justify="right")
    for worker in fleet.workers:
        reason = worker.result.reason.value if worker.result else (worker.error or "error")
        color = "green" if worker.done else "yellow"
        iters = str(worker.result.iterations) if worker.result else "-"
        table.add_row(str(worker.task.get("slug", worker.task.get("id", "?"))),
                      worker.branch, f"[{color}]{reason}[/]", iters)
    return table


def _evolution_table(result) -> Table:
    table = Table(title="evolution result", header_style="bold")
    table.add_column("gen", justify="right")
    table.add_column("best (score)")
    table.add_column("confirmed")
    table.add_column("inflation")
    for gen in result.generations:
        best = (f"{gen.survivors[0].branch} ({gen.survivors[0].score:.2f})"
                if gen.survivors else "-")
        confirmed = gen.confirmed.branch if gen.confirmed else "-"
        flag = "[yellow]caught[/]" if gen.inflated else "[green]clean[/]"
        table.add_row(str(gen.index), best, confirmed, flag)
    return table


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
        err.print("[red]sandbox[/] docker not found on PATH (build the image: docker build -t loopkit .)")
        raise typer.Exit(1)
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


def _load(config: Path) -> Config:
    if not config.exists():
        err.print(f"[red]no config[/] {config}  (run [bold]loopkit init[/])")
        raise typer.Exit(1)
    try:
        return load_config(config)
    except Exception as exc:                       # noqa: BLE001 — surface any validation error cleanly
        err.print(f"[red]invalid config[/] {exc}")
        raise typer.Exit(1)


def _render(result: RunResult) -> None:
    if result.reason is StopReason.DONE:
        color = "green"
    elif result.reason is StopReason.SAFETY:
        color = "red"
    else:
        color = "yellow"
    lines = [f"reason: [{color}]{result.reason.value}[/]",
             f"iterations: {result.iterations}",
             f"cost: ${result.cost_usd:.2f}"]
    if result.overfit:
        lines.append("[yellow]overfit: the iteration gate passed but the held-out gate did not[/]")
    if result.detail:
        lines.append(result.detail)
    console.print(Panel.fit("\n".join(lines), title="result"))


def main() -> None:
    app()
