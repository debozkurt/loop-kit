# Using loopkit on your own repo

The demo-repo teaches the mechanics; this is how you point loopkit at **your** project, sync the
result to GitHub/GitLab, and let issues drive a fleet. Everything outward-facing is **opt-in and
off by default** — nothing leaves your machine until you set `[remote] enabled = true`.

The mental model never changes: **a run is `goal` + two gates + a repo.** Everything else (the tick
lifecycle, durability, safety, stops) is fixed. So "use it on a new project" is really "write a
good goal and two honest gates."

---

## 1. Target any repo on your machine

The single loop runs against whatever the config's `repo` points at.

```bash
cd ~/code/my-project
loopkit init .                 # scaffolds loopkit.toml + PROMPT.md (never overwrites)
```

Edit `loopkit.toml` — the four fields that define the run:

```toml
goal   = "Make the CSV exporter handle quoted fields with embedded newlines, per PROMPT.md."
repo   = "."
branch = "loopkit/run"         # never main/master

[agent]
adapter = "claude-code"        # mock | claude-code | codex

[gate]
iteration  = "pytest tests/unit -q -k exporter"     # FAST, in-sample — runs every tick
acceptance = "pytest tests/integration/exporter"    # HELD-OUT — runs once before DONE

[safety]
protected_paths = ["tests/"]   # the loop may not edit its own gates (Ch 9 + 16)
```

Then:

```bash
loopkit doctor                 # preflight: branch safe? agent on PATH? gates set?
loopkit run --dry-run          # rehearse the control flow — no agent, no tokens
loopkit run                    # real run; commits every tick to loopkit/run, drives to DONE
loopkit run --sandbox          # same, inside the Docker image (OS-level blast-radius containment)
```

Two ways to point at a repo without editing the toml's `repo`:

```bash
loopkit run -c ~/code/my-project/loopkit.toml --repo ~/code/my-project   # --repo overrides
cd ~/code/my-project && loopkit run                                      # or just run from inside
```

**Choosing the two gates is the whole craft.** The iteration gate is what the loop optimizes every
tick — keep it fast. The acceptance gate is *held-out*: checks the loop never sees until it claims
victory, so a green iteration gate that's actually overfit gets caught (the demo-repo lesson). If
you only have one test suite, split it: put the broad/edge-case tests behind `acceptance` and a
fast subset behind `iteration`. No held-out gate → no protection against "passes the tests, wrong
behaviour."

---

## 2. Sync the result to a remote (GitHub / GitLab)

The loop is always durable locally (commit every tick). To take the finished branch *outward*, add
a `[remote]` block:

```toml
[remote]
enabled  = true          # master switch — no push/PR happens unless this is true
name     = "origin"      # the git remote to push to
push     = true          # push the loop branch on DONE
open_pr  = true          # then open a PR/MR
provider = "auto"        # auto (detect from the remote URL) | github | gitlab
pr_base  = "main"        # the base branch the PR targets
draft    = true          # open as a draft — a human reviews + merges
```

**One-off, no block:** `loopkit run --open-pr` flips `enabled` + `open_pr` for a single run — the
same switch a static `[remote]` sets. It's how the CI tier (§4) opens its draft PR without keeping a
`[remote]` block in the repo's config.

**Prerequisites** (loopkit shells out to these — no Python SDK dependency):

```bash
gh auth status      # GitHub: `brew install gh && gh auth login`
glab auth status    # GitLab: `brew install glab && glab auth login`
```

Now a finished run ends with:

```
pushed loopkit/run → origin
opened PR https://github.com/you/my-project/pull/123
```

**Safety at the outward edge.** The same Ch 16 guard that stops the loop committing to `main` stops
it *pushing* to `main`: `push_branch` refuses any branch in `safety.forbid_branches`, and it never
force-pushes. The PR is a **draft** by default — loopkit proposes, a human disposes.

---

## 3. Drive the fleet from issues

Turn a labelled backlog into the fleet's work queue. Each open issue becomes a task on its own
branch (`loopkit/issue-<N>`); solve it, and (with `[remote]`) the PR that lands **closes the issue**.

There are two roles, and they're separate on purpose (the queue decouples *what* from *how*):

- **Workers** (the executors) — started with `--target` so they operate on your repo.
- **The coordinator** (`fleet run --from-issues`) — reads issues and enqueues them.

### Host-process flow (simplest — your machine, your git creds, no cluster)

```bash
# 1. label the issues you want automated:  add the `loopkit` label on GitHub/GitLab
# 2. start a few workers pointed at your repo (each its own process):
for i in 1 2 3; do
  loopkit fleet worker --target ~/code/my-project --adapter claude-code --name w$i &
done
# 3. coordinator: open issues -> tasks -> the workers solve them
loopkit fleet run --from-issues --target ~/code/my-project --label loopkit
```

This needs a Redis to connect through — either the Tilt fleet (`make fleet-up && tilt up`, redis on
`localhost:16379`, pass `--redis-url redis://localhost:16379`) or any local redis. The workers pull
each issue, run the loop on a clone, and — if the repo's `[remote]` is enabled — push the branch and
open a PR that closes the issue.

### On the kind/Tilt cluster (pods)

Same commands, but the worker `Deployment` (`k8s/worker.yaml`) sets `--target` and the pods need
two things mounted that the host has for free:

- **The target's toolchain** — gates run the project's test commands, so extend the worker image
  (the `Dockerfile`) for your stack (Node, Go, …); it ships Python + pytest today.
- **Git credentials** to clone (if private) and push — mount a token as a Kubernetes `Secret` and
  expose it to `git`/`gh` (e.g. `GH_TOKEN`, or a `~/.git-credentials` file). Treat it like any
  production secret; never bake it into the image.

> The bundled demo (`--adapter mock`, no `--target`) needs none of this — it's the token-free,
> credential-free smoke test. The two mounts above are what graduate it to a real project.

---

## 4. Hands-off in CI (no cluster)

The lowest-friction way to put loopkit on a real repo is the **CI deployment tier**: a labelled issue
starts a CI job that runs one `loopkit run` and opens a **draft** PR — the forge is the trigger, the
secret store, the identity, and the sandbox, so there's no infrastructure to operate.

```bash
loopkit init --ci github     # scaffold .github/workflows/loopkit.yml (or: --ci gitlab → .gitlab-ci.yml)
# add the repo secret ANTHROPIC_API_KEY, edit the two gates in loopkit.toml, commit
# then open an issue, add the `loopkit` label, and watch a draft PR appear
```

Under the hood the job runs `loopkit run --from-event "$GITHUB_EVENT_PATH" --adapter claude-api
--open-pr` (GitHub) or `--from-issue "$ISSUE_IID" --provider gitlab …` (GitLab). The full how-it-works
+ the GitHub-vs-GitLab differences + the runnable labs (`loopkit demo 20`/`21`) are in the teaching
module **[`part-iii-ecosystem.md`](part-iii-ecosystem.md)**; the templates live in
[`../examples/ci/`](../examples/ci/).

---

## Parallel on one machine (git worktrees)

The fleet (§3) is the issue-driven, Redis-backed path. For a quick "run N goals at once on this box" —
no Redis, no issues — give each run its own **git worktree**: a checkout has exactly one branch, so
parallel runs can't share one. One worktree = one branch = one loop.

```bash
for slug in featA featB featC; do
  git worktree add -b "loopkit/$slug" "../wt-$slug" HEAD
  ( cd "../wt-$slug" && loopkit run --repo "../wt-$slug" \
       --branch "loopkit/$slug" -c "../$slug.toml" ) &     # one config per goal (or --from-issue N)
done
wait
# review each on its branch, merge the winners, then:  git worktree remove ../wt-<slug>
```

Each worktree commits to its own branch; you review and merge the good ones. Mind the worktree/parallel
sharp edges in **Gotchas** below (gitignored files, clean-tree, gate `cwd`, shared-file conflicts).

---

## Gotchas

- **The gate runs the target's toolchain.** A real run needs that toolchain present (locally, or in
  the worker image). `loopkit doctor` checks the agent binary, not your test runner.
- **`--from-issues` finds nothing?** Check `gh issue list --label loopkit` works in that repo, that
  the label exists, and that `gh`/`glab` is authed. Empty queue → the coordinator exits with a note.
- **Workers and coordinator must agree on the repo.** `fleet run --target X` enqueues issues from
  `X`; the workers must have been started with `--target X` too, or they'll run the wrong project.
- **Private repos in pods** need the clone credential mounted, not just the push one.
- **`loopkit run` checks out the config's `branch` in the target tree — even with `--dry-run`.** Point
  it at your working checkout and it switches that checkout to `loopkit/…`. Use a worktree (above) to
  keep your main checkout put, or `git checkout -` afterward.
- **A fresh worktree/clone doesn't have your *gitignored* files.** If a gate script, config, or rubric
  lives under a gitignored path it won't exist in a new worktree — symlink it in, or keep it tracked.
  (Tracked is also what CI clones need and what `protected_paths` needs: that guard is *git-diff based*,
  so it can't protect a gitignored file and won't see one on a clone.) When you symlink a **directory**,
  note a gitignore directory pattern (`foo/`) does *not* match a *symlink* named `foo`, so the untracked
  symlink trips `require_clean_tree` — add it to `.git/info/exclude`.
- **Write gates to grade the *workspace*, not their own location.** loopkit runs a gate with `cwd` set
  to the workspace, so derive the repo root from `$PWD` (`git rev-parse --show-toplevel`) — then one
  gate, symlinked into every worktree, grades each worktree instead of the original repo.
- **Parallel runs that all edit one shared file conflict on merge.** If every run touches a common
  status/changelog/index file you get N-way conflicts. Have the agent confine changes to its own
  artifact and do the shared-file update in a single pass after merging.
- **Size `max_cost_usd` for more than one tick.** A budget that barely covers a single author-tick
  can't iterate against gate feedback — leave room for the acceptance check plus at least one fix-tick.
  (The budget stop halts *starting a new tick*; it won't abort a candidate that reaches DONE mid-tick.)

See also: [`CONTROL-FILES.md`](CONTROL-FILES.md) for the `.md` files that steer each run, and
[`archive/part-ii-tilt-fleet-plan.md`](archive/part-ii-tilt-fleet-plan.md) for the cluster bring-up.
