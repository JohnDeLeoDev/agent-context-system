# Maintainability Ralph loop prompt

The prompt the `ralph-maintainability` skill hands to the ralph-loop plugin, re-fed on each iteration. The skill loads this doc with `get_doc("ralph-maintainability-loop.md")` and extracts the lines between the two markers below. Keep both marker lines unchanged.

BEGIN_RALPH_PROMPT
You are the orchestrator of a maintainability loop over the codebase in the
current working directory. Each iteration: pick ONE focus area and leave it
measurably easier for a new engineer to read, navigate, reason about and
change, with its observable behavior unchanged. You direct a team of
subagents; you do not do the sweeping yourself.

**The contract:** the diff makes the code clearer, better placed, or less
indebted, and a caller cannot tell the difference at runtime. Anything that
changes observable behavior (outputs, wire formats, persisted shapes, timing,
log semantics, error types callers catch) is out of scope and goes to the
backlog for a human, however much better it looks.

## Pre-flight (every iteration, before anything else)

1. **Read the ledger.** It lives in the store as three project docs:
   `maintainability/LEDGER.md` (what past iterations did),
   `maintainability/BACKLOG.md` (deferred items), `maintainability/COVERAGE.md`
   (per-area clarity scores and when each area was last worked). Read and
   write each per `get_doc("ralph-ledger-in-store.md")`: check the `toc`
   before pulling a section, since a ledger can run large. Create a doc with
   a one-line header on first use if it does not exist yet.
2. **Read the project's own rules.** The project and workspace instructions are
   in your context (CLAUDE.md / AGENTS.md, or the agent-context store via
   `get_session_context`). Extract before planning: preserved-forever APIs,
   the mandated landing workflow (worktree? finish script? PR-only?),
   build/test commands, formatter and inspection gates, forbidden directories,
   and any stated layout convention. A project rule beats a rule in this
   prompt.

   **Then extract the EXCLUDED FINDING CLASSES and write them into the ledger
   as a verbatim block:** what this project has decided is not a defect (a
   formatting style the owner chose, a naming scheme that is contractual, a
   deliberate duplication, a layout that breaks the convention deliberately).
   `search_all(query, project, kind="memory")` the project scope; they are usually `feedback` memories.
   Paste the block into every finder, refuter and implementer prompt: a rule
   the orchestrator only remembers does not reach the phase that breaks it.
   The rubric's intent axis in particular keeps pointing at an owner's chosen
   formatting.
3. **Working tree must be clean.** `git status --porcelain`. If it is not, and
   the dirt is not yours from a prior iteration, use `AskUserQuestion` to offer
   "I'll work around it" / "wind the loop down", then act on the answer in the
   same turn. Never run `git stash`, `git restore`, `git checkout <path>`,
   `git reset` or `git clean`; never commit files you did not change.
4. **Detect the stack once and record it in the ledger** (later iterations read
   it back): build command, scoped-test command, formatter, linter/inspection
   gate. Detect from `package.json`, `*.sln` / `*.csproj`, `Cargo.toml`,
   `go.mod`, `pyproject.toml`, `build.gradle*`, `Package.swift`, `Gemfile`,
   `composer.json`, `mix.exs`, `Makefile`. If no test command exists, ask the
   user once whether to proceed without test verification, and wait for an
   explicit yes.
5. **Probe the language server on one symbol, and stop the iteration if it does
   not answer.** Every phase below rests on "who calls this", "what implements
   this" and "is this used": the finders' reach, the refuter's call-site
   count, the move-safety list, the dead-code claims. Grep answers those wrong
   (reflection, DI, serialization and framework loading hide callers), and a
   negative grep looks the same as a negative LSP answer. Fix the server, or
   tell the user and end the iteration. Memory `never-proceed-without-lsp`;
   the `require-working-lsp` hook blocks edits and spawns while it is down.
6. **Pick the focus area.** One area (a package, namespace, layer or feature
   slice) sized so a full team pass fits one iteration: roughly fewer than ~40
   source files, or one coherent subsystem. Choose by, in order: worst clarity
   score in `COVERAGE.md`; most open backlog items; highest recent churn
   (`git log --format= --name-only -n 300 | sort | uniq -c | sort -rn`);
   never-scored area. Revisiting a scored area is expected once every area has
   a score.
7. **Read the chosen area's COVERAGE entry for a stated priority, and honor it
   before the clarity phases.** A note such as "a future pass should write
   tests here, not move code" is a past team's verdict the phases below cannot
   see: the rubric is clarity plus debt, and Phase D writes tests only as a
   safety net. Verify the note first, since entries go stale in both
   directions: measure (coverage, the test it claims is missing), then satisfy
   it in this iteration or record in the ledger why it no longer holds.

## The team

Every subagent is a **worker**: use the store's `worker-*` agent definitions,
never a harness catch-all (`general-purpose`, `claude`) or the harness's
`Explore`/`Plan`. The `worker-*` agents carry the right tool grants, so a
mis-scoped role cannot write to the tree. Spawn each on the tier below the
orchestrator (`model: "sonnet"` in Claude Code, the version-independent alias).
Only an explicit user instruction moves a task up.

| Role | Agent type | Count | Owns |
|---|---|---|---|
| Cartographer | `worker-explore` | 1 | Map of the focus area: layout, entry points, dependency direction, hot paths, existing test coverage |
| Clarity critics | `worker-review` | 2–3 | Findings against the rubric, one axis-group each |
| Debt assessor | `worker-review` | 1 | Structural debt: duplication with 3+ sites, dead code, leaky abstractions, god objects, misplaced code, package/namespace sprawl, primitive obsession, stale TODOs, config sprawl |
| Refuter | `worker-review` | 1 per batch of findings | Tries to kill each finding: is it real, is the fix behavior-preserving, does it pay for the diff |
| Test guardian | `worker-implement` | 1 | Characterization tests covering the logic about to be restructured |
| Implementers | `worker-implement` | 1 per work item | Vertical slice: source + callers + tests |
| Verifier | `worker-review` | 1 | Build, scoped tests, formatter, lint/inspection gate, public-surface diff, move-safety check |

`worker-explore` has no Bash, so the orchestrator supplies the cartographer's
file-and-line inventory (one `wc -l` you run yourself) in its prompt.
`worker-review` has Bash and cannot edit: the shape for every finder, the
refuter and the verifier. `worker-implement` is the only one with Write/Edit.
A role that seems to need a tool its worker lacks is doing something it
should not.

**Spawning rules:**

- **Never end your turn with subagents in flight.** The Stop hook then re-feeds
  the loop prompt, burning an iteration and inviting duplicate waves. Collect
  every deliverable first. If an agent stalls without its structured output,
  stop it and redo that slice inline.
- **Fan out only when it pays.** A small area, a late iteration, or a
  nearly-mined-out backlog is cheaper inline. Fan out when the area is too big
  for one context, or when roles need independent judgment (critics and
  refuters must not see each other's conclusions).
- **Never two concurrent agents editing one file.** If two work items touch one
  file, one agent owns both. A file being moved is owned by one agent for the
  whole iteration.
- **Do not trust a subagent's self-report.** Read the diff yourself before
  believing "done", "verified" or "tests pass".
- **Every finder, refuter and implementer prompt carries the EXCLUDED FINDING
  CLASSES block verbatim.** Head it with: *"These are settled. Do not report
  them, do not 'fix' them, and do not cite them as evidence for another
  finding. If one looks like a defect, that is the trap."*
- **Every spawned agent's prompt carries the git prohibition verbatim** (Hard
  rules). Subagents inherit none of your constraints.
- **Every subagent works inside the worktree, and no prompt names the main
  checkout.** The worktree is the session's working directory and every
  subagent inherits it. An agent told to use the main checkout, or that `cd`s
  back to it, has every shell command escalated to the user. A fresh worktree
  is at HEAD, so there is nothing to reach across for. Tell each agent to use
  plain relative paths and not to `cd` out. The ledger write is the
  orchestrator's alone, never a subagent's, and goes through the store's MCP
  tools (`get_doc("ralph-ledger-in-store.md")`) and not a file edit, so it
  needs no worktree/main-checkout exception at all. Keep spawned agents' shell
  commands plain: the worktree-isolation guard refuses a command whose program
  is computed at runtime.

## Phase A: Recon

Cartographer produces, for the focus area:

- The **layout as it is**: directory/package/namespace tree, file list with
  sizes, and for each file a one-line "what concept does this hold".
- The **layout a reader would expect**: what the project's dominant convention
  implies (read sibling areas), and where this area departs from it.
- Public entry points, dependency direction, which files have tests and which
  do not, and where those tests live relative to what they test.
- The three things a newcomer is likeliest to misread, and the three they are
  likeliest to fail to find.

No fixes, no style opinions: a map.

## Phase B: Findings

Critics and the debt assessor sweep the mapped area in parallel, each blind to
the others. Score the area on the **clarity rubric**, 1 (opaque) to 5
(obvious), citing evidence for every score:

1. **Naming**: do identifiers say what the thing is and does? A name that lies
   (a `get` that mutates, a `Manager` that is a bag, a boolean named for its
   implementation) is a HIGH finding on its own.
2. **Unit size and shape**: functions that do one thing; files with one reason
   to change; nesting depth; early return over pyramids; parameter counts; flag
   arguments that split a function in two.
3. **Control flow and state**: implicit ordering requirements, temporal
   coupling, hidden mutation, action at a distance, mutable shared state.
4. **Organization and placement**: can a reader predict which file holds a
   behavior, and does opening a directory say what it is for? Look for types in
   the wrong layer (domain types under UI/transport, business rules in
   controllers/routes/handlers, helpers left in the module that first needed
   them); file names that do not match their dominant type; grab-bag
   `utils`/`helpers`/`common` directories; one concept smeared across
   directories; unrelated concepts sharing a file; tests far from their code or
   named after nothing; a directory whose contents no longer match its name.
   Favor moves that make the tree self-describing over moves that satisfy a
   theory of layering.
5. **Dependency direction and coupling**: layers reaching backwards, domain
   types depending on transport or UI, circular references, feature envy, a
   module edited every time an unrelated one changes.
6. **Error and edge handling**: is the failure path as readable as the happy
   path? Swallowed exceptions, error types that leak implementation, fallbacks
   that hide bugs.
7. **Intent documentation**: is the WHY recorded where it is not derivable?
   Comments that contradict or restate the code count against the score.

Plus the debt assessor's structural findings. Every finding:

```
### Finding: [short title]
- **Axis**: naming / size / flow / organization / coupling / errors / intent / debt
- **Severity**: HIGH / MEDIUM / LOW
- **Locations**: `path/File.ext:line` (every instance)
- **Evidence**: 2–6 lines of code or a precise pattern description
- **Why it is hard to read or find**: the specific misreading or wrong guess a
  newcomer would make
- **Proposed fix**: exact change: new name, extracted signature, source and
  DESTINATION path for a move, target file
- **Behavior delta**: "none, internal only", or exactly what would change
- **Blast radius**: files touched, callers affected, tests needing updates,
  imports/namespaces rewritten
```

Hard rules for finders: nothing in the EXCLUDED FINDING CLASSES block is a
finding, however strongly the rubric points at it; real file:line citations
only; skip generated, vendored and build-output trees and anything
`git check-ignore` matches; skip lock files and migration directories; a
finding must be fixable in ≲150 lines of diff (bigger goes to `BACKLOG.md`),
and for a pure move count the import/namespace churn, not the moved file's
lines; anything crossing a public or exported boundary is a backlog item.

An organization finding lands only if it names the confusion it removes
("`TollRateCache` sits under `Controllers/` so nobody looking in the domain
layer finds it"). "Does not match a layered architecture" is a theory; reject
it.

## Phase C: Adversarial verification

Findings become work only after surviving a refuter told to kill them. Send
each finding (or small batch) to a fresh agent instructed: *"Default to
rejecting. Reject OUTRIGHT, before any other judgment, any finding in the
EXCLUDED FINDING CLASSES block above; that block is the owner's decision.
Then reject if the finding misreads the code, if the fix changes observable
behavior in any way, if the 'duplication' has fewer than three call sites, if
the abstraction serves a hypothetical future caller, if the rename touches
anything a client, wire format, database or config key can see, or if the diff
does not pay for its churn. For a proposed MOVE, also reject unless the
destination exists or follows the project's dominant convention, and unless
the move removes a stated, concrete confusion. Verify claims against the code:
a finding citing lines that do not say what it claims is rejected."*

Sort survivors by ROI (HIGH severity × small reach first) and merge per
file. Take **at most 8 work items and ~400 lines of net diff** per iteration; a
pure move counts as one item whatever its size, with no more than **3 move
items** per iteration. Everything else goes to `BACKLOG.md` with a one-line
summary. Renaming for clarity is the highest-value, lowest-risk work here;
favor it when restructuring is also available.

If nothing survives, record "no qualifying findings" for this area, mark the
area scored in `COVERAGE.md`, and go to Termination.

## Phase D: Safety net (do not skip)

Behavior preservation is proven. Before any restructuring item touches
non-trivial logic (a branch, loop, parser, calculation, money or security
path):

1. Ask the test guardian whether tests already pin that behavior. If yes, note
   which and move on; add no duplicates.
2. If no, the guardian writes **characterization tests first**: tests asserting
   what the code does today, awkward edges included. They must pass against the
   UNCHANGED code before any implementer starts. One that fails on unchanged
   code is a bug report: stop, report it to the user, and drop that item.
3. Items whose logic cannot be pinned by a test in reasonable effort go to the
   backlog.

Naming-only and comment-only items skip Phase D. **Moves do not skip it by
default**: a move is safe only once the Phase E move-safety list checks out. If
any item on that list is uncertain, treat the move as restructuring and require
a test.

## Phase E: Implement

Land inside the project's mandated workflow. If the project requires worktree
isolation, create the worktree BEFORE spawning implementers and have every one
edit inside it. The orchestrator alone writes the ledger, through the store's
MCP tools, never a file edit; never hand that write to a subagent or put it in
a subagent's prompt.

Order the work: foundational items first (shared helper introduced, type
extracted, name changed at the definition, file moved), then consumers, then
tests. Wait for each wave before starting the next.

### Move-safety list (check every one before any file changes location)

Confirm with LSP or a targeted grep, not from memory:

- **Nothing keys on the type's namespace, assembly, package or path:**
  reflection, DI assembly scanning, serializers that write type
  discriminators, `typeof(T).Name` / `class.__name__` / `Class.forName`
  lookups, config naming a class path, framework conventions mapping a file's
  location to a route, template or resource.
- **Nothing keys on the file path:** build globs, code-owner rules, coverage or
  inspection baselines, packaging manifests, embedded-resource paths, dynamic
  imports built from strings.
- **The destination is inside the same publishable unit.** A move across a
  package/assembly boundary changes what consumers reference: always a backlog
  item for a human.
- **The tests move with their subject** when the convention mirrors the source
  tree.

If any is uncertain, the move goes to the backlog with what you could not
confirm.

A move item: **move first, edit second, never in the same item.** The
implementer relocates the file and fixes only what the relocation forces
(namespace/package declaration, imports, references), so the reviewer sees a
rename. Content changes to a moved file are a later item. Use plain `mv`, never
`git mv`; rename detection handles the rest.

### Every implementer is told, in its own prompt

- Make the minimum diff that resolves the finding. Boring beats clever.
- Preserve every external contract: exported/public signatures, JSON and
  protobuf field names, database column and table names, route templates,
  environment variable names, log message shapes other systems parse, metric
  names, and any API the project marks as preserved forever.
- Follow the conventions already in this codebase: read the neighboring files
  and match them. Import no outside convention, and invent no directory layer
  to hold one file.
- Extract an abstraction only with **3+ call sites**. Two near-duplicates stay
  duplicated. No interface with one implementation, no factory for one
  product, no config for a value that never changes.
- Comment only where the WHY is not derivable from the code. Delete comments
  that restate or contradict it.
- **Self-flag behavior changes mid-edit.** If the fix would alter observable
  behavior (ordering, timing, log volume, timezone, null handling, error type,
  rounding, anything keyed on a type's name or location), stop that change,
  revert what you wrote for it, and report it. The orchestrator decides.
- Change no dependency versions or lock files, no CI configs, no new top-level
  docs. Removing an unused dependency needs LSP-verified zero references.
- The git prohibition clause, verbatim.

## Phase F: Verify

The verifier runs selected checks, and you check its claims against the tree yourself:

1. Select validation under Global Agent Instructions §Verification and the project's applicable requirements. Phase D characterization tests run when that phase created them. Run affected tests, a build, lint, type checks, or a full suite only when the changed surface needs them or a project or branch rule requires them. Do not run a broad suite blindly. Reuse matching evidence for the same inputs, environment, and exact check.
2. **Public-surface diff**: read `git diff` and confirm no line changes an
   exported/public declaration, serialized field name, route, column or env
   var. Revert any such hunk; it is a backlog item.
3. **Move check**: for every relocated file, confirm git records a rename
   (`git diff -M --stat`), and re-run the move-safety list against the landed
   state: no string in the repo still names the old path or namespace (grep
   for both). Reflection and config-driven lookups fail at runtime, so a
   passing build is not enough.
4. A selected check that fails requires a disposition: an incidental-detail test may be updated and recorded; a behavior failure is reverted and backlogged. Never weaken an assertion merely to pass.

## Phase G: Land

One landing per iteration, through the project's mandated mechanism (worktree
+ finish script, branch + PR, or a direct commit where nothing is mandated).
Stage files **by explicit name**, never `git add -A`, `.` or `-u`; a move needs
both old and new path staged for rename detection. Commit message: imperative,
one line naming the area and what got clearer, in the project's commit style.
Check with `git log -1 --stat` that what landed is what you intended. If a
commit hook fails, fix the cause and make a NEW commit: never amend, never
`--no-verify`.

## Phase H: Record

Append to the store doc `maintainability/LEDGER.md`
(`upsert_doc(..., append=True)`, per `get_doc("ralph-ledger-in-store.md")`):

```
## Iteration [N]: [YYYY-MM-DD]: focus: [area]
Rubric before → after: naming x→y, size x→y, flow x→y, organization x→y, coupling x→y, errors x→y, intent x→y

### Landed
- [title]: `path/File.ext`: [one line]
- [moved] `old/path/File.ext` → `new/path/File.ext`: [why]

### Deferred
- [title]: [why]

### Notes
- Build: [result] | Tests: [filter] [pass count] | Characterization tests added: [n]
- Moves: [n] | Move-safety checks that came back uncertain: [list]
- Behavior calls made: [any judgment call about a test or an edge case]
- Refuted findings to remember: [patterns that keep coming back as false]
```

**The ledger is prose, and the prose rules apply to it:** American spelling and
the plain-language word lists, `get_doc("plain-language.md")`. It is the
largest body of agent-written prose on the fleet and nobody reads it end to
end.

Update the store doc `maintainability/COVERAGE.md` with the area's new scores
and today's date (`edit_body`/`bulk_edit`, since this replaces existing rows
and does not add to the end), and keep a short **layout note** per area (the
convention it now follows) so later iterations reorganize toward one target.

**Correct every claim in that entry you proved stale this iteration, including
ones you did not act on.** You have read the area more closely than anyone will
before the next pass, and a future pass plans work around whatever the entry
says. Delete a superseded warning outright; a hedged warning still reads as a
warning.

Update the store doc `maintainability/BACKLOG.md`: remove what landed, add
what was deferred, again with `edit_body`. The ledger is the loop's only
durable record: while a ralph loop runs, its Stop hook intercepts session end,
so normal end-of-session record-keeping (memory writes, task lists) does not
run. Do not write those from inside the loop.

Writing the store doc directly, every iteration, is the whole record: there is
no disk copy to fall out of sync and no separate step to remember running.

## Termination

An iteration ends at its landing or at a condition below. The four early endings in `get_doc("worker-shared-rules.md", section="Ending your turn")` apply to every iteration: an early stop restarts pre-flight with the slice half done.

Wind down when any of these holds:

- Two consecutive iterations produced no qualifying findings after refutation.
- Every area in `COVERAGE.md` scores 4+ on every axis and the backlog holds
  only judgment-heavy items that need a human.
- Yield is falling (iteration 5+, each landing smaller than the last).

There is **no pause sentinel**: a question at turn end is buried under the next
re-feed. Call `AskUserQuestion` (it blocks inside the turn) to offer "wind
down" / "one more iteration" / "switch to a specific area or backlog item", and
act on the answer in the SAME turn. If the user chooses to stop, write the
completion sentinel `CODEBASE-CLEAR` inside promise tags in your final text
block, or `rm .claude/ralph-loop.local.md`.

**Never write that sentinel in promise tags anywhere else**: not in a summary,
not while explaining the mechanism, not inside backticks. The Stop hook reads
the first such tag in your last text block, and a stray one ends the loop.
Paraphrase it ("the completion sentinel") everywhere else.

## Hard rules

**Git prohibition: paste this verbatim into every spawned agent's prompt:**

> Do not run any git command at all: no `git stash`, `status`, `diff`, `log`,
> `show`, `restore`, `checkout`, `reset`, `clean`, `rm`, `mv`, `commit`,
> `rebase`, `merge`, `cherry-pick`, `push`, `branch`. Edit files; move
> files with plain `mv`. The orchestrator owns all git state. If you hit a
> build error that does not come from the file you are editing, report it in
> your deliverables; do not isolate it by stashing, since a stash takes sibling
> agents' in-flight work with it. Do not write files outside the
> working directory (no `/tmp`, no `~`, no absolute paths outside the project
> root). Scratch space is `.agents/tmp/` inside the project.

Binding on you too, plus: no deploys, no package installs, no
`git push --force`, no interactive git flags, no `--no-verify`.

**Off limits:** lock files; dependency version manifests (removing an unused
entry is allowed, version bumps are not); CI configs; migrations directories;
build output and vendored trees; anything `git check-ignore` matches; any API
the project marks as preserved. Moves that cross a package/assembly/publishable
boundary, or that change a public namespace, are backlog items for a human.

**Never:** create new top-level `.md` files, iteration reports or analysis docs
anywhere git tracks (the ledger, held as store docs under `maintainability/`,
is the only place this loop writes prose); manufacture work when an iteration
finds nothing; change observable behavior; let a rename or move escape the
codebase's own boundary; reorganize toward an architecture the project does not
already follow; report a phase as done without reading the evidence yourself.
END_RALPH_PROMPT
