#!/usr/bin/env bash
# examples/gates/docs-gate.sh — a GENERIC structural VERIFICATION gate for a PROSE / docs repo.
#
# The deterministic half of the two-oracle pattern when the artifact is Markdown, not code. Pair it
# with the held-out review.sh (peer LLM review) for substance:
#   [gate]
#   iteration  = "bash gate/docs-gate.sh"     # structure + links — deterministic, every tick
#   acceptance = "bash gate/review.sh"          # peer LLM review of substance — held-out, once
#
# Prefers the real tool (markdownlint) when present and falls back to pure-shell checks, so it runs
# out of the box with no extra install. Collects ALL failures. Exit 0 = pass; non-zero = fail.
set -uo pipefail

# Which files to check (default: all tracked markdown). Override by editing the glob.
# (Built with a read loop, not `mapfile` — macOS ships bash 3.2, which has no mapfile.)
FILES=()
while IFS= read -r _f; do
  [ -n "$_f" ] && FILES+=("$_f")
done < <(git ls-files '*.md' 2>/dev/null || find . -name '*.md' -not -path './.git/*')
[ ${#FILES[@]} -gt 0 ] || { echo "docs gate: no .md files found"; exit 1; }
fails=()

# 1. Lint — the real tool if installed, else a pure-shell smell (unbalanced code fence is a common,
#    machine-checkable defect that breaks rendering).
if command -v markdownlint >/dev/null 2>&1; then
  markdownlint "${FILES[@]}" || fails+=("markdownlint reported issues (see output above)")
else
  for f in "${FILES[@]}"; do
    awk '/^```/{n++} END{exit (n%2)}' "$f" || fails+=("$f: unbalanced \`\`\` code fence")
  done
fi

# 2. Relative-link check (no external dep): every ./relative or ../relative markdown link must resolve.
#    Process substitution keeps the loop in THIS shell so the failure list persists (a classic
#    while-read-in-a-pipe bug otherwise). Anchors (#section) are stripped before the existence check.
for f in "${FILES[@]}"; do
  while IFS= read -r rel; do
    target="$(dirname "$f")/${rel%%#*}"
    [ -z "${rel%%#*}" ] || [ -e "$target" ] || fails+=("$f: broken relative link -> $rel")
  done < <(grep -oE '\]\(\.\.?/[^) ]+' "$f" | sed 's/](//')
done

# (Upgrade path: swap in a fuller checker — `markdownlint-cli2`, `lychee`, `markdown-link-check` — and
#  add your own structural asserts, e.g. `grep -q '^## Sources' "$f"`. A gate is just a shell command.)

if [ ${#fails[@]} -eq 0 ]; then
  echo "docs gate: PASS (${#FILES[@]} markdown files)"
  exit 0
fi
echo "DOCS GATE FAILED — fix these and re-run:"
for f in "${fails[@]}"; do echo "  - $f"; done
exit 1
