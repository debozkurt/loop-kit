"""Prompt assembly: a fixed prompt rebuilt into a fresh context every tick (Chapters 4-5).

The ralph discipline: discard conversation history, and rebuild the prompt each tick from
durable anchor files on disk plus the one piece of dynamic state that matters — the feedback
from the last gate. A small, fresh, correct context beats a large, accumulating, degrading
one.
"""
from __future__ import annotations

from pathlib import Path

_MAX_ANCHOR_BYTES = 64_000   # guard against a giant anchor crowding out the window


def read_anchors(repo: Path, anchors: list[str]) -> str:
    """Concatenate the anchor files (and the files inside anchor directories)."""
    chunks: list[str] = []
    for rel in anchors:
        target = repo / rel
        if target.is_dir():
            for f in sorted(target.rglob("*")):
                if f.is_file():
                    chunks.append(_read_one(f, repo))
        elif target.is_file():
            chunks.append(_read_one(target, repo))
    return "\n\n".join(c for c in chunks if c)


def _read_one(path: Path, repo: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_ANCHOR_BYTES]
    except OSError:
        return ""
    # An anchor may resolve outside the repo (a shared prompt file referenced by absolute path);
    # `relative_to` raises there, so fall back to the bare name rather than crash the prompt build.
    try:
        label = path.relative_to(repo)
    except ValueError:
        label = path.name
    return f"# --- {label} ---\n{text}"


def build_prompt(config, feedback: str | None, skills: str | None = None) -> str:
    """Assemble the tick's prompt: goal + anchors + skills + last feedback + rules. No history."""
    repo = config.repo_path()
    parts = [
        f"# Goal\n{config.goal}",
        read_anchors(repo, config.prompt.anchors),
    ]
    if skills:
        # Lessons distilled from past successful runs (the write-back flywheel, Ch 17). Already
        # carries its own heading; placed before feedback so a learned skill frames the attempt.
        parts.append(skills)
    if feedback:
        parts.append(
            "# Feedback from the last attempt (the gate failed — address this)\n"
            f"{feedback}"
        )
    if config.safety.protected_paths:
        guards = ", ".join(config.safety.protected_paths)
        parts.append(f"# Off-limits\nDo not read or edit files under: {guards}")
    parts.append(
        "# Rules\n"
        "- Make the goal's checks pass by making the code correct, not by weakening, "
        "deleting, or skipping the checks.\n"
        "- Keep changes minimal and focused on the goal."
    )
    return "\n\n".join(p for p in parts if p)
