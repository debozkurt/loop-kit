# Operating a run — the operator's view

How to *drive* a loop day to day: read what it's doing, tell a healthy-but-silent run from a hung one,
stay out of its way, resume it, and know where its output goes. (For *configuring* a run see
[`CONTROL-FILES.md`](CONTROL-FILES.md); for *billing* see [`BILLING.md`](BILLING.md); for *failures* see
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).)

## A run is silent by design — here's how to know it's alive

loopkit prints `agent.invoke` **before** calling the agent, then runs `claude`/the gate as a
**captured subprocess** — so a research or test phase can be *minutes* with no output. That is normal,
not a hang. Two ways to confirm liveness:

1. **The heartbeat.** During any long phase the loop now emits a ping every ~20 s:
   ```text
   agent.invoke tick=1 …
   tick.progress phase=agent elapsedSec=20
   tick.progress phase=agent elapsedSec=40
   agent.done tick=1 …
   ```
   `phase` is `agent` / `iteration_gate` / `acceptance_gate` — so you can see *what* it's chewing on.
2. **From a second terminal (read-only — never mutate a live run's repo):**
   ```bash
   pgrep -lf 'claude -p'                 # the agent process is alive (and it's your prompt)
   ls -la <the-new-file>                 # once it appears, the agent has started writing this tick
   git -C <repo> log --oneline -3        # each completed tick adds a commit on the work branch
   ```
   Truly stuck = no heartbeat **and** no `claude` process **and** no new commits for a long time →
   `Ctrl-C` (you lose at most the current tick; prior ticks are committed) and re-run to resume.

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

## The two-layer observability you're reading

- **Logs (always on, payload-free):** `<ISO> LEVEL [loopkit][component] event key=value …` — ids,
  lengths, counts, never payloads. One `grep` reconstructs a run: `… | grep run=<id>`.
- **Traces (optional):** install `loopkit[trace]` + set `LANGSMITH_API_KEY` and every run emits a full
  span tree (run → tick → agent → llm/tool → gate) with the human-readable I/O + per-span cost. `doctor`
  shows whether tracing is on.

Key events to watch: `run.start` · `agent.invoke`/`tick.progress`/`agent.done` · `tick.commit` ·
`gate.iteration`/`gate.acceptance` · `run.done` or `loop.halt reason=…`.
