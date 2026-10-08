---
uuid: "e644b08c-097d-5e50-bc0d-bc7426b7fa79"
type: "skill"
name: "ralph-maintainability"
description: "Start the maintainability Ralph loop: subagents improve readability and pay down debt, one focus area per iteration, behavior preserved."
allowed_tools: "Bash, Read, Write, Edit, Grep, Glob, Agent, AskUserQuestion"
disable_model_invocation: true
---
# Maintainability: Ralph loop

Starts a self-referential loop that raises the clarity of the codebase in the
current working directory (better names, smaller and more obvious units, code
where a reader would look for it, removed debt) while observable behavior stays
byte-for-byte the same. Each iteration takes one focus area, runs a team of
subagents through recon, findings, adversarial verification, safety net,
implementation, verification and landing, and records what changed plus a
re-scored clarity rubric so the next iteration starts from evidence.

Stack-agnostic: pre-flight detects the project's build and test tooling.

The loop prompt lives in `get_doc("ralph-maintainability-loop.md")`, between
its BEGIN_RALPH_PROMPT and END_RALPH_PROMPT lines. The loop state holds a
pointer to it, never a copy. The ledger lives in store docs, read and written
through MCP: `get_doc("ralph-ledger-in-store.md")`. Nothing goes under
`.agents/maintainability/`.

## Step 1: Preflight and launch

Run this. It checks the environment and hands the ralph-loop plugin a pointer
to the loop prompt. Then load the prompt with
`get_doc("ralph-maintainability-loop.md")`.

```bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "X Not a git repository." >&2; exit 1; }
cd "$ROOT"
case "$ROOT" in
  */.agents/worktrees/*|*/.claude/worktrees/*) echo "X Launch from the MAIN checkout, not a worktree." >&2; exit 1 ;;
esac

# -- ralph-loop plugin ------------------------------------------------------
SETUP="$(ls "$HOME"/.claude/plugins/cache/claude-plugins-official/ralph-loop/*/scripts/setup-ralph-loop.sh 2>/dev/null | tail -1)"
[[ -n "$SETUP" ]] || { echo "X ralph-loop plugin not installed." >&2; exit 1; }

# -- the loop prompt: a pointer to the store doc, read over MCP ---------------
PROMPT='Your loop prompt is the block between the BEGIN_RALPH_PROMPT and END_RALPH_PROMPT lines of the store doc ralph-maintainability-loop.md. If it is not already in your context (the first iteration, or after a compaction), load it with get_doc("ralph-maintainability-loop.md"). Then follow it verbatim, starting at its Pre-flight section.'

# -- starting picture -------------------------------------------------------
echo "== Working tree =="; git status --short | head -20
echo; echo "== Recent commits =="; git log --oneline -5

"$SETUP" --completion-promise 'CODEBASE-CLEAR' "$PROMPT"
```

If the working tree is dirty, say so and let the user commit before the first
iteration does anything. Do not commit or stash their work yourself.

## Step 2: Run the loop

Follow the loop prompt verbatim, starting at its Pre-flight section. The Stop
hook re-feeds it on every iteration. The local re-feed patch sends a short
pointer on most iterations, so an iteration may open with a pointer at
`.claude/ralph-loop.local.md`: re-read that file's body before acting,
especially after a compaction.

## Notes for the agent running this skill

- The loop uses the ralph-loop plugin's state file
  (`.claude/ralph-loop.local.md`), so it does not collide with `/ralph-cleanup`,
  which owns `.claude/ralph-cleanup.local.md`. Still run one at a time: they land
  commits into the same tree.
- Kill switches, in ascending order: `AskUserQuestion` → wind-down → sentinel;
  `/cancel-ralph`; `rm .claude/ralph-loop.local.md`.
- Universal ralph gotchas (session handoff, the re-feed patch, why there is no
  pause sentinel): `get_doc("ralph-loop-notes.md")`.
