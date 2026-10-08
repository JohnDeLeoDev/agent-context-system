---
uuid: "6b014932-227e-5036-987a-989d7ffc97d6"
type: "command"
name: "ralph-cleanup"
description: "Start the universal iterative cleanup/DRY/optimization ralph loop in the current working directory"
allowed_tools: ["Bash"]
disable_model_invocation: true
---
Starts the universal cleanup-audit Ralph Loop. The prompt body is the store doc `ralph-cleanup-universal.md` (`get_doc("ralph-cleanup-universal.md")`), read over MCP; the loop state holds only a pointer to it. **Edit the store doc** (`upsert_doc` / `edit_body("doc", …)`). The body is stack-agnostic: it works in any project under the user folder, and stack auto-detection happens inside the prompt's pre-flight phase.

Uses our own setup + Stop hook (`~/.agent-context/global/scripts/ralph-cleanup-setup.py` + `~/.agent-context/global/hooks/ralph-cleanup-stop.py`), not the ralph-loop plugin's. The state file is `.claude/ralph-cleanup.local.md` (distinct from the plugin's `.claude/ralph-loop.local.md`).

```!
SETUP="$HOME/.agent-context/global/scripts/ralph-cleanup-setup.py"
if [[ ! -f "$SETUP" ]]; then
  echo "Setup script not found: $SETUP" >&2
  exit 1
fi
PROMPT_BODY='Your loop prompt is the store doc ralph-cleanup-universal.md. If its text is not already in your context (the first iteration, or after a compaction), load it with get_doc("ralph-cleanup-universal.md"). Then follow it verbatim.'
python3 "$SETUP" "$PROMPT_BODY"
```

After the setup script runs, load the prompt with `get_doc("ralph-cleanup-universal.md")` and follow it verbatim. On each iteration the Stop hook re-feeds a one-line pointer to the state file (the whole prompt only when the state file sets `full_prompt_every`), so re-read the state file body when unsure. It keeps going until any one of:

- You emit `<promise>DONE</promise>` in your final text block (one such tag anywhere in the last text block is enough)
- You run `touch .claude/ralph-cleanup.cancel` (hard kill switch from any tool call)
- You set `active: false` in `.claude/ralph-cleanup.local.md` (soft kill)
- You delete `.claude/ralph-cleanup.local.md` (immediate kill)

See the prompt's Termination section for guidance on when to wind down.
