#!/usr/bin/env bash
# examples/skills/distill.sh — a GENERIC skills distiller for `run --skills-distiller <cmd>`.
#
# After a run reaches DONE, this turns the solved diff into ONE short, GENERAL lesson for future
# similar goals (not a restatement of this fix). Its stdout becomes the skill guidance; loopkit
# bounds the length and scrubs secrets before storing/rendering it. Non-zero exit or empty output
# means "learn nothing from this run" — better than learning noise.
#
#   run --skills ./skills --skills-distiller "GATE_BASE=origin/main bash examples/skills/distill.sh"
#
# Env: GATE_BASE (default origin/main), GATE_JUDGE_CMD (headless summarizer; default `claude -p`).
# CWD = the workspace (the just-solved tree).
set -uo pipefail

base="${GATE_BASE:-origin/main}"
git rev-parse --verify "$base" >/dev/null 2>&1 || base="HEAD~1"
cmd="${GATE_JUDGE_CMD:-claude -p}"

diff="$(git diff "$base"...HEAD 2>/dev/null)"
[ -z "$diff" ] && diff="$(git diff HEAD~1..HEAD 2>/dev/null)"
[ -z "$diff" ] && exit 1   # nothing solved to learn from

prompt="A coding loop just solved a task. From the diff below, write ONE reusable lesson (2-3
sentences) that would help a DIFFERENT future task of the same KIND — a general technique, pitfall,
or pattern, NOT a description of this specific change. Name no project-specific files. If there is no
generalizable lesson, output nothing.

\`\`\`diff
$diff
\`\`\`"

printf '%s' "$prompt" | $cmd 2>/dev/null
