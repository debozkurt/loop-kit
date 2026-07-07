# loopkit CI templates — the no-cluster deployment tier

These two workflows run the **single loop** from a forge's CI: a labelled issue becomes a draft
PR/MR, with **no Kubernetes and no loopkit infrastructure**. The forge supplies everything the cloud
tier hand-builds — the trigger, the secret store, the run identity, the compute, and the per-job
sandbox — so loopkit is just the loop. This is the middle of loopkit's three deployment tiers
(*local* · **CI** · *cloud fleet*); the full rationale is in
[`docs/part-iii-ci-mode.md`](../../docs/part-iii-ci-mode.md).

## How it fits the loopkit flow

A labelled issue is the trigger; the CI runner is the sandbox; the **loop body is identical** to a
local or cloud run (agent → gates → commit). On `DONE` the loop pushes a branch and opens a draft PR
that closes the issue — and the **human reviewing that PR is the held-out second oracle** (the
keyless-CI-friendly stand-in for an in-loop LLM-review gate, Ch 9).

```mermaid
%%{init: {'theme':'base', 'themeVariables': {'background':'#0d1117','primaryColor':'#21262d','primaryTextColor':'#e6edf3','primaryBorderColor':'#8b949e','lineColor':'#8b949e','fontSize':'13px'}}}%%
flowchart LR
  I["issue labelled<br/>loopkit"] -->|trigger| R["CI runner<br/>(the sandbox)"]
  R --> K["loopkit run<br/>--from-issue --open-pr"]
  K --> L["the loop body<br/>agent → gates → commit"]
  L -->|DONE| P["push branch +<br/>draft PR · Closes #N"]
  P --> H["human review<br/>(held-out 2nd oracle)"]
  H -->|approve| M["merge"]
```

*Same loop body in every tier — only the **trigger**, the **secret delivery**, and the **sandbox**
come from the forge instead of your laptop (local) or Kubernetes (cloud).*

| File | Forge | Adapter / billing | Drop it at |
|---|---|---|---|
| [`github-actions.yml`](github-actions.yml) | GitHub Actions | `claude-api` · `ANTHROPIC_API_KEY` | `.github/workflows/loopkit.yml` |
| [`github-actions-claude-code.yml`](github-actions-claude-code.yml) | GitHub Actions | `claude-code` · **Claude Code subscription** (`CLAUDE_CODE_OAUTH_TOKEN`, no API key) | `.github/workflows/loopkit.yml` |
| [`gitlab-ci.yml`](gitlab-ci.yml) | GitLab CI | `claude-api` · `ANTHROPIC_API_KEY` | `.gitlab-ci.yml` |
| [`gitlab-ci-claude-code.yml`](gitlab-ci-claude-code.yml) | GitLab CI | `claude-code` · **subscription** (`CLAUDE_CODE_OAUTH_TOKEN` + `GITLAB_TOKEN`, no API key) | `.gitlab-ci.yml` |

Pick the `claude-code` variant to bill your **subscription** instead of a metered API key: it installs
the `claude` CLI on the runner and authenticates with a `CLAUDE_CODE_OAUTH_TOKEN` repo secret (from
`claude setup-token`). Do **not** set `ANTHROPIC_API_KEY` alongside it — `claude-code` defaults to the
subscription and withholds an API key (`run --api-key` opts back into billing).

On **GitLab** the templates self-install runner deps in `before_script` (`glab` for both; Node + the
`claude` CLI for claude-code) and handle the runtime sharp edges a live run found (clone strategy, git
identity, stripping `CI_JOB_TOKEN`, base-ref materialization, non-root for claude-code). Two things are
on **you**:
- **`GITLAB_TOKEN`**: a PAT with **`api` + `write_repository`** scopes whose owner has **≥ Developer
  role** on the project. Scope *and* role — missing either 403s the push. (`CI_JOB_TOKEN` can do
  neither.) Already using `GITLAB_TOKEN` for other CI? Set **`LOOPKIT_GITLAB_PAT`** instead — the
  templates remap it.
- **Runner**: a **docker-executor** runner (a low-pids k8s runner breaks DNS in `before_script`).

Full gotcha list + fixes: [`docs/TROUBLESHOOTING.md`](../../docs/TROUBLESHOOTING.md#gitlab-ci--runner--push-gotchas-issuemr-worker).

The fastest way to get either is to let loopkit scaffold it (it also writes a starter `loopkit.toml`
+ `PROMPT.md`, which the workflow needs):

```bash
loopkit init --ci github     # or: --ci gitlab
```

These files are byte-identical to what `loopkit init --ci <forge>` writes (a test enforces it) —
they live here so you can read them without running the CLI.

## Installing loopkit on the runner

loopkit isn't on PyPI yet — install it from the `loop-kit` git repo. Swap the template's `pip install`
line for whichever applies (token injected so it never lands in argv/logs):

- **Private `loop-kit` on GitHub** — add CI/CD var **`LOOPKIT_GH_PAT`** (fine-grained PAT, Contents:
  read on the repo), then:
  ```
  pip install "loopkit[claude] @ git+https://x-access-token:${LOOPKIT_GH_PAT}@github.com/<owner>/loop-kit.git@main"
  ```
  (claude-code adapter: drop `[claude]` — no provider SDK needed.)
- **`loop-kit` mirrored to the same GitLab instance** — use the built-in job token, **no extra var**:
  ```
  pip install "loopkit @ git+https://gitlab-ci-token:${CI_JOB_TOKEN}@${CI_SERVER_HOST}/<group>/loop-kit.git@main"
  ```
- **Published** (PyPI or your package index) — plain `pip install 'loopkit[claude]'`, as the templates show.

## What you supply

1. **A `loopkit.toml`** in the repo (gates, branch, safety envelope). `loopkit init` scaffolds one;
   edit the two gates so they actually check the work.
2. **Secrets**, as CI-native masked variables — **no resolver, no k8s Secrets, no shred** (that's the
   cloud tier's machinery; it deliberately stays there):
   - GitHub: a repo/org secret `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_OAUTH_TOKEN` for the subscription
     variant). The push + PR use the job's scoped `github.token`.
   - GitLab: masked `ANTHROPIC_API_KEY` and a `GITLAB_TOKEN` (PAT, `api` + `write_repository`, owner
     with ≥ Developer role) — authenticates `glab` (issue + MR) **and** the git push.
3. **(GitHub, one-time) Let Actions open PRs.** GitHub blocks the `github.token` from *creating* PRs by
   default, independent of the `permissions:` block. Enable it once: *Settings → Actions → General →
   Workflow permissions →* ☑ *Allow GitHub Actions to create and approve pull requests* (or
   `gh api -X PUT repos/<owner>/<repo>/actions/permissions/workflow -F can_approve_pull_request_reviews=true`).
   Skip it and `--open-pr` fails with `pr.failed … not permitted to create or approve pull requests`
   **even though the loop reached DONE and pushed the branch** — so check `gh pr list`, not just the
   green check. (Or open the PR with a user PAT / GitHub App token instead of `github.token`.)

## How a run starts

- **GitHub** fires on `issues: [opened, labeled]`; the job's `if:` gates on the `loopkit` label (the
  opt-in switch), and `--from-event "$GITHUB_EVENT_PATH"` reads the issue straight off the event JSON.
  A `workflow_dispatch` with an issue number takes the `--from-issue` path instead.
- **GitLab** has no native issue→pipeline trigger, so it fires on a manual *Run pipeline* (pass an
  `ISSUE_IID` variable), a webhook → trigger token, or a pipeline schedule; `--from-issue "$ISSUE_IID"`
  fetches that one issue via `glab`.

In both, `--adapter claude-api` keeps the key in loopkit's process (no agent binary to install or
auth), and `--open-pr` flips on push + a **draft** PR/MR for that one invocation — so the template is
turnkey on a repo whose `loopkit.toml` leaves `[remote]` off (the safe default). loopkit's own
controls (protected paths, branch-only, held-out gate, budget stop) still apply.

The GitHub templates also pass **`--branch loopkit/issue-$ISSUE`** so each issue lands on its own
branch → its own draft PR, and several issues can run **concurrently** without colliding on a shared
branch. Re-firing one issue reuses its branch and updates that issue's single PR (idempotent). It's
still N independent single-loops — coordinated/`evolve`/shared-queue runs are the cloud fleet. See
[`docs/part-iii-ci-mode.md`](../../docs/part-iii-ci-mode.md) → *Multiple PRs in flight*.

**Revise runs (GitHub only):** the templates also fire on `pull_request_review`. **Request changes**
on a loopkit PR and the loop runs again with your review as the goal — it resumes the PR's own
branch and pushes, so the *same* PR updates. Each review round runs once; approvals and plain
comments do nothing; only `loopkit/*` branches are ever revised (the loop follows through on its own
PRs, it never adopts yours). GitLab has no changes-requested MR primitive, so its templates have no
revise lane. See [`docs/part-iii-ci-mode.md`](../../docs/part-iii-ci-mode.md) → *Revise runs*.

## Identity & cost attribution

CI secrets are **repo/env-scoped**, so a run spends the *repo's* key, attributed to the run — not to
the engineer who filed the issue. Per-submitter keying + cost-capping is a **cloud-tier** feature; CI
is per-repo by design. If you need per-engineer attribution or many concurrent `evolve` runs, that's
the cloud fleet tier.
