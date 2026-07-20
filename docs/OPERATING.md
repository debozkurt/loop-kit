# Operating a run — the operator's view

How to *drive* a loop day to day: read what it's doing, tell a healthy-but-silent run from a hung one,
stay out of its way, resume it, and know where its output goes. (For *configuring* a run see
[`CONTROL-FILES.md`](CONTROL-FILES.md); for *billing* see [`BILLING.md`](BILLING.md); for *failures* see
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).)

## Watching a run — the live step stream (and the heartbeat behind it)

The **claude-code** adapter streams by default: loopkit reads the agent's `stream-json` events as they
arrive and emits one `[loopkit][agent]` line per step — every thought, tool call, and tool result — so
you can watch a run work in real time instead of guessing whether it's alive:

```text
… [loopkit][loop]  agent.invoke tick=1 promptLen=14948
… [loopkit][agent] agent.think Scoping the notes queryset to the owner before returning … idx=1 len=212
… [loopkit][agent] agent.tool idx=2 tool=Read arg=src/apps/notes/views_legacy.py
… [loopkit][agent] agent.say Adding an owner-scoped filter so buyers can't read others' notes idx=4 len=61
… [loopkit][agent] agent.tool idx=5 tool=Edit arg=src/apps/notes/views/preferences.py
… [loopkit][agent] agent.result idx=6 isError=False outLen=0
… [loopkit][loop]  agent.done tick=1 costUsd=7.60
```

- `agent.say` / `agent.think` — the agent's narration / extended-thinking (the *thoughts*).
- `agent.tool` — a tool call: `tool=` name, `arg=` the salient target (file, command, url).
- `agent.result` — a tool result, payload-free (`isError` + output `outLen`).

Grep by subsystem: `grep '\[loopkit\]\[agent\]'` for the agent's steps, `grep agent.tool` for just its
actions, `grep '\[loopkit\]\[loop\]'` for the loop's own lifecycle.

**The heartbeat is the fallback.** When a phase genuinely produces no events for a while — a gate
running the project's test suite, or the model thinking before its first token — the loop still pings
every ~20 s, so a slow phase never looks like a hang:

```text
tick.progress phase=agent elapsedSec=20
tick.progress phase=iteration_gate elapsedSec=40
```

`phase` is `agent` / `iteration_gate` / `review` / `acceptance_gate` / `regression_gate`. Truly stuck = no new
`[loopkit][agent]` lines **and** no heartbeat **and** no new commits for a long time → `Ctrl-C` (you
lose at most the current tick; prior ticks are committed) and re-run to resume. From a second terminal,
stay **read-only** — never mutate a live run's repo: `pgrep -lf 'claude -p'` (agent alive),
`git -C <repo> log --oneline -3` (each completed tick is a commit on the work branch).

> **Buffered adapters** (codex, or claude-code with `--output-format json` pinned) don't stream — for
> those the heartbeat is the only liveness signal, exactly as before.

## Never touch the working tree of a live run

If anything modifies a file while a run is operating on the repo — **especially a protected path** —
the next tick's protected-path check sees it and halts with `safety_halt`, reverting that tick:

```text
ERROR safety.protected_path_touched … first=RUNBOOK.md
reason: safety_halt
```

The guard is doing its job (no corruption, `main` untouched), but the run dies. **Operate hands-off;
make edits to the config, gates, or docs only *between* runs.** During a run, limit yourself to the
read-only checks above. (This is also why the loop commits every tick — so a stray halt costs you one
tick, never the whole run.)

## Resuming

The loop is **durable by commit-every-tick** (Ch 15): every tick is a commit on the work branch, so
progress survives a crash, a `Ctrl-C`, or a `safety_halt`.

- **Re-run to continue.** `loopkit run` again picks up the committed state on the branch and keeps
  going toward the goal. The clean-tree preflight expects a committed tree — which is exactly what a
  prior run leaves.
- **Throw it away.** A bad run is one `git branch -D loopkit/<branch>` from gone; `main` was never
  touched (Ch 16). Start fresh.
- **Tighten and retry.** Edit the goal / gates / `--max-iter` *(between runs)* and re-run — the most
  common loop-engineering move.

## Where the output goes (per tier)

Same loop body in every tier; only *where you read it* differs:

| Tier | Launch | Output / logs |
|---|---|---|
| **Local** | `loopkit run` | your **terminal** (the `[loopkit][loop]` lines on stderr) |
| **CI** | a labelled issue → Actions/GitLab | the **CI job log**, then a **draft PR** ([`examples/ci/`](../examples/ci/)) |
| **Cloud fleet** | `loopkit cloud run` | `loopkit cloud logs` / `kubectl logs` (Kubernetes Jobs) |

> A local `loopkit run` is **not** Kubernetes — there are no `kubectl` logs for it. Only the cloud
> tier (`loopkit cloud …`) runs as k8s Jobs.

## The observability layers you're reading

- **Loop logs (`[loopkit][loop]`, always on, payload-free):** `<ISO> LEVEL [loopkit][loop] event
  key=value …` — ids, lengths, counts, never payloads. One `grep` reconstructs a run: `… | grep run=<id>`.
  Safe to ship anywhere.
- **Agent step stream (`[loopkit][agent]`, claude-code streaming — ⚠️ payload-bearing):** one line per
  agent step. `agent.say`/`agent.think` carry the model's **thoughts verbatim** and `agent.tool` carries
  the **target file/command**, so these lines are **not** ship-safe by default — that's a deliberate
  operator trade for live, readable visibility (secrets are still redacted). To go back to payload-free
  logs, pin `[agent] args = ["--output-format", "json"]` in `loopkit.toml`: that reverts claude-code
  to the quiet buffered path — you lose the live step stream but the logs are shippable again.
- **Traces (optional, full detail):** install `loopkit[trace]` + set `LANGSMITH_API_KEY` and every run
  emits a span tree (run → tick → agent → llm/tool → gate) with the human-readable I/O + per-span cost.
  `doctor` shows whether tracing is on.
- **Durable activity artifact (batch):** the raw event stream persisted to disk — see below.

Key events to watch: `run.start` · `agent.invoke` · `agent.think`/`agent.say`/`agent.tool` ·
`tick.progress` · `agent.done` · `tick.commit` · `gate.iteration`/`gate.review`/`gate.acceptance` ·
`run.done` or `loop.halt reason=…`.

## The durable activity artifact (`loopkit batch`)

A batch run writes each task's full agent activity to `<manifest_dir>/<task>.activity.jsonl` — **next to
the journal**, so it survives the per-task worktree that's deleted when the task ends. It's the durable,
replayable record of *what the agent actually did* (the live `[loopkit][agent]` stream is ephemeral
terminal output; this is the copy you can read tomorrow):

```jsonl
{"loopkit": "run.start", "run": "7c1d7844", "branch": "loopkit/issue-22"}
{"loopkit": "tick.start", "run": "7c1d7844", "tick": 1}
{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"…"},{"type":"tool_use","name":"Edit",…}]}}
{"type":"user","message":{"content":[{"type":"tool_result",…}]}}
{"type":"result","subtype":"success","total_cost_usd":7.60,"result":"…"}
{"loopkit": "run.done", "run": "7c1d7844"}
```

- **loopkit-authored markers** (`{"loopkit": …}`) delimit the run and each tick; **verbatim agent
  events** (`{"type": …}`) are teed in between, faithful for replay.
- Written **line-by-line as events arrive** (flushed each line), so a *hung or crashed* tick still leaves
  a partial trail — the exact "why did this tick go sideways" case.
- Same payload caveat as the live stream: it contains the agent's thoughts + tool I/O (secrets redacted),
  so treat it as sensitive. Inspect a single task: `jq -c 'select(.type=="tool_use") | .name' <task>.activity.jsonl`.
- Single `loopkit run` does not write one (no manifest dir); it's a batch artifact.
