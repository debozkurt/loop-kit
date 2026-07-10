<!--
  acceptance/<key>/judge-rubric.md — this issue's finding-specific REJECT criteria, appended to the
  shared adversarial checklist (examples/gates/rubric.md) by the review hook. The deterministic oracle
  proves the fix *works*; this rubric adds the semantic review a test can't — root cause vs symptom,
  gaming, wire contract, generality. Skeleton generalized from a spacer `acceptance/<finding>/judge-rubric.md`.

  Keep each criterion CONCRETE and checkable against the diff. Delete the FILL examples; write yours.
-->

## Finding-specific criteria for `FILL_KEY` (REJECT if any is unmet)

- FILL: the exact behavior that must be present — name the file/function and the observable outcome
  (e.g. "`X` must reject a wrong-tenant caller with 403; REJECT a client-flag check instead of real auth").
- FILL: what must NOT change — the wire contract to preserve (e.g. "no public field removed except the N
  named; REJECT if `address`/`price`/… dropped").
- FILL: the fix must be general — REJECT special-casing the oracle (hardcoded values, magic paths, a
  branch that only makes one test input pass).
- FILL: the shipped test must fail without the code change — REJECT a trivially-passing test.
- FILL: scope limits — REJECT an unrequested migration / chart / CI edit; REJECT changes outside the
  finding's blast radius.
