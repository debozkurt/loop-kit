# Coverage tiers → a typed Definition of Done

*Generalized from the spacer remediation harness's `ledger2issues.py` (`COVERAGE_TIER_DOD`). That
script hand-classified each audit finding and turned the class into a concrete "what the test must
assert." This is that logic, made repo-agnostic — the copilot applies it to any goal/issue.*

The point: **a goal is only verifiable if you know what its test must prove.** Classifying the work
first turns "write a test" into "write *this* test," which is what makes the acceptance oracle (step 3
of the [skill](SKILL.md)) tractable — you template the assertion instead of inventing it.

## The tiers

Pick the one that matches the work; the assertion is what the *shipped* test (and the held-out oracle)
must demonstrate.

| Tier | The test must assert… |
|---|---|
| `authz` | a wrong-role / cross-tenant caller is rejected (403/404) **and** the legitimate owner still succeeds |
| `wire-contract` | the wire shape (HTTP status + response fields) is locked before and after the fix — no field renamed/removed unless the goal called for it |
| `silent-fallback` | the failure branch is exercised and lands on the **safe** default (not just the happy path) |
| `serializer` | the exact field set emitted — confidential fields **absent**, public fields **present** |
| `input-validation` | the boundary value (cap enforced, empty/whitespace handled) **and** just past it |
| `concurrency` | the race itself (TransactionTestCase / advisory-lock / unique constraint) fails without the fix |
| `correctness` | a unit test that fails against the current (buggy) code and passes after the fix |

`correctness` is the default when nothing more specific fits. If a goal spans two tiers (e.g. an authz
fix that also changes the wire shape), assert both.

## The Definition of Done (assemble per goal)

Classify → then write the DoD the loop drives toward. Four parts, always:

1. **Behavior:** the intended change is implemented (state it concretely, not "fix the bug").
2. **Test ships (test-as-you-go, non-negotiable):** add *the tier's assertion above*, co-located with the
   code per this repo's convention. The diff MUST add/modify a test file — enforced by
   [`../gates/has-tests.sh`](../gates/has-tests.sh). A fix with no test change does not pass.
3. **No regressions:** the existing suite stays green; no test weakened, skipped, or deleted to pass.
4. **Observability:** logging/tracing added where a new failure path is introduced, per repo convention.

Then an explicit **out of scope / do not touch** note (CI, charts, migrations, lockfiles are protected
unless the goal explicitly unlocks them) and any hard **constraints**.

## Two test sets, two purposes (keep them separate)

- **The held-out acceptance oracle** (yours, hidden, outside the tree the agent edits) — protects the
  *loop* from gaming its own gates. This is what the tier assertion feeds.
- **The agent's shipped, co-located test** (test-as-you-go) — protects the *repo*, enforced by
  `has-tests.sh`.

They are not the same file and must not be conflated: the oracle proves the fix works to *loopkit*; the
shipped test proves it to *the repo's future maintainers*.
