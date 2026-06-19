"""loopkit — a self-governed coding loop for any repository.

This package is the runnable form of the agentic-loops engineering manual. Each module
implements one part of the course and is a named, swappable seam:

    config.py       the one Config object — the whole loop as one file   (Ch 18)
    agent.py        the model as a subroutine the loop invokes            (Ch 1-3)
    prompt.py       fixed prompt, fresh context, anchor files             (Ch 4-5)
    gate.py         the iteration gate and the held-out acceptance gate   (Ch 6-7, 9)
    stops.py        the three hard stops + precedence                     (Ch 13-14)
    durability.py   commit every tick; resume from git                    (Ch 15)
    safety.py       blast-radius preflight + protected-path guard         (Ch 16)
    loop.py         the controller that wires them — the tick lifecycle   (Ch 1-3,7,13)

The single-agent core lives here. Orchestration, continuous review, and skills are
defined-but-deferred seams under `loopkit.extensions` (Part II of the course).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
