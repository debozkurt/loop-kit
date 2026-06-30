"""loopkit — a self-governed coding loop for any repository.

This package is the runnable form of the agentic-loops engineering manual. Each module
implements one part of the course and is a named, swappable seam. The loop spine:

    config.py       the one Config object — the whole loop as one file   (Ch 18)
    agent.py        the model as a subroutine the loop invokes            (Ch 1-3)
    prompt.py       fixed prompt, fresh context, anchor files             (Ch 4-5)
    gate.py         the iteration gate and the held-out acceptance gate   (Ch 6-7, 9)
    stops.py        the three hard stops + precedence                     (Ch 13-14)
    durability.py   commit every tick; resume from git                    (Ch 15)
    safety.py       blast-radius preflight + protected-path guard         (Ch 16)
    loop.py         the controller that wires them — the tick lifecycle   (Ch 1-3, 7, 13)

Cross-cutting seams the loop leans on:

    pricing.py      per-adapter cost — what makes the budget stop bite    (Ch 14)
    log.py          structured, greppable, payload-free logging           (Ch 15)
    trace.py        optional full-tree LangSmith observability (auto-on)  (Ch 14-15)
    secrets.py      keep a real key out of the injected agent's reach     (Part III, P5a)
    executor.py     tool execution behind a seam — relocatable off the key (Part III, P6)
    cli.py          the typer entrypoint: init / doctor / run / fleet / cloud
    _templates.py   the file bodies `init` scaffolds (pure data)

The single-agent core lives here and keeps **no runtime dependency** on the opt-in layers —
orchestration, continuous review, skills, the deployable fleet, and the Part III cloud control
plane are seams under `loopkit.extensions`. See that package's docstring for the map.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
