"""Extensions — opt-in layers on top of the single-agent core.

Each module is a named seam with a fixed interface, attached to the core keyword-only and
`None`-safe, so the core keeps **no runtime dependency** on this package (`pip install loopkit`
pulls none of it; the heavy ones sit behind extras — `[fleet]`, `[cloud]`). Two tiers:

Part II — library seams (the loop, extended in-process):

    review.py       continuous review of every commit (Ch 8)            -> run_loop(review_hook=...)
    orchestrate.py  a supervisor over many worker loops (Ch 10-12)      -> wraps run_loop as the worker
    skills.py       the skill registry + write-back flywheel (Ch 17)    -> run_loop(skills=...) + build_prompt
    fleet.py        the loop behind a Redis queue, run by many workers  -> `loopkit fleet`            (Ch 12)
    remote.py       push the loop's branch + open a PR/MR               -> run_loop(remote=) / `run --open-pr`
    issues.py       GitHub/GitLab issues as the fleet's work queue      -> `fleet run --from-issues`

Part III — the cloud control plane (the fleet on managed Kubernetes, behind `[cloud]`) + the
measurement layer:

    cloud.py        `loopkit cloud` — the CLI talking to a DOKS cluster, context-pinned   (Phase 2)
    cloudrun.py     create_run() — the ephemeral per-run Job topology it builds            (Phase 3)
    triggers.py     external events -> runs via the one create_run() seam: webhook + CronJob (Phase 4)
    creds.py        per-submitter creds: identity -> Secret, projected into a run          (Phase 5a)
    measure.py      reliability — pass^k over N trials of one goal      -> `loopkit measure` (runs local)

Part IV — the molding kit (configure loopkit for a repo; the copilot molds with verified primitives):

    synth_gate.py   fail-first (and, with a fix, fail->pass) oracle verification -> `loopkit synth-gate`
    detect.py       deterministic repo introspection -> a proposed loopkit.toml -> `loopkit detect`
    route.py        measure pass^k -> a single-run-vs-evolve decision            -> `loopkit route`
"""
