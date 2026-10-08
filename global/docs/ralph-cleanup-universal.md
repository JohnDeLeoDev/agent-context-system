You are running an iterative cleanup audit of the project in the current working directory. Your job is to make the codebase smaller, simpler, more consistent, and better-documented WITHOUT changing observable behavior or breaking the build. Each iteration must produce NEW, concrete improvements — never repeat work already recorded in the cleanup log (see Pre-flight step 2 for its location).

## Pre-flight (do this once per iteration before scanning)

1. **Verify the working tree is clean.** Run `git status --porcelain`. If output is non-empty, stop and tell the user: "Working tree is dirty — commit or stash before running cleanup." Do not proceed. Do not run `git stash` yourself.
   - **Repeated-halt escalation:** if you've already reported the dirty tree this session and the file list is unchanged from the prior report, do not just re-halt. Immediately use `AskUserQuestion` to offer commit-pending / cancel-loop / skip-check. The Stop hook would otherwise re-feed the loop forever, and `AskUserQuestion` gets the answer inside the turn instead of burying it. (There is no pause sentinel; see [[global/docs/ralph-loop-notes|ralph-loop-notes.md]].)
2. **Read the cleanup state.** It lives in the store as two project docs:
   `cleanup/CLEANUP_LOG.md` (what past iterations did) and
   `cleanup/CLEANUP_BACKLOG.md` (deferred items). Read and write them per
   `get_doc("ralph-ledger-in-store.md")`: check the `toc` before pulling a
   section, since the log can run large. Create a doc with a one-line header
   on first use if it does not exist yet.
   For the rest of this document, "the cleanup log" / "the backlog" refers to
   these two store docs.
3. **Detect the stack.** Inspect the project root for:
   - `package.json` → Node/JS/TS — build/test commands from `scripts`
   - `Cargo.toml` → Rust — `cargo build`, `cargo test`, `cargo clippy`
   - `go.mod` → Go — `go build ./...`, `go test ./...`, `go vet ./...`
   - `pyproject.toml` / `setup.py` / `requirements.txt` → Python — detect from project (pytest, mypy, ruff)
   - `build.gradle*` / `settings.gradle*` → JVM/Kotlin/Android — `./gradlew assembleDebug`/`build`, `./gradlew test`, `./gradlew lint`
   - `Package.swift` / `*.xcodeproj` / `*.xcworkspace` → Swift/iOS — `xcodebuild` / `swift build` / `swift test`
   - `Gemfile` → Ruby — `bundle exec rake`, `bundle exec rspec`
   - `composer.json` → PHP
   - `mix.exs` → Elixir
   - `Makefile` → look for `build`, `test`, `lint` targets
   Record what you found. If no test command is detectable, ask the user once: "No test command detected — proceed without test verification?" Wait for an explicit yes before continuing.
4. **Check tool availability.** Try the language server (LSP tools: `goToDefinition`, `findReferences`, `workspaceSymbol`, `documentSymbol`, etc.) on one symbol. Note whether it is responsive — fall back to grep/find for anything it can't answer. Do not block on it being available.
5. **Read history.** The cleanup log (what's been done) and the backlog (deferred) — at whichever location step 2 selected. The CLAUDE.md (project + universal) is already in your context.

## Phase 1: Scan (parallel, MULTIPLE passes per area)

**One pass over an area is never enough — every iteration runs at least two passes over each scan category, and a category having been covered before (in a prior iteration OR an earlier pass this iteration) does not exclude it from being analyzed again.** Coverage is never "done": re-scan areas you have already looked at, because each fresh pass reaches ground the earlier ones skimmed (different packages/layers, deeper coupling, edge cases, newly-landed code).

Run it as waves: **Pass 1** — spawn subagents in parallel, each owning one scan category, sweeping the codebase broadly. **Pass 2 (and 3 if yield warrants)** — spawn a fresh wave over the SAME categories, each subagent given the prior passes' findings and told to dig for genuinely NEW/different issues in territory the earlier pass did not reach (e.g. pass 1 sweeps `views/`, pass 2 pushes into `model/`, `db/`, `helpers/`; then a re-sweep of the highest-yield categories). Do not "rotate away" from or skip a category because it was recently scanned — prior coverage only changes WHERE within the area later passes look, never WHETHER the area is re-examined. Keep spawning passes over an area until a pass turns up nothing genuinely new.

**Scan inline unless fan-out genuinely pays.** Subagents spawned from this loop run
**detached** in this harness — they do not block the orchestrator, so your turn ends
while they are still working, and the Stop hook then re-feeds the loop on
every Stop event (it deliberately does not exit on `stop_hook_active`). That burns a
full prompt's worth of tokens per cycle and tempts duplicate scan waves. Two hard rules:

- **Never end your turn with scan agents in flight.** If you spawned them, collect
  every deliverable before the turn ends. If a scanner stalls — its transcript shows
  only intermediate reasoning and never produces the `### Finding:` blocks — stop it
  and redo that category inline rather than waiting through more re-feeds.
- **Default to inline** when the surface is small or near-exhausted: a late iteration,
  a small codebase, or categories the backlog shows are nearly mined out. Fan out only
  when the codebase is genuinely too large for one context to sweep. A stalled async
  fan-out costs a full-prompt re-feed per cycle and delivers nothing.

When you do fan out, use the store's worker definitions, never a harness catch-all
(`general-purpose`, `claude`) and never the harness's own (`Explore`, `Plan`). A
catch-all is not a registered agent type outside Claude Code, so naming one is how a
fan-out silently fails to spawn in pi or opencode — and it carries the harness's full
tool grant rather than the narrow one the role needs.

Scanners read and judge; they do not edit and do not need a shell, so they are
`worker-explore`. A scanner that must RUN something (a build, a test, a linter) is
`worker-review` instead — worker-explore has no Bash at all, by design.

The worker definitions already carry the worker tier, so there is nothing to pass:
`model:` is set in the definition and resolved per harness at spawn time. Never write
a versioned model id here.

Each scanner should prefer LSP for symbol questions and Grep only when LSP can't answer. Do not grep the whole repo when LSP can answer in tens of tokens.

Categories:

- **DRY Hunter**: Duplicated logic, near-identical functions across modules, copy-pasted constants, repeated error-handling shapes (try/catch with identical bodies, identical logging patterns), validation reimplemented per screen/route/handler, parallel `switch`/`when`/`match` statements that should be a lookup table, similar collection chains (`filter`/`map`/`groupBy` / list comprehensions). Cross-package/cross-module duplication is highest priority. DRY extractions require **3+ real call sites** — two near-duplicates stay duplicated.
- **Dead Weight**: Unreachable code, unused private/internal members, commented-out blocks, stale TODO/FIXME with no owner or date, unused dependencies in the package manifest, unused imports the formatter missed, orphan source files not referenced anywhere, unused static assets (images, strings, color/dimension resources). For "unused" claims, verify with LSP `findReferences` — never delete based on grep alone, reflection / dynamic dispatch / framework-loaded code can mask real uses.
- **Convention Drift**: First identify the project's conventions by reading the dominant patterns in helpers/utils/lib/shared modules. Then find places that diverge. Hardcoded values where a constants/tokens/config file exists, inconsistent logger usage (raw `print`/`println`/`console.log` next to a structured logger), mixed error handling (sometimes returning, sometimes throwing, sometimes both in the same flow), inconsistent null/undefined/optional handling, magic numbers/strings that should be named constants, mixed naming conventions in one file.
- **Structure & Placement**: Code in the wrong package/directory (domain types in UI dirs, UI helpers in model dirs, business logic in routes/controllers), oversized files (>500 lines) violating SRP, side effects in modules billed as pure, composables/components with inline business logic that belongs in a service/manager/store, types declared next to a single consumer that are actually used by multiple consumers.
- **Performance Hygiene**: Language-specific — apply only what fits the stack. Sync I/O on hot paths or in UI/render code, blocking calls in async contexts, unnecessary intermediate materializations (`.toList()` / `Array.from` / `list(...)` before further filtering), string concatenation in loops (use a builder), repeated DB queries that should be batched into one query or a transaction, unstable lambdas in hot composables/renders, large lists rendered without keys, `Sequence`/`Flow`/`Iterable`/`Stream` consumed multiple times.
- **Test Hygiene**: Duplicated test setup that should be a fixture/builder, copy-pasted assertion blocks, tests that share mutable static state, brittle string-match assertions on log output, tests with no Arrange/Act/Assert separation, redundant tests covering the same path, tests that exercise mocks instead of behavior.
- **Documentation Drift**: Doc claims that contradict current code — file paths that no longer exist, function names that have been renamed, hardcoded counts ("we have 17 managers") that are now wrong, fields that have moved. Verify each claim with LSP `workspaceSymbol` or `findReferences`. Per-feature catalog entries (under `.agents/docs/features/` when that convention exists) that describe behavior no longer in the code. Public functions/classes/types without doc comments (KDoc / JSDoc / docstrings / Rustdoc). **Do not flag missing root-level docs** (CONTRIBUTING.md, ARCHITECTURE.md, etc.) — out of scope for this loop.

Each scanner must produce findings as:

```
### Finding: [Short Title]
- **Category**: [DRY / Dead / Drift / Structure / Perf / Test / Docs]
- **Severity**: HIGH / MEDIUM / LOW
- **Locations**: `path/to/File.ext:Line` (list every instance for DRY)
- **Evidence**: 2-6 line code excerpt or pattern description
- **Proposed Fix**: Specific change — extracted helper signature, target location, what callers update
- **Blast Radius**: Files touched, callers affected, test files needing updates
- **Risk**: Why this could break something (or "low — internal only")
```

Hard rules for scanners:
- Every finding must cite real files and line numbers. No hypotheticals.
- Skip generated / vendored / build output: `node_modules/`, `vendor/`, `target/`, `build/`, `dist/`, `out/`, `.next/`, `.nuxt/`, `__pycache__/`, `.venv/`, `venv/`, `.gradle/`, `.idea/`, `.vscode/`, `Pods/`, `DerivedData/`, `coverage/`, `.parcel-cache/`, anything matching `.gitignore`.
- Skip lock files entirely: `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `Pipfile.lock`, `poetry.lock`, `Gemfile.lock`, `composer.lock`, `*.lock`.
- Skip migrations directories (`migrations/`, `db/migrations/`, `prisma/migrations/`) — sequence-dependent, never edit historical migrations.
- A finding must be actionable in `<= ~150 lines of diff`. Bigger items go to CLEANUP_BACKLOG.md, not this iteration.
- Public/exported API signatures (anything exported from a package boundary, marked `pub`, `public`, exported in `index.ts`, etc.) are OFF LIMITS unless the finding is "internal-only rename" with proof no external caller references it.

## Phase 2: Triage

**Inline-vs-subagent decision:** if all scanner reports collectively fit comfortably in your current context (rough threshold: under ~15k tokens of findings, no overwhelming cross-file overlap), do triage inline as the orchestrator. Skip the subagent — you already have the CLEANUP_LOG and CLEANUP_BACKLOG loaded, so re-injecting them into a fresh subagent is pure overhead. Spawn a triage subagent only when scanner output is large enough that an inline pass would crowd out subsequent agent prompts. For a 5-iteration run this typically saves 5 Sonnet invocations.

Whether inline or via subagent, triage must:

1. Drop duplicates of already-completed work.
2. Merge findings that touch the same file into a single work item (file-contention rule).
3. Reject findings that violate the rules (forbidden paths, oversized scope, speculative, public-API changes).
4. Sort by ROI: HIGH severity + low blast radius first.
5. Select 5–10 findings for THIS iteration. Push the rest to CLEANUP_BACKLOG.md with a one-line summary each.
6. Build a dependency graph: foundational changes (shared helpers, base types, design tokens, manager interfaces) → consumer changes (call sites, screens, routes).

Output: an ordered work plan with explicit phases.

If triage returns zero qualifying findings, see Termination below.

## Phase 3: Implement (dependency-aware, phased)

**Vertical-slice rule**: every agent owns the source change and its callers and its tests. Never split a refactor across agents.

**File-contention rule**: never run two concurrent agents that edit the same source file. If two findings touch the same file, one agent handles both.

Pick the STORE worker definition that matches the finding shape — never a harness
catch-all (`general-purpose`, `claude`) or the harness's own (`Explore`, `Plan`),
which are not registered agent types outside Claude Code:
- Anything edit-heavy → `worker-implement` (Read/Edit/Write/Bash + LSP).
- Research-only / broad reads → `worker-explore` (read-only, LSP, **no shell**).
- Anything that must RUN a build, test or linter → `worker-review` (has Bash, cannot
  edit, so it reports rather than fixes).
- Architecture-spanning design questions (no edits) → gather with `worker-explore`
  and do the synthesis inline. There is deliberately no planner worker: a design
  judgment made in a context you cannot see is one you cannot check.

The tier is already set in each definition and resolved per harness at spawn time, so
there is no `model:` to pass. The global *Model tiering* rule makes the role→tier
split unconditional: everything below the orchestrator is a worker, with no
self-judgment escalation for "this one involves real reasoning." Only an explicit
instruction from the user moves a specific task up a tier.

The same detached-subagent hazard from Phase 1 applies here: do not end your turn with implementation agents still running.

Phases:

- **Phase 3a — Foundational**: Spawn agents in parallel for findings that introduce new shared helpers, new base types, new design tokens, new interfaces, or DAO/base-class changes. Wait for all to complete.
- **Phase 3b — Consumers**: Spawn agents in parallel for call-site changes. Each agent receives Phase 3a's diffs as context.
- **Phase 3c — Tests**: If a Phase 3b agent didn't already update its tests (it should have), spawn targeted test-update agents now.

### MANDATORY language in every spawned-agent prompt

Every Agent invocation in Phases 1–4 MUST include this clause verbatim near the top of its constraints:

> **DO not run any git commands at all — no `git stash`, no `git status`, no `git diff`, no `git log`, no `git show`, no `git restore`, no `git checkout`, no `git reset`, no `git clean`. Just edit files. The orchestrator owns all git state. If you observe a build error that doesn't seem to come from the file you're editing, report it in your deliverables — do not try to "isolate" or "verify" it by stashing. `git stash` has historically swallowed sibling agents' in-flight work and silently dropped it on pop. Do not write files outside the working directory (no `/tmp`, no `~`, no absolute paths outside the project root). For scratch space, use `.agents/tmp/` inside the working directory.**

This is not optional. Subagents do not inherit the orchestrator's prohibitions automatically; the prohibition must travel inside each prompt.

Each implementation agent must:
- Make the minimum diff that resolves the finding.
- Preserve external contracts (public/exported API signatures, JSON wire formats, DB column names, route templates, environment variable names).
- **Self-flag behavior changes mid-implementation.** If during editing you discover the proposed fix would alter observable behavior (timezone shift, log-volume change, animation timing, schema affinity, ordering semantics, etc.), STOP that change, report the discovery in your deliverables, and let the orchestrator decide whether to revert or defer to backlog. The loop's promise is "smaller, simpler, better-documented" — not "functionally different." Silent acceptance of a behavior change violates the loop's contract.
- Use the project's existing conventions (constants/tokens files, error-handling helpers, logger) when they exist. Detect these by reading the codebase, not by guessing.
- Add no comments unless the WHY is genuinely non-obvious.
- Not introduce abstractions for hypothetical future needs — extract only when there are **3+ real call sites**.
- Do not modify dependency versions (`package.json` versions, `Cargo.toml` versions, `gradle/libs.versions.toml`, `requirements.txt` versions, etc.). Removing genuinely unused dependencies IS allowed — verify zero references first.
- Do not modify lock files.
- Do not modify CI/CD configs (`.github/workflows/`, `.gitlab-ci.yml`, etc.) — out of scope.

### Documentation-writing rules (this iteration's doc work)

When a Docs-category finding is being implemented:
- **Update existing docs**: rewrite the stale section. Match the doc's existing style and depth.
- **Auto-write missing docs ALLOWED in only two places**:
  1. **Per-feature catalog entries** at `.agents/docs/features/<feature>.md` — only when the project already uses this convention (i.e., `.agents/docs/features/` exists). Match the existing template (read a sibling doc). Content policy: intent, entry points, non-obvious gotchas. Structural enumerations (file lists, signatures) are forbidden — those rot. Do not create `.agents/docs/features/` if it doesn't already exist; just flag the missing-feature-docs in backlog.
  2. **Inline doc comments on public APIs** — KDoc / JSDoc / Rustdoc / docstrings on exported functions, classes, types, and public fields. Keep them one to three lines. Document WHY and contract (inputs, outputs, side effects, thrown errors), not WHAT — well-named identifiers convey what.
- **Never create** new top-level `.md` files (README.md, CONTRIBUTING.md, ARCHITECTURE.md, CHANGELOG.md, etc.), new files in the repo root, or new files outside `.claude/` and source directories.

## Phase 4: Verify

Spawn one validation agent (`worker-review`). It runs selected checks and cannot fix what it finds.

Select validation under Global Agent Instructions §Verification and the project's applicable requirements:

1. Docs: inspect links or rendering. Config: parse, render, or load it through its consumer.
2. Localized code: run affected existing tests and add a focused regression only when warranted.
3. Public APIs or formats, migrations, auth or security, concurrency or state integrity, and consequential releases: run applicable integration, compatibility, and failure-path checks.
4. Run a build, lint, type check, or full suite only when effects cannot be adequately bounded or a project or branch rule requires it. Reuse matching evidence for the same inputs, environment, and exact check.
5. If a selected check fails, determine whether it exposes a real behavior problem before changing code or a test. Never weaken an assertion merely to pass. Document the disposition in CLEANUP_LOG.md.

## Phase 5: Record

Append to the cleanup log (from Pre-flight step 2), per
`get_doc("ralph-ledger-in-store.md")` (`upsert_doc(..., append=True)`):

```
## Iteration [N] — [YYYY-MM-DD]
Focus categories: [list]

### Completed
- [Finding title] — `path/to/File.ext` — [one-line description of fix]
- ...

### Deferred to Backlog
- [Finding title] — reason

### Notes
- Build: clean / details
- Tests run: [filter expression(s)] — [pass count]
- Test-behavior calls made: [list any]
```

Update CLEANUP_BACKLOG.md with `edit_body`/`bulk_edit`: remove items addressed this iteration; add newly deferred items.

## Phase 6: Commit / land (one landing per iteration — MANDATORY end-of-round step)

1. `git status` — confirm only intended files changed. If unrelated modifications appear (other agents, user work in flight), STOP and ask.
2. `git add` the specific files this iteration changed (list them by name; never `git add -A`, `git add .`, or `git add -u`).
3. `git commit -m "Cleanup audit — Iteration <N> (<focus categories>)"` — one commit per iteration; do not squash.
4. Verify with `git log -1 --stat` that the commit landed and contents match expectations.

**Prohibited git commands (never run, no exceptions: there may be unsaved work or other agents operating concurrently). This list binds both the orchestrator and every spawned subagent: paste the subagent clause from Phase 3 verbatim into each subagent's prompt, and do not merge the two lists. Subagents may run no git at all; the orchestrator alone may commit, per Phase 6.**

- `git stash` / `git stash push` / `git stash pop` / `git stash drop` / `git stash apply` / `git stash clear` / `git stash list` — loses sibling agents' in-flight work. Forbidden, no "verification" or "isolation" exception. (The harness blocks this at the hook level anyway.) If you suspect an error is pre-existing, read `git log` / `git show HEAD:path` or just Read the file.
- `git reset --hard` / `git reset --merge` / `git reset --keep` — destroys working-tree state.
- `git reset --soft` / `git reset --mixed` to a prior commit — rewrites shared history.
- `git restore` / `git restore --staged` — reverts files; another agent's edits would vanish.
- `git checkout <path>` / `git checkout -- <path>` — same problem as `restore`.
- `git checkout <branch>` / `git switch <branch>` — abandons in-progress work.
- `git clean` (any flags) — deletes untracked files, including other agents' new files.
- `git rm` / `git mv` — use plain `rm` / `mv` and let the user (or this iteration's intentional staging) handle it.
- `git revert` of any non-cleanup commit.
- `git rebase` (any form), `git merge`, `git cherry-pick`.
- `git commit --amend` — never amend a prior commit, cleanup or otherwise.
- `git push` (any form, especially `--force`).
- `git branch -D` / `git branch -d`.
- `--no-verify` on commit (don't bypass hooks).

If a hook fails, fix the underlying issue and create a NEW commit. Do not amend, do not skip hooks.

If you need to abandon mid-iteration changes, document the reason in CLEANUP_LOG.md and ask the user to revert manually — never self-revert.

## Termination

An iteration ends at its landing or at a stop condition below. The four early endings in `get_doc("worker-shared-rules.md", section="Ending your turn")` apply to every iteration: an early stop restarts pre-flight with the slice half done.

**Stop the loop** when any of:
- This iteration's triage produced zero qualifying findings and the prior iteration also produced zero (two consecutive zero-finding iterations).
- CLEANUP_BACKLOG.md is empty and a fresh scan produces zero new findings.
- The user explicitly cancels (or you reach an obvious natural stopping point — diminishing yield, all remaining backlog items are judgment-heavy and need human review).

**Recommended exit mechanism:**

1. Use `AskUserQuestion` to offer "wind down" / "one more iteration" / "tackle a specific backlog item", with a short summary of why you are winding down. It gets the answer inside the turn. There is no pause sentinel: `get_doc("ralph-loop-notes.md")`.
2. If the user picks "wind down", emit the terminal-phrase sentinel (`<promise>DONE</promise>`) in your next text block. The Stop hook scans every `<promise>...</promise>` tag in the last text block.

**Fallback kill switches** — if the sentinel ever fails to terminate (transcript-buffering races, hook bugs), you can:
- Run `touch .claude/ralph-cleanup.cancel` via Bash — the hook deletes both that sentinel file and the state file on the next Stop event.
- Edit `.claude/ralph-cleanup.local.md` and change `active: true` to `active: false`.
- Delete `.claude/ralph-cleanup.local.md` outright via `rm`.

Otherwise, continue. There is no hard iteration cap — but if you find yourself on iteration 5+ with monotonically-decreasing yield, proactively offer the wind-down option rather than waiting for iteration 10+.

## Loop interaction with session-end record-keeping

While a ralph loop is active, the loop's Stop hook intercepts every session-end event and re-feeds the loop, suppressing any normal session-end record-keeping (memory updates) until the loop ends. The durable record of what the loop is doing is the cleanup log: the store doc `cleanup/CLEANUP_LOG.md`. Keep per-iteration narrative there.

Don't try to write project records from inside the loop. (There is no task tracker. Durable state lives only in the agent-context store, through its MCP tools.) When the loop is canceled (by emitting the completion-promise sentinel, by `touch .claude/ralph-cleanup.cancel`, by setting `active: false` in `.claude/ralph-cleanup.local.md`, or by deleting that file), normal session-end resumes and any memory updates happen then.

## Global Rules

- **Never write the literal completion-promise tag in your output unless the loop should genuinely terminate.** The Stop hook scans all `<promise>...</promise>` occurrences in your last text block and terminates if any one of them equals `DONE` (case-sensitive). So even inline-code mentions inside backticks will trigger termination if the contents say `DONE`. Paraphrase instead ("the completion-promise tag", "the DONE sentinel", "the loop's terminal phrase") when you need to discuss the mechanism. A loop that exits because of self-described documentation is a self-inflicted loss of state.
- Read CLEANUP_LOG.md before scanning only to avoid re-proposing already-completed fixes — not to skip whole categories. Every area is re-analyzed with multiple passes every iteration regardless of when it was last covered; "already covered" is never a reason to skip an area, only a reason for later passes to target the ground earlier passes missed.
- **Off-limits files** (no edits, ever): lock files (`*.lock`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `Pipfile.lock`, `poetry.lock`, `Gemfile.lock`, `composer.lock`); dependency version manifests for VERSION CHANGES (`package.json` deps, `Cargo.toml` deps, `gradle/libs.versions.toml`, `requirements.txt` versions, `pyproject.toml` deps) — removing unused entries is allowed, version bumps are not; CI configs (`.github/workflows/`, `.gitlab-ci.yml`, `.circleci/`, `azure-pipelines.yml`); build output dirs (`build/`, `target/`, `dist/`, `out/`, `node_modules/`, `.venv/`, etc.); migrations directories.
- **No new `.md` outside `.claude/`.** CLEANUP_LOG.md and CLEANUP_BACKLOG.md are loop state, held as store docs under `cleanup/` (Pre-flight step 2), not on disk. Per-feature docs (when allowed) live at `.agents/docs/features/`. Do not create iteration reports, audit summaries, or analysis docs anywhere tracked by git.
- No deploys. No package installs. No `git push`. No interactive git flags.
- **Never run destructive or working-tree-altering git commands** (see Phase 6 prohibited list). Other agents and user edits may be in flight. When in doubt, `git status` and ask.
- If an iteration produces zero qualifying findings, record "No actionable findings this iteration" and exit cleanly — do not manufacture work.
- DRY extractions require 3+ real call sites; two near-duplicates stay duplicated.
- Public/exported API signatures are not changed in this loop.
- No files written outside the working directory. Scratch space goes in `.agents/tmp/` inside the project.
- Behavior is preserved. The loop's promise to the user is "smaller, simpler, better-documented" — not "functionally different." If a finding's fix would change observable behavior in any way, defer it to backlog with a note for the user to review manually.
