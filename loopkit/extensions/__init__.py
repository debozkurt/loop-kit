"""Part II seams — defined here, implemented in the second lecture.

These modules fix the *interfaces* so the single-agent core stays stable while the advanced
layers land later. v1 ships none of the implementations; the attach points in the core are
marked in code:

    review.py       continuous review of every commit (Ch 8)      -> after the commit in loop.py
    orchestrate.py  a supervisor over many worker loops (Ch 10-12) -> wraps run_loop as the worker
    skills.py       the skill registry + write-back flywheel (Ch 17) -> prompt assembly + after DONE
"""
