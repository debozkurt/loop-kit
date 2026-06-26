# Walkthrough — copy this, run it, see `DONE`

A worked end-to-end run on a real (throwaway) repo. The [demos](../../loopkit/scenarios/) *teach
concepts*; this is the **"paste these commands, watch the loop reach DONE"** starter. It reuses the
bundled [`../demo-repo/`](../demo-repo/) — a tiny Python repo with a deliberate bug and a **held-out
test split** (`tests/seen/` vs `tests/holdout/`), the two-oracle gate in its simplest real form.

The bug: `pricing.py`'s `line_total` ignores the "10% bulk discount at quantity ≥ 10" rule. The
visible tests (`tests/seen`) pass *anyway* (a coverage gap); only the held-out tests catch it — so a
loop that just fits the visible tests is **overfitting**, and the acceptance gate is what certifies a
real fix.

## 0. Set it up (a throwaway git repo)

```bash
cp -r "$(python -c 'import loopkit, pathlib; print(pathlib.Path(loopkit.__file__).parent.parent/"examples"/"demo-repo")')" /tmp/lk-walkthrough
cd /tmp/lk-walkthrough
git init -q . && git add -A && git -c user.email=you@example.com -c user.name=you commit -qm init
```

## 1. Pre-flight — is it safe to point a loop here?

```bash
loopkit doctor
```
```text
│ safety preflight │ ok   │ branch loopkit/run                                              │
│ agent            │ ok   │ /opt/homebrew/bin/claude · auth subscription (… CLAUDE_CODE_… ) │
│                  │      │ · ANTHROPIC_API_KEY present but withheld (--api-key to bill it) │
│ budget           │ ok   │ cost parsed from claude JSON · ceiling $5.0                     │
│ iteration gate   │ set  │ python -m pytest tests/seen -q                                  │
│ acceptance gate  │ set  │ python -m pytest tests/holdout -q                              │
```
Note the `agent` row: `claude-code` will run on your **subscription** (the ambient `ANTHROPIC_API_KEY`
is withheld) — billing is visible *before* you spend. (`--api-key` opts into the billed key.)

## 2. Dry-run the mechanics, token-free

Before spending anything, watch the control flow with the `mock` agent. It makes no edits, so the
gate never passes and the loop halts on `NO_PROGRESS` — proving the gates run and the stops bite:

```bash
loopkit run --adapter mock --max-iter 3
```
```text
run.start … adapter=mock maxIter=3 budgetUsd=5.0
agent.invoke tick=1 promptLen=1134 …
agent.done   tick=1 ok=True costUsd=0.5 summary=noop
tick.commit  tick=1 committed=False sig=3ea907cf…          # mock changed nothing
gate.iteration tick=1 passed=False                          # the VERIFICATION gate actually ran
agent.invoke tick=2 promptLen=1237 …                        # promptLen grew = gate feedback fed back
…
loop.halt tick=3 reason=no_progress iterations=3 costUsd=1.5
╭── result ───────────╮
│ reason: no_progress │      # signature unchanged 3 ticks → stall caught before the cap
╰─────────────────────╯
```
(The `costUsd` here is the mock's *simulated* per-tick charge — fake money, to exercise the budget
stop.)

## 3. The real run — reach `DONE`

```bash
loopkit run                       # uses claude-code from loopkit.toml; bills your subscription
```
A real run looks like this (numbers vary). On a **long, silent** agent/gate phase you'll now see
`tick.progress` liveness pings every ~20 s — so it never looks hung:

```text
run.start … adapter=claude-code maxIter=15 budgetUsd=5.0
agent.invoke tick=1 …
tick.progress phase=agent elapsedSec=20                     # ← liveness while claude works
agent.done   tick=1 ok=True costUsd=0.04 summary=rc=0…
tick.commit  tick=1 committed=True sig=…                    # the fix is committed durably
gate.iteration  tick=1 passed=True                          # visible tests pass
gate.acceptance tick=1 passed=True                          # held-out tests ALSO pass → not overfit
run.done tick=1 iterations=1 …
╭── result ──╮
│ reason: done │
╰────────────╯
```
> **On a subscription the `$` cost can read low/0** (a parsing quirk of the CLI's output) — so bound a
> real run with `--max-iter` rather than relying on `max_cost_usd` alone.

## 4. Inspect what it did

```bash
git switch loopkit/run        # the run's branch — main was never touched
git show --stat HEAD          # the fix it committed
git diff main..loopkit/run    # the full change
```

## 5. Advanced features to try (all on this same repo)

| Try | What it shows |
|---|---|
| `loopkit run --check-gate 5` | refuse to start if the iteration gate is **flaky** (Ch 9 stability preflight) |
| edit `tests/holdout/…` during a run | the **protected-path** guard → `safety_halt` (it can't touch the verifier) |
| `loopkit measure -n 5` | run the goal 5× → **pass^k** (reliability) vs **pass@k** (discovery) |
| `loopkit run --api-key` | opt into billing the API key instead of the subscription |
| swap the gates | this repo uses the **test** two-oracle; see [`../gates/`](../gates/) for the **docs/prose** flavor (`docs-gate.sh` + `review.sh`) |

For day-to-day operation — telling a *silent* run from a hung one, resuming, where output goes per
tier — see [`docs/OPERATING.md`](../../docs/OPERATING.md).
