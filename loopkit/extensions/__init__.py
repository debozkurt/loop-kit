"""Part II extensions — the advanced layers on top of the single-agent core.

Each module is a named seam with a fixed interface, so the core stays stable while these attach
to it. All three are implemented; each opt-in and `None`-safe, so the core's behaviour is
unchanged when an extension isn't supplied:

    review.py       continuous review of every commit (Ch 8)        -> run_loop(review_hook=...)
    orchestrate.py  a supervisor over many worker loops (Ch 10-12)  -> wraps run_loop as the worker
    skills.py       the skill registry + write-back flywheel (Ch 17) -> run_loop(skills=...) + build_prompt

The one Part II item still outstanding is the Tilt deployable fleet (worker loops as containers
+ a task queue); the library pieces above are done.
"""
