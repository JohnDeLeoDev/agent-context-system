---
uuid: "5ce92618-2553-5296-9b3b-eb74dfffab44"
type: "command"
name: "handoff"
description: "Write a store handoff when user asks, before /clear, or when moving sessions or machines; never for context size alone."
disable_model_invocation: true
---
Capture the state of this conversation as a durable handoff doc in the agent-context store, so a fresh session can resume with `/resume-handoff` and lose nothing that mattered.

Argument: `$ARGUMENTS`: an optional short slug for the handoff (e.g. `l2-cache-fix`). If empty, derive a 2 to 4 word kebab slug from the task.

## Scope and path

## What goes in, and what must not

The handoff exists to carry what a fresh session **cannot recover on its own**. Everything else is waste that the next agent pays for on every request.

**Include:**

- **Decisions and their reasons**, including alternatives rejected and why. This is the highest-value content: it is unrecoverable and re-deriving it burns a whole session.
- **user's own words** on anything he corrected, chose, constrained or vetoed. Quote him verbatim, not your paraphrase.
- **What was tried and failed**, with the error. A trap re-entered is the most expensive failure a handoff can leave open.
- **State of the tree**: worktree path, branch, whether it is landed, what is uncommitted. Read it (`git status --short`, `git branch --show-current`); do not report it from memory.
- **The exact verify command** for the stack, copy-pasteable, and its last known result.
- **Store entities to warm**: the memory slugs and doc paths this task needed, so the next agent loads them at once and does not rediscover them.
- **Open questions for user**: anything you were waiting on.
- **The one next concrete action.**

**Exclude:**

- Anything LSP, `git`, or a file read answers on demand: file inventories, function signatures, directory trees, symbol lists. Point at `path:line`; never transcribe.
- Narrative of the session. No "then I looked at…". Facts and state only.
- Rules already in the global instructions or a memory. Cite the slug; never restate a rule.
- Content that duplicates a doc or memory. Link it.

## Honesty rules

- **Distinguish verified from assumed.** Anything you did not run or read is marked `ASSUMED:` on its own line. A handoff that states an unverified claim flatly is worse than no handoff: the next agent inherits it as established fact and builds on it.
- **Report failures as failures.** If tests fail, paste the failing output. If you skipped part of the ask, say which part and why.
- **A wrong premise stays flagged.** If the task rests on something that turned out not to exist, that goes at the top, not in a footnote.

## Body template

```markdown
# Handoff: <task, ≤10 words>

**Status:** open · **Opened:** <YYYY-MM-DD> · **Machine:** <machine_id> · **Project:** <name|global>

## The ask
<user's request, verbatim where he stated it.>

## State
- **Done:** …
- **In progress:** …
- **Not started:** …

## Tree
- Worktree: `<path>` · Branch: `<branch>` · Landed: yes/no
- Uncommitted: <git status --short output, or "clean">

## Traps hit
- <what was tried> → <the error> → <resolution, or "unresolved">

## Verify
```bash
<exact command>
```
Last result: <pass / fail + the failing lines>

## Warm these
- memory: `<slug>` · `<slug>`
- doc: `<path>`

## Open questions for user
- …

## Next action
<one concrete step>
```

Drop any section that is empty; do not emit a heading with "none" under it.

## Finish

1. `upsert_doc(path, body, title="Handoff: <task>", project=<name|None>, origin="agent")`.
2. Do not commit or sync by hand: the daemon commits every store write (memory `always-sync-store-after-modifying`).
3. Print the pickup line in its own fenced block, nothing else after it:

```
/resume-handoff <YYYY-MM-DD>-<slug>
```

Then stop. Do not summarize the handoff back to user; he can read the doc.
