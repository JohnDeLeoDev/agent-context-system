---
uuid: "e0a97811-57c1-5e2d-a2bb-c5c8c5b05648"
type: "command"
name: "resume-handoff"
description: "Load a store handoff, verify its claims, mark it consumed and report the next action. Start no work."
disable_model_invocation: true
---
Resume work handed off by a previous session. Counterpart to `/handoff`.

## Select

## Load

4. Read the handoff in full.
5. **Warm what it names, in one batch:** every `get_memory` slug and `get_doc` path under *Warm these*. Do this before anything else: it is why the section exists.
6. **Verify its tree claims; do not trust them.** The handoff is a prior agent's self-report, and a self-report is not evidence:
   - Worktree path exists; branch is what it says (`git branch --show-current`, `git status --short`).
   - Anything it calls landed is in the target branch (`git log`, after `git fetch` if a remote ref is involved).
   - Files it cites at `path:line` still say what it claims.
   Read-only git only. Any drift between the doc and reality is reported to user: it means the tree moved under the handoff.

## Report, then wait

7. Say, terse:
   - one line of current state,
   - the next action the handoff names,
   - any drift found in step 6,
   - any open question it left for user.
8. **Stop. Start no work.** The handoff's plan may no longer be what user wants; he confirms before you act. If he left open questions, ask them through `AskUserQuestion`.

## Stamp consumed

9. After a successful load, mark the doc consumed. Keep it, never delete it:
   `edit_body` the Status line to `**Status:** consumed <YYYY-MM-DD> · **Opened:** …`
   A handoff that turned out to be stale or wrong is stamped `**Status:** stale <YYYY-MM-DD> — <one-line reason>` instead, so the trail records why.
10. Do not commit or sync by hand: the daemon commits every store write (memory `always-sync-store-after-modifying`).

## Rules

- **A handoff is a self-report.** Wherever it disagrees with the tree, the tree wins and you say so.
- Anything it marked `ASSUMED` stays assumed. Do not promote it to fact by repeating it flatly.
- Never re-fetch what it already resolved; never skip verifying what it asserts.
