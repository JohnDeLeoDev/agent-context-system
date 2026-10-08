# Ralph Loop notes

Universal gotchas for ralph loops in any project. Loaded lazily. The always-loaded global instruction carries a one-line pointer here.

There are two ralph-loop mechanisms on this machine:

| Command                       | Setup script                                            | State file                              | Stop hook                                              |
| ----------------------------- | ------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------ |
| `/ralph-cleanup` (ours)       | `~/.agent-context/global/scripts/ralph-cleanup-setup.py`              | `.claude/ralph-cleanup.local.md`        | `~/.agent-context/global/hooks/ralph-cleanup-stop.py`                |
| `/ralph-loop` (plugin)        | `~/.claude/plugins/.../scripts/setup-ralph-loop.sh`     | `.claude/ralph-loop.local.md`           | `~/.claude/plugins/.../hooks/stop-hook.sh`             |

The two mechanisms use different state files, so they can coexist without interference. The notes below apply to both unless called out.

## The plugin Stop hook carries two local patches (`ralph-patch-guard`)

The plugin's `stop-hook.sh` lives in a git clone under the plugin **cache**, so a plugin update silently reverts any edit to it. The `ralph-patch-guard` SessionStart hook re-applies both patches, is idempotent, and fails loudly if upstream moves the code a patch attaches to. Both patch bodies are defined in that hook and nowhere else — never hand-edit the cached hook, and never restore it from a whole-file copy.

- **A — session handoff.** Upstream `exit 0`s whenever the state file's `session_id` differs from the session hitting Stop, so any handoff (`/clear`, resume, compaction takeover) silently disowns the loop. Patched: a differing `session_id` means "someone else owns it" only while that owner is alive (its transcript touched in the last 10 min); otherwise this session adopts the loop and rewrites `session_id`.
- **B — re-feed policy.** Upstream inlines the entire prompt on every fire, so a long prompt is re-sent every turn forever and the loop dies of context exhaustion rather than of finishing. Patched: the full prompt on iteration 1 and every `full_prompt_every`-th after (state-file frontmatter key — **opt-in: absent or unparseable means 0, which is never re-fed**; see the patch body in `ralph-patch-guard.py`, where `FULL_EVERY=0` is the fallback and only `1` or `>1` can set `SEND_FULL`); in between, a short pointer at the state file. **So an iteration may open with a pointer rather than the task text — re-read the state file body, especially after a compaction.** No delay is added either way; the loop still fires immediately. Set `full_prompt_every: 1` in the state file to get the old always-full behavior.
  Our own Stop hook (`ralph-cleanup-stop`, behind `/ralph-cleanup`)
  follows the same policy, and invariant `ralph-refeed-is-a-pointer` holds
  every re-feed site to it.

  **The pointer stays one line** (`Ralph N, continue, do not restart. <state file>`): the
  `reason` field renders as a Stop hook error block on every fire. Anything more goes in
  the state file body. **Change the policy in the store hook `ralph-patch-guard`**,
  never only in the plugin's `stop-hook.sh`, because a plugin-only edit is reverted the next time
  anything materializes.

## Completion-promise tag — when to write the literal sentinel

When a ralph loop is active with a completion promise (e.g., `DONE`):

- **Ours (`/ralph-cleanup`)**: the Stop hook scans all `<promise>...</promise>` occurrences in your last text block and terminates if any one of them equals the promise. Inline mentions inside backticks/code fences do count — paraphrase ("the DONE sentinel", "the completion-promise tag") when you need to discuss the mechanism.
- **Plugin (`/ralph-loop`)**: the Stop hook regex matches the first `<promise>...</promise>` tag only. A prior inline mention can shadow your real terminator, leaving the loop running.

For both: never write the literal tag in iteration summaries, status updates, or anywhere you don't mean to terminate.

## There is no pause sentinel: use `AskUserQuestion`

Neither Stop hook implements a pause tag. Both inspect the last assistant message for the **completion promise only**; every other ending re-feeds the loop prompt.

**Do this instead:** call `AskUserQuestion`. It blocks *inside* the turn and returns the user's actual answer, so you act on the decision in the same turn rather than ending and hoping. If the answer means the loop should stop, stopping is a deliberate act — `rm .claude/ralph-loop.local.md` (plugin, what `/cancel-ralph` does) or `touch .claude/ralph-cleanup.cancel` (ours) — and say so. If the answer means the work continues, carry on; the re-feed is then harmless.

## Termination — escape hatches by loop mechanism

**For `/ralph-cleanup` (ours)**, the loop ends on any of these:

1. Your last text block contains `<promise>DONE</promise>` (any occurrence, not just the first) — soft exit, requires the loop's task to genuinely be done.
2. `touch .claude/ralph-cleanup.cancel` — hard kill switch, fires from any Bash call. The hook deletes both this sentinel and the state file on the next Stop event.
3. Set `active: false` in `.claude/ralph-cleanup.local.md` frontmatter — soft kill.
4. `rm .claude/ralph-cleanup.local.md` — immediate kill, the next Stop event sees no state and exits cleanly.

**For `/ralph-loop` (plugin)**, the loop ends on:

1. `<promise>DONE</promise>` (or whatever phrase was configured), matching only the first tag — unreliable in practice.
2. `/cancel-ralph` plugin command — **removes** the plugin's state file (`rm .claude/ralph-loop.local.md`).
3. Max iterations reached (if `--max-iterations` was set).
4. `rm .claude/ralph-loop.local.md` — escape hatch the plugin docs don't mention but the hook honors.

The wind-down pattern:
1. `AskUserQuestion` to offer wind-down vs. continue. It blocks inside the turn, so the answer arrives before you end.
2. Act on the answer in that same turn. If they choose to stop, do it yourself — `rm .claude/ralph-loop.local.md` (plugin) or `touch .claude/ralph-cleanup.cancel` (ours), or emit the promise sentinel if the loop's task is genuinely complete — and say which you did.
3. Never end a turn holding an unanswered question and expect the loop to wait. It will not.

## "Wind down the loop" means stop everything, now

1. `TaskStop` every running subagent, and confirm with `ListAgents`.
2. Land and record the iteration in flight in the loop's ledger, then **delete or rename** the
   state file and check it is gone. Never disarm the plugin loop by editing `active: false`:
   its `stop-hook.sh` tests only that the file exists, and walks up the tree for the same name,
   so the flag is a silent no-op and a copy in a parent directory still arms it.
3. Check nothing under `.agents/claude/` re-projects an arming file at the next SessionStart.
4. Say plainly that it is stopped, and how to restart it.
5. Do the rest of the work **inline**, with no background agents unless user asks.

## Loops suppress session-end record-keeping

While any ralph loop is active, its Stop hook intercepts every session-end event and re-feeds the loop — short-circuiting the normal Stop-hook chain. This means normal session-end record-keeping (memory updates) never happens during the loop. Per-iteration narrative belongs in the loop's own ledger, held as a store doc and read/written through MCP, per `get_doc("ralph-ledger-in-store.md")` (`cleanup/CLEANUP_LOG.md` for cleanup loops, or whatever the loop's prompt body specifies).

Don't manually write project records from inside the loop. (Durable project state lives in the agent-context store, `get_doc("store-operations.md")`, and there is no task tracker.) When the loop is canceled, normal session-end resumes and any memory updates happen then.

## Subagents stay inside the worktree

Once the orchestrator creates the iteration's worktree, the session's working directory
is the worktree. Every subagent inherits it. A subagent prompt that names the main
checkout — or any `cd` back to it — makes each of that agent's shell commands ask the
user for permission, because the harness cannot resolve a relative path across the `cd`
and a `Read()` deny rule is configured.

Rules, and they cost nothing to follow:

- **Name the worktree path in every subagent prompt, never the main checkout.** A
  freshly-created worktree is at HEAD with no edits, so its files are byte-identical to
  the main checkout. There is nothing to gain by reaching across.
- **Tell each subagent not to `cd` out of its working directory.** Plain relative paths
  (`grep -rn "pattern" src/app`) work and never prompt.
- **The ledger is a store doc**, written by the orchestrator through MCP
  (`get_doc("ralph-ledger-in-store.md")`). No subagent writes it, and nothing goes under
  `.agents/<loop>/`.
- **Keep shell commands plain.** The worktree-isolation guard refuses a command whose
  program is computed at runtime inside a loop or a substitution, because it cannot
  prove the thing is not `git`. Split it into separate, literal commands.

## Guardrails from the wider Ralph experience

Our loops predate most of the published retrospectives. These four come from
HumanLayer's history of the technique and Huntley's own framing, and none of them
conflict with how our loops already work — they are about *sizing and inputs*, which is
where the published failures cluster.

1. **Small batches on a schedule beat one long overnight run.** A large changeset is
   hard to review, and a bad early iteration compounds silently through every later one.
   Prefer several bounded runs over one unbounded one. Our loops already have kill
   switches; what they lack is a habit of scoping the *input* small.
2. **Re-run on fresh code rather than merging a conflicted branch.** When a loop's output
   collides with work that landed underneath it, rebasing a large machine-generated
   changeset costs more than regenerating it. This matters more here than in most
   setups, because destructive git is denied fleet-wide, so a messy rebase has fewer
   escape hatches.
3. **Review the intermediate artifacts, not just the final diff.** Where a loop writes a
   spec, a plan, or a findings ledger before implementing, that document is the cheapest
   place to catch a wrong direction. Once implementation starts, the spec's errors are
   spread across every file it touched.
4. **A loop is the wrong tool while the end state is still undefined.** Ralph's strength
   is naive persistence toward a *stated* target; its documented failure mode is an
   exploratory phase where the target moves. Use a normal session to decide what "done"
   means, then hand that to the loop.

The underlying principle Huntley states is worth keeping in view: the technique is
"deterministically bad in an undeterministic world" — it works because the filesystem and
git hold the state, not the context window. Anything that quietly makes the context window
load-bearing again (re-feeding a whole prompt, relying on a summary surviving compaction)
takes the property away. That is the same reasoning behind
patch B above.

See also this doc's *Ralph loops vs the native primitives* section, below, for how these
loops line up against `/goal`, `/loop` and dynamic workflows, which did not exist when
they were written.

## A loop turn's visible output is one line

Each iteration's visible output is one line: `done:` / `blocked:` / `no-op:` plus why.
Everything else goes in the loop's ledger, not the terminal. A loop that narrates each turn
buries the one turn that mattered.

Two exceptions, both of which earn full prose:

- an `AskUserQuestion` (see "There is no pause sentinel" above), and
- a regression user must know about now.

## Testing a ralph loop in pi: never use `--no-session`

| flags | result |
|---|---|
| real flags (tools + session) | iteration 1 → 2 → 3 → 4 across turn boundaries |
| `--no-tools` | reaches the cap. Innocent. |
| `--no-session` | never leaves 1. **The culprit.** |

`--no-session` isolates away the mechanism under test, because the re-feed handler reads
`ctx.sessionManager` and re-feeds via `pi.sendUserMessage`. Use `--no-tools` alone: it
gives the same safety and leaves the loop intact.

Two rig rules that still hold, from the same records:
- **Never pipe pi's stdout** (`pi … | tee`). That takes away its TTY, pi answers once
  non-interactively and exits, and it reads as a re-feed failure.
- **Instrument with the state file's own `iteration:` field.** The extension writes
  N+1 to disk *before* `sendUserMessage`, so a re-feed is observable at 100ms
  resolution without an interactive session.

Do not diagnose a stop from the absence of a `.stopped` tombstone. It is currently never
written, so it is a broken instrument, not evidence.

## Ralph loops vs the native primitives (`/goal`, `/loop`, dynamic workflows)

Our ralph loops (`/ralph-cleanup`, `/ralph-context-audit`,
`/ralph-maintainability`) were built before Claude Code had `/goal`, `/loop` or dynamic
workflows. This is the comparison, so the migration question gets decided on evidence
once, not re-argued each time it comes up. Nothing has been migrated.
Read this, pick per loop. `/ralph-context-audit` is retired, merged into
`/context-audit`; the workflow-shape argument below may still apply to its `inventory` mode.

### What each primitive is

| | Ralph (ours) | `/goal` | `/loop` | Dynamic workflow |
|---|---|---|---|---|
| Re-fires on | Stop hook, every turn | Stop hook, every turn | A time interval | n/a: a script runs to completion |
| Stops when | Promise sentinel, cancel file, `active: false`, state file deleted | A **separate model** judges the condition met or impossible; or an unrecoverable error | You stop it, or the model decides it is done | The script returns |
| Who judges "done" | The loop's own agent | A fresh evaluator that did not do the work | The loop's own agent | The script |
| State lives in | Filesystem plus git | Session and condition string | Session | Script variables |
| Survives `/clear` | Yes (state file on disk) | No | No | Within the session only |
| Portable off Claude Code | Yes: shell plus a state file | No | No | No |

### The one real capability gap

`/goal` grades with a different model than the one doing the work. Our loops ask the
agent that just did the work whether the work is done, which is the weakest possible
grader and the exact bias an adversarial review step exists to remove. `/goal` runs the
condition past a fresh small model after every turn, and it distinguishes *not yet met*
from *impossible*: our loops have no notion of "impossible", they keep going.

That matters for `/ralph-maintainability`, whose success condition ("observable behavior
provably preserved") is the kind of claim the author should not score.

### What we would lose by migrating

- **Portability.** `/goal`, `/loop` and workflows are Claude Code features. The ralph
  loops are a shell script plus a markdown state file, and this fleet deliberately runs
  copilot, antigravity, opencode and pi against the same store. A loop that only works
  on one harness is a step away from the whole point of the store.
- **`/clear` survival.** Ralph state is a file; a goal is session state. Long cleanup
  runs routinely outlive a session.
- **Two guards with earned scars.** The re-feed policy (patch B, see this doc's `ralph-patch-guard`
  section) and `ralph-patch-guard` exist because of specific failures. `/goal` re-sends the whole
  conversation each turn as part of its design, which is the cost patch B avoids.
- **The kill switches.** `touch .claude/ralph-cleanup.cancel` works from any tool call on
  any machine. `/goal clear` needs an interactive session.

### Where a dynamic workflow is the better shape

`/ralph-context-audit` swept global then every project round-robin, and its intermediate
findings all landed in the agent's context even though only the applied fixes mattered.
That is the textbook case for a workflow: the script holds the loop and the intermediate
results, and only the final report reaches a context window. It could also fan the
per-project passes out in parallel; the round-robin budget rule approximates that by hand.

Against that: workflows are Claude-Code-only, capped at 16 concurrent agents, and cost
meaningfully more per run.

### Recommendation, per loop

- **`/ralph-cleanup`**: keep as-is. Long-running, `/clear`-surviving, cross-harness. No
  primitive improves on it.
- **`/ralph-context-audit`** (retired, merged into `/context-audit`): was the
  strongest workflow candidate on context economics; correctness was not the deciding
  factor. Prototype on one project before committing; the candidate is now
  `/context-audit`'s `inventory` mode.
- **`/ralph-maintainability`**: keep the loop; borrow `/goal`'s idea, not its feature.
  Have the loop's own stop condition graded by a fresh subagent that sees only the diff
  and the invariant. It should never see the reasoning that produced it. That gets the
  independent grader while keeping portability.

### Do not adopt

- **`/loop`**: fires on a clock; it does not test a condition. Strictly worse than what
  we have.
- **Agent teams**: about 7x tokens, experimental, and enabling them converts any
  *named* subagent into a teammate with no warning, which collides with the model-tiering
  rule. There is also a published case of 24 idle teammates running for two days
  unnoticed.
