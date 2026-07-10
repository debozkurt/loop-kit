#!/usr/bin/env bash
# worktree.sh — isolate one issue's run on a fresh base (spacer `sequencer.py` ensure_clone +
# prep_workspace, generalized). Give each independent task its own working tree reset to the base
# branch, so parallel runs never collide and each starts from a clean, current base. Use this for a
# QUEUE of independent tasks (the fleet/batch shape); for dependent steps in ONE feature use `--plan`.
#
# Usage:  bash worktree.sh <clone-or-worktree-dir> <base-branch> <run-branch>
# Example: bash worktree.sh .wt/issue-42 main loopkit/issue-42
set -euo pipefail

WT="${1:?usage: worktree.sh <dir> <base-branch> <run-branch>}"
BASE="${2:?base branch, e.g. main}"
RUN_BRANCH="${3:?run branch, e.g. loopkit/issue-42}"

# Create the isolated tree if it doesn't exist — a git worktree off the current repo (cheap, shares
# .git) or a full clone if you need process/dependency isolation. Worktree shown:
if [ ! -e "$WT/.git" ] && [ ! -d "$WT" ]; then
  git worktree add --detach "$WT" "origin/$BASE"
fi

# Reset to a clean, current base — no drift from a prior run.
git -C "$WT" fetch origin "$BASE"
git -C "$WT" checkout --detach "origin/$BASE"
git -C "$WT" reset --hard "origin/$BASE"
# Keep dependency dirs across runs (re-syncing every task is slow / network-fragile); adjust the -e list.
git -C "$WT" clean -fd -e .venv -e node_modules
# Delete a stale run branch so loopkit re-creates it fresh off the base (idempotent reruns).
git -C "$WT" branch -D "$RUN_BRANCH" 2>/dev/null || true

echo "worktree ready: $WT (reset to origin/$BASE; run branch $RUN_BRANCH will be created by loopkit)"
