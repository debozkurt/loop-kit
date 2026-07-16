"""Predicted-touch overlap analysis — which batch tasks are likely to collide. [Part III]

`batch` gives the manifest two scheduling levers (`group` serialization, `after` dependencies) but
nothing *derives* them — the author is left to eyeball which tasks will step on the same files.
This module is that derivation: read each task's **predicted-touch set**, intersect them pairwise,
and suggest the levers for the pairs the manifest doesn't already cover.

Touch data comes cheapest-first, and honesty beats guessing:

- ``touches = ["src/handlers/search.go", ...]`` — an explicit per-task field, highest trust.
- **goal text** — zero-config fallback: well-written goals (and forge issues) cite the files they
  are about, so repo-relative path tokens (``dir/file.ext``) are lifted straight out of the text.
- neither → the task is reported **unanalyzed**, never silently assumed conflict-free.

Advisory, never a gate. Predicted-touch is a heuristic: a false positive that forced serialization
would tax every future batch, while a missed conflict costs one rebase at merge time — so the
analysis only ever *suggests*. And because each batch task runs in an isolated clone, overlapping
tasks don't collide while running — their PRs collide **at merge**. One analysis therefore feeds
two consumers: `group`/`after` suggestions for the run, and a merge-order hint (manifest order
within each overlap component) for the human merging the PRs afterwards.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

# Repo-relative path tokens: at least one `dir/` segment then `name.ext`. The lookbehind rejects
# tokens already inside a longer path or URL (`https://host/a/b.py` matches nothing: every segment
# after the scheme is preceded by `/`), and the extension anchor drops trailing `:12-34` line refs
# for free. Deliberate misses — absolute paths, leading-dot files, extensionless files (Makefile):
# cite those via the explicit `touches` field.
_PATH_RE = re.compile(r"(?<![\w@:/.])((?:[\w.-]+/)+[\w-]+\.[A-Za-z]\w{0,7})\b")

# Touch-data provenance per task, in trust order.
EXPLICIT = "touches"          # the task declared its paths
FROM_GOAL = "goal"            # paths lifted out of the goal text
NONE = "none"                 # nothing to analyze — reported, never assumed safe


@dataclass(frozen=True)
class TaskTouches:
    """One task's predicted-touch set and where it came from."""

    id: str
    paths: frozenset[str]
    source: str               # EXPLICIT | FROM_GOAL | NONE


@dataclass(frozen=True)
class Collision:
    """Two tasks predicted to touch the same file(s).

    `covered` means the manifest already handles the pair — same `group`, or an `after` path
    connects them (either direction, transitively) — so no suggestion is needed.
    """

    a: str
    b: str
    paths: tuple[str, ...]
    covered: bool


@dataclass
class OverlapReport:
    """Everything the analysis found: per-task touches, collisions, and the suggested levers."""

    touches: list[TaskTouches] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)
    components: list[list[str]] = field(default_factory=list)   # overlap clusters, manifest order
    suggestions: dict[str, str] = field(default_factory=dict)   # task id -> suggested group name
    unanalyzed: list[str] = field(default_factory=list)         # ids with no touch data

    @property
    def uncovered(self) -> list[Collision]:
        return [c for c in self.collisions if not c.covered]


def touches_for(spec) -> TaskTouches:
    """A task's predicted-touch set: the explicit `touches` field, else paths in the goal text.

    Duck-typed over `TaskSpec`/`MoldSpec` (both carry id/goal/touches), so one analysis serves a
    batch manifest and a mold plan alike. An issue-sourced task with no fetched goal and no
    explicit touches lands on NONE — `batch` warns post-fetch, where the goal text exists.
    """
    explicit = list(getattr(spec, "touches", None) or [])
    if explicit:
        return TaskTouches(id=spec.id, paths=frozenset(explicit), source=EXPLICIT)
    found = frozenset(_PATH_RE.findall(spec.goal or ""))
    if found:
        return TaskTouches(id=spec.id, paths=found, source=FROM_GOAL)
    return TaskTouches(id=spec.id, paths=frozenset(), source=NONE)


def analyze(specs: list) -> OverlapReport:
    """Intersect every pair's predicted-touch sets; suggest `group`s for the uncovered overlaps.

    Suggestions go only to tasks that overlap and don't already share a group — an existing group
    or a connecting `after` path marks the pair covered (the author already declared the collision).
    Suggested names come from the most-shared path's stem, so the manifest reads as intent
    ("settings", "handlers"), not as generated noise ("overlap-1").
    """
    report = OverlapReport(touches=[touches_for(s) for s in specs])
    by_id = {t.id: t for t in report.touches}
    report.unanalyzed = [t.id for t in report.touches if t.source == NONE]
    reach = _after_reachability(specs)

    adjacency: dict[str, set[str]] = defaultdict(set)
    for i, a in enumerate(specs):
        for b in specs[i + 1:]:
            shared = by_id[a.id].paths & by_id[b.id].paths
            if not shared:
                continue
            covered = (a.group is not None and a.group == b.group) \
                or b.id in reach[a.id] or a.id in reach[b.id]
            report.collisions.append(
                Collision(a=a.id, b=b.id, paths=tuple(sorted(shared)), covered=covered))
            adjacency[a.id].add(b.id)
            adjacency[b.id].add(a.id)

    # Connected components over ALL collisions (covered ones included): the merge-order hint wants
    # the whole cluster, even where scheduling is already declared.
    order = [s.id for s in specs]                          # manifest order = merge-order suggestion
    seen: set[str] = set()
    for tid in order:
        if tid in seen or tid not in adjacency:
            continue
        stack, members = [tid], set()
        while stack:
            node = stack.pop()
            if node in members:
                continue
            members.add(node)
            stack.extend(adjacency[node] - members)
        seen |= members
        report.components.append([t for t in order if t in members])

    used: set[str] = set()
    for members in report.components:
        if not any(not c.covered for c in report.collisions
                   if c.a in members and c.b in members):
            continue                                       # fully declared already — nothing to suggest
        name = _group_name(members, report.collisions, used)
        for m in members:
            if next(s for s in specs if s.id == m).group is None:
                report.suggestions[m] = name               # only ungrouped members get the suggestion
    return report


def _after_reachability(specs: list) -> dict[str, set[str]]:
    """Transitive closure of `after` edges: reach[x] = every task x (indirectly) depends on."""
    edges = {s.id: list(getattr(s, "after", None) or []) for s in specs}
    reach: dict[str, set[str]] = {}

    def visit(node: str) -> set[str]:
        if node in reach:
            return reach[node]
        reach[node] = set()                                # placeholder guards against cycles
        acc: set[str] = set()
        for dep in edges.get(node, []):
            acc.add(dep)
            acc |= visit(dep)
        reach[node] = acc
        return acc

    for node in edges:
        visit(node)
    return reach


def _group_name(members: list[str], collisions: list[Collision], used: set[str]) -> str:
    """Name the suggested group after the most-shared path's stem — intent, not generated noise."""
    counts: dict[str, int] = defaultdict(int)
    for c in collisions:
        if c.a in members and c.b in members:
            for p in c.paths:
                counts[p] += 1
    top = max(sorted(counts), key=lambda p: counts[p]) if counts else members[0]
    stem = top.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    name = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "overlap"
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{name}-{n}"
        n += 1
    used.add(candidate)
    return candidate
