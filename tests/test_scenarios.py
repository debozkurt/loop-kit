"""Smoke tests for the demo/learn scenarios — each plays in scripted mode without raising.

Keeps the teaching scenarios from rotting as the core evolves: a signature change in the loop,
gates, or agent that breaks a scenario fails here instead of in front of a class.
"""
from __future__ import annotations

from io import StringIO

from rich.console import Console

from loopkit import scenarios


def test_registry_has_core_chapters():
    chapters = {s.chapter for s in scenarios.available()}
    assert {5, 7, 9, 13, 16}.issubset(chapters)


def test_all_scenarios_play_in_scripted_mode():
    for scenario in scenarios.available():
        console = Console(file=StringIO(), width=100)
        scenarios.play(scenario.chapter, console, live=False, pause=False)  # must not raise
