# Peer-review rubric (generic) — edit for your task

The held-out reviewer ([`review.sh`](review.sh)) accepts the change **only if every criterion holds**.
Keep this file under `safety.protected_paths` so the author can't tune the grader (Ch 9 — the verifier
is not part of the agent's editable surface). Make the criteria *task-specific* — the sharper the
rubric, the more honest the gate.

## Accept criteria

1. **Solves the stated goal.** The change actually does what the task asked — not a near-miss, not a
   partial.
2. **Correct, not merely present.** No obvious logic or factual errors; edge cases are handled; any
   claim/citation is real and checkable.
3. **No regressions.** Existing behavior, tests, and docs still hold — the fix doesn't pass its target
   by breaking something else.
4. **Scope discipline.** Touches only what the goal needs; no unrelated churn or drive-by rewrites.
5. **Quality bar.** Readable and consistent with the surrounding code/prose; a careful reviewer would
   trust it without rework.

## Reject if

- The goal is only partially met, OR
- A clear error exists (logic, fact, or a citation that doesn't resolve), OR
- Existing behavior/tests regress, OR
- The change sprawls beyond the task, OR
- It games the visible/mechanical check without actually solving the problem.
