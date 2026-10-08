Material split out of **Global Agent Instructions** because it is *rationale* or a
*procedure needed at one specific moment*, not a rule that has to be in front of the model
on every turn. Nothing here was dropped — the always-loaded instruction keeps every rule
and points here for the detail.

Load this when you are (a) about to spawn workers and want the tiering rationale, (b) about
to recommend clearing context and need the handoff-prompt contents, (c) writing or pruning
memory, (d) filing an audit observation, or (e) weighing whether a claim needs verifying.

## Model tiering — rationale and harness notes

The rule is the **role→tier split**, not any specific model name. **Never write model
version numbers into the instruction** — resolve tiers at spawn time from what the
harness offers, so new model releases never require editing the rule:

- Read the orchestrator's tier from the live session (the harness reports the running
  model); the worker tier is the next family down that the harness exposes, but never
  below `sonnet`. **Sonnet is the floor for every worker and reviewer: never spawn one on
  `haiku`.**
- Address tiers by their **version-independent alias**, never a versioned id — e.g.
  Claude Code's `model: sonnet` / `opus` / `haiku`. A versioned id (`claude-*-5`, `4.8`)
  is a bug here. The alias can lag a release (2.1.284 still ran `sonnet` as
  claude-sonnet-5), so the id `sonnet` runs is set in one place: `ANTHROPIC_DEFAULT_SONNET_MODEL`
  in home-settings-sync.py's MANAGED_ENV, which agents-pin.py also applies to pi and
  opencode workers (policy).
- If the orchestrator is already running the lowest tier the harness offers, workers
  stay at that tier — never spawn a worker above the orchestrator.

**The tiers are configuration.** Five global `agent_definition` entities carry the
role→tier split and materialize into each harness's own agents dir:

| Definition | model | effort | tools |
|---|---|---|---|
| `worker-explore` | sonnet | low | Read, Grep, Glob |
| `worker-review` | sonnet | medium | Read, Grep, Glob, Bash (no Edit/Write) |
| `worker-implement` | sonnet | medium | Read, Write, Edit, Grep, Glob, Bash |
| `worker-ops` | sonnet | medium | Bash, Read, Grep, Glob (no Edit/Write) |
| `worker-device` | sonnet | medium | Read, Write, Grep, Glob, Bash, Skill, the Xcode device tools |

Each also carries the `mcp__agent-context__*` read tools (and, for `worker-implement`, the
write ones) plus the project's LSP bridge. That grant is load-bearing: `ToolSearch` can only
surface a tool the session was already granted, so a worker sent to read or edit the store
without it fails with no matches and no way to recover. A project-scope definition
of the same name shadows the global one entirely — when you add one, copy the full tool list
across rather than listing only the project's own additions.

Spawn one of these by name rather than passing `model:` by hand — an unset model means the
worker silently inherits the lead's tier, which independent reports name as one of the two
costliest subagent mistakes (loose tool scopes being the other). Each definition also
carries the destructive-git prohibition in its own body, so the orchestrator does not have
to restate it in every worker prompt. Edit them with `upsert_agent_definition`; never write
the projected file. `effort` is the second dial the old rule was missing: Anthropic names
`low` as the subagent setting, and it is cheaper than a model downgrade on its own.

Harness notes (mechanism, not model names):

- **Claude Code:** `fork` subagents always inherit the orchestrator's model and cannot
  be downgraded — prefer non-fork delegation for worker tasks; use `fork` only when
  inheriting the orchestrator's full context genuinely matters.
- **Other harnesses:** same role→tier split against their own default/lower tiers.

## Handoff prompt — what a clear recommendation must contain

The rule (always loaded): never recommend clearing context without immediately giving a
copy-pasteable starting prompt for the next session, in its own fenced code block, ready
to paste with zero editing. It must stand alone — the next agent sees none of the
conversation. This applies to every form of the recommendation: "let's clear", "/clear
and restart", "a fresh session would help", or ending a turn at a stopping point you
flagged as a good clear point.

Contents:

- the goal in one line (what we're doing and why)
- where the work lives — repo, worktree path, branch
- what's already done, and what's not done
- the concrete next step to take first
- constraints/decisions already settled (so they aren't relitigated)
- exact build/test/verify commands
- pointers to store entities by slug — a `get_doc` / `get_memory` call naming the
  real path or slug — rather than restating their contents

## Memory hygiene — the long form

The rule (always loaded): search before writing, prefer updating a slug over adding a
near-duplicate, keep descriptions ≤140 chars, tier narrow memories lazy at write time.
The reasoning and the mechanics:

## Self-improvement reporting — the argument contract

The rule (always loaded): when this turn's actual work exposes a concrete workflow or rule
defect, file `add_audit_observation(observation, scope, project, evidence, severity)`.

- `scope` is `"universal"` (touches global config) or `"project"`; `evidence` is a file:line
  reference or quoted text; `severity` is `blocker` (the task could not be completed at all),
  `high`, `normal` (default) or `low` — it sets the drain order, nothing else.
- **Pass each as its own tool argument.** Inlining `<parameter name=…>` markup into the
  observation text loses `evidence`/`project` — the two fields the next audit filters and
  cites by. The `audit-observation-guard` hook **blocks** that call, along with an empty
  `evidence` and a `scope: "project"` with no `project`.
- **Amend, don't re-file.** `update_audit_observation(id, note=…, evidence=…, recurred=True,
  severity=…)` edits an open observation in place; `resolve_audit_observation(id,
  resolution_note=…)` closes it. Re-filing produces several records for one bug.
- Fire only on: a rule contradicting current code, stale memory verified against source, a
  repeated tool sequence a rule could shortcut, an instruction violation, or user pushback.
  Stay silent when the observation is vague, the fix is already covered, or it is already
  logged.

## Verify before you trust: the reasons

The rule (always loaded): check the system, not your memory of it. Each clause has a reason:

## Why this doc exists at all

Every byte of an `always`-loaded instruction is paid by every session on every machine
and every project. A rule earns that slot; a rationale or a once-in-a-session
procedure does not. When you add to the global instruction, ask which of the two you are
writing, and put the second kind here.

## Read width: why the rule exists

**Why Read outranks everything else by an order of magnitude while each call looks harmless.** A tool result is not paid once. It is cache-written when it lands, then cache-*read* again on every subsequent request in the session. A 900-line file read at turn 3 is still being paid for at turn 60. That compounding is what `tools_by_amortized_context` measures and what raw result-token counts hide.

**Why the rule ships with a hook.** The Read tool's own description already says to read only the part needed, and that advice alone did not change the behavior. `read-width-nudge` (PostToolUse on Read) fires on a file of 600 lines or more read with no `offset`/`limit`.

**Why a nudge and not a deny.** Whole-file reads are legitimately right sometimes: a file about to be rewritten, a config that must be seen entire. A guard that fires on honest work gets switched off, and then there is no guard at all. It self-silences — once per file, five times per session — so it cannot decay into background noise.

**What to do instead, in order of preference.** One region → grep or LSP for the anchor, then Read with `offset`+`limit`. Breadth across many files → `worker-explore`, which reads excerpts in its own context and returns the conclusion rather than the files; that is the whole point of a read-only worker, and it is why the definition exists. Whole file → fine, when you need the whole file.

## Tool use and reads: rationale

### Why "use the purpose-built tool" needs enforcement, not prose

The rule keeps losing to a *recency* effect, not a comprehension one. Claude Code's
bypass-permissions mode injects a per-turn note preferring `cat`/`grep`/`sed`/heredocs over
Read/Edit/Write. It arrives mid-turn, long after the instruction loaded, so it wins on
position. Prose loses this fight, which is why the enforcement
is mechanical: `require-worktree-edit-bash` for shell writes, `block-shell-file-read` for
shell reads and searches.

What a shell lookup costs relative to the tool: no line-accurate anchoring, no
staleness check, no diagnostics, no hook coverage, no undo. The answer it returns is worth
less than the tool's even when it is correct.

**The one sanctioned fallback**: a tool absent from the session's roster. With `Grep` and
`Glob` missing, Bash is the only option and a flat deny in `block-shell-file-read` would
deadlock. The hook checks availability, and the agent says when it takes the fallback.

### Redundant reads — the second half of the Read problem

Re-reading a whole file the session already holds pays the full cache-write again, then a
cache-read on every remaining request, for no new information. `block-redundant-read`
denies it; `read-width-nudge` covers width. Neither catches the other: the typical
duplicate is a short file, far under any width threshold.

**It denies, and here is what made a deny safe.** The first guard could only warn, because
subagents run under the parent's `session_id` and a session-keyed ledger cannot tell "the
orchestrator is re-reading what it already has" from "worker-explore, context empty, is
reading it for the first time" — denying would have broken the delegation pattern the rule
exists to encourage. `block-redundant-read` keys its exemption on `agent_id`
instead, so a worker's first read of a file always passes while the orchestrator's second
whole-file read of an unchanged file is denied. Bounded reads are never touched, which
leaves the always-legal move available, and the ledger is cleared on compaction so a
summarized-away file can be read again.

**`transcript_path` does not tell a worker from the orchestrator.** A worker's read fires
the PostToolUse hook under the parent's `session_id`, with no `/subagents/` component in
`transcript_path`. `agent_id` is the discriminator.

### Where reads belong

Route breadth to `worker-explore` by default. Discovery done in the orchestrator runs at
the top tier's price, and the tiering only saves where it is used. How to read a report:
`get_doc("token-usage-tracking.md")`.

### How the read guards meter

- Whole-file reads: `block-redundant-read` refuses a repeat of an unchanged file and caps
  an unbounded read at 1,200 lines.
- Bounded reads have their own ledger (2 free, 3 and 4 nudged, the 5th refused), kept
  apart from the whole-file ledger so a region read and a whole-file read never pair as
  duplicates.
- The ledger key is the path relative to the checkout, so the same file in the main
  checkout and in a worktree counts as one. The mtime signature stays per physical copy.
- A guard's documented escape becomes the dominant behavior, so meter the escape.
- Enforcement belongs in a PreToolUse hook. `read-width-nudge` is PostToolUse, where a
  deny would cost the full context and show an error, so it measures and never blocks.

### A command that prompts reads EOF

Every route an agent has is non-interactive — the `!` prefix the harness offers for "hand
this to the user" included, despite its own guidance naming `gcloud auth login` as the
example. A prompting command therefore reads EOF, and a tool that accepts empty input as a
value stores it and exits 0.

Give the command to user to run in his own terminal. The shape to watch for is a tool that
treats an empty prompt answer as a legitimate value rather than an error.

### Verifying the auto-compact threshold

Do not re-derive it from the binary. The only reliable check is `compact_boundary`
`preTokens` in `~/.claude/projects/*/<session>.jsonl`. Match on the quoted JSON key
`"compact_boundary"`; a bare substring grep also matches prose and tool results that merely
mention it.

**The two knobs are one setting and must be managed together.** The percentage is taken of
`autoCompactWindow`, not of the model's window: `trigger = floor((autoCompactWindow - 20000)
* pct/100)`. Moving one knob alone leaves the other wrong.

home-settings-sync's `MANAGED_TOP`/`MANAGED_ENV` set `autoCompactWindow=1000000` and
`pct=61` together, predicting 597,800, which is where the always-loaded 40/60 ladder
assumes the backstop sits. The ladder needs no per-model row.

Re-measure with the `compact_boundary` check after any harness update.

### Shell gotcha worth remembering

This fleet's interactive shell is zsh, which does **not** word-split unquoted parameter
expansions the way bash does. `files=$(find ...); for f in $files` silently iterates once
over the whole newline-joined string ("file name too long") and yields plausible-looking
zeros rather than an obvious failure. Use `find -exec ... +` or `find -print0 | while IFS=
read -r -d ''`. A zero from a shell loop is not evidence until the loop is proven to iterate.

## Instruction rationale

### Why there is no task tracker

user retired it. The old mandate named one system as the only permitted one and forbade
every alternative, so an agent that found the system missing had no sanctioned move. A
rule that names one mechanism and bans the rest becomes a deadlock when the mechanism is
gone.

### Why memory tiering is decided at write time

Deferring the tier to a later sweep means the memory pays a description slot in every
session until someone runs that sweep. `get_usage_report()` ranks always-loaded memories
coldest-first, but its zeros are meaningless until `tracking_days` reaches 30 — so the
write-time decision is the only one that is both timely and informed. `lazy` rosters by
slug rather than hiding, which is what makes aggressive tiering safe.

### Why core systems get explicit probes

Each one fails open, and each degraded mode is indistinguishable from its working mode: a
dead LSP falls back to grep, which still returns hits; an unloaded store leaves a session
that still answers fluently; a half-done materialization exits 0. Nothing raises, so
nothing surfaces without a probe. That is why adding a core system means adding its probe —
a system with no probe is one whose failure nobody will ever see.

Two consequences that are easy to get wrong:

- **A degraded system's negative answer is not evidence.** A language server that is up and
  cannot resolve a symbol reports "not found", which reads exactly like a symbol that does
  not exist.
- **Cold index, always.** sourcekit-lsp answers negatively while it indexes (measured:
  wrong at t=0, correct at t=10s). `lsp-canary` warms it at SessionStart, but a "not found"
  in the first seconds of a session should be asked again before it is believed.

A fault that silently repairs itself every session is still a fault, which is why a
`SELF-REPAIRED` block is worth one line even though nothing is broken any more.

Repair is automatic only where it is cheap, idempotent and unambiguous — `self-heal.py`'s
recipe table, never a command read from a verdict file. Anything slow or side-effecting
(rebuilding an app to repopulate DerivedData) is surfaced as an exact command and left for
user. Per-probe detail: `get_doc("core-system-health.md")`.

### Memory types

A lookup needed at the moment you write a memory.

- **`feedback`** — user's corrections, and approaches he has confirmed. The type that most
  often deserves `always`, because it exists to stop a repeat.
- **`project`** — live work on something in flight. **Prune it once the work stabilizes**;
  a `project` memory that has outlived its work is the most common husk.
- **`reference`** — pointers to external systems, quotas, aliases, build provenance.
  Usually `lazy`: needed at point of use, not before.
- **`user`** — user's role, preferences, and standing facts about him.

The rest of the contract is unchanged and lives in "Memory hygiene — the long form" above:
description ≤140 chars because it loads into every session for that scope, narrative and
`[[links]]` in the body, superseding means folding the lesson in and deleting the old slug,
and tiering is decided at write time.

## Fifth split: guardrail rationale

AGENTS.md is `~/.claude/CLAUDE.md` on every machine and loads before the store, in every
session in every project. It keeps the rules. The reasons live here.

### Why multi-turn state goes to `~/.local/state/agent-scratch/<session-id>/`

A turn-end cleanup of `.agents/tmp/` destroys a long task's ledger. Nothing sweeps the
state directory.

### Why `git stash list`, `show` and `drop` are allowed

A read loses nothing, and a drop removes an entry already judged dead. Blocking them
left stash entries no operator could reach, not even through user's own `!` escape hatch.

### Why `guard-git-write` exempts two writes

The first commit in a repo with no commits: `git worktree add` needs a HEAD, so the
worktree mandate could not be met at all. A write user authorized with a single-use
token (`git-write-consent.py`): some writes he asks for, such as landing one feature
branch into another, have no worktree route.

### Why "ask the guard before you ask the human"

The exemption list used to be visible only by triggering a refusal, so consent tokens
got minted for writes that were already allowed. `guard-git-write.py --would-block`
answers for free.
