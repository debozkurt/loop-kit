"""Shared demo/learn scenarios — one format, two front-ends (filled in the next increment).

A Scenario is a small data object (chapter, setup, narration, run, observe) that both
`loopkit demo --chapter N` (run it) and `loopkit learn --chapter N` (narrate it) consume.
Defining it once here is what makes the guided `learn` mode cheap on top of `demo`.
"""
