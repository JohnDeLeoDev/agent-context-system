# The agent quality system

Four instruments, each answering a question nothing else here answers. Read this before
changing any of them; the reasoning is what stops them being "simplified" into
uselessness.

---

## How it works, end to end

This section is the operator's view.

### Five layers, and what each one is for

**1. Knowledge — the store.** `~/.agent-context` is a git repo served by an MCP server
and synced to four remotes across seven machines. `get_session_context` opens every
session with the global instructions, the memory index, open audit observations and the
machine's identity. The index carries descriptions only; bodies are fetched on demand.
That trade is what keeps the bootstrap affordable, and it is also the source of the
system's worst failure mode: a description that reads as complete gets acted on without
the body.

**2. Prevention: hooks.** Written in the store and run in place from
`~/.agent-context/global/hooks`; `list_entities(kind="hook")` is the live list. `PreToolUse` hooks refuse: destructive git, deploys, disk writes
to store entities, shell file reads, redundant reads, secrets, unresolvable paths.
`SessionStart` materializes the projections and launches probes. The subtle one:
**a Stop hook's `systemMessage` reaches the terminal, not the model.** Advisory hooks
therefore live on `UserPromptSubmit`, looking backward at the turn that just finished and
injecting `additionalContext`, which is the only advisory channel proven to arrive.

**2b. Detection of stale knowledge — `freshness-diff.py`.** An entity's
`updated_at` is tied to nothing, so the always tier goes stale unnoticed: it describes the
fastest-moving things and is re-read least. This reports an always-loaded entity edited
before the last change to a file its own body names. Narrow on purpose — always tier only,
resolvable paths only, with a skip-list for build artifacts, `node_modules`, and files a
loop rewrites: the general "check every pointer in the corpus" version is mostly noise
and would be switched off. `--all` widens it to lazy
entities for a deliberate sweep; it reports and never edits.

**3. Detection — invariants and probes.** `invariant-check.py` is the registry: each rule
is one lesson this fleet already paid for, asserted at every site in its class. It runs
detached at SessionStart and `preflight-core-health` surfaces a bad verdict next session.
`observation-coverage.py` asks the prior question — which recurring lessons have no
mechanism at all.

**4. Measurement — the eval.** `eval-run.py` spawns a real session per case against a
throwaway git fixture and scores it deterministically (which files were touched, what
they contain, whether a command succeeds, which tools were called — read from the run's
own transcript) plus an LLM judge for what an assertion cannot express.
`eval-selftest.py` gates it: a case that passes against its own untouched fixture cannot
detect anything, and `--go` refuses to spend until every case can fail.

**5. Learning — audit observations.** A defect gets filed with evidence and a machine.
Recurrences are counted. That count is the trigger: a one-off is a note, a recurrence is
a demand for a mechanism.

### The loop that actually produces the quality

    defect → observation (with evidence) → recurrence → mechanism (hook or invariant)
           → a test that proves the mechanism can fail → the obs number cited in the code
           → observation-coverage confirms the loop closed

Every step is load-bearing. Without the citation, `observation-coverage` cannot see the
mechanism and a later reader cannot tell what a check guards. Without the failing test, a
mechanism is indistinguishable from dead code.

**The defect class this whole apparatus exists for is the half-applied invariant:** a
lesson learned at one site and never applied to its siblings. It looks handled — there is
an observation, a resolution note, a paragraph of commentary — while the same hole sits
open two files over. It turns up inside the tools built to catch it.
Hence the rule: when you fix something that could exist at more than one site, the fix is
not the edit, it is the check that makes it true everywhere.

### What user does, and why each one needs him

Most of this runs without him. Five things do not:

1. **Say when the work is wrong, in the moment — and say when it is the second time.**
   This is the only input the loop cannot generate for itself. "You keep making that
   mistake" is worth more than a bug report, because recurrence is what converts a note
   into a mechanism.
2. **Read the audit digest** in the session-start inbox. It is the queue of things that
   need a human, already triaged worst-and-oldest first.
3. **Ask for a behavior measurement when he wants one.** Agent output is probabilistic,
   so a sampled eval is not a per-change rule. `eval-run` and `scaffold-regression` run
   only when he asks.
4. **Decide the things an agent must not decide alone**: spending on an eval run, a deploy
   or daemon restart, and minting a git-write consent token. All three are gated, and the
   gates are the point.
5. **Refuse a noisy check.** A check that false-positives is fixed or deleted the same
   turn. The moment one is tolerated, the whole registry becomes something to skim.

### The two commands

    python3 ~/.agent-context/global/scripts/verify-quality-system.py        # free, ~60s, run after any change
    python3 ~/.agent-context/global/scripts/eval-run.py --go --save-baseline   # ~$3.40, only when user asks

The first proves the mechanism is intact. The second samples agent behavior, which is
probabilistic, so it is not a gate on any change.

### Mechanisms in force

One line per item, each a script in `~/.agent-context/global/scripts/`:

- `eval-run.py`: `--bare` refuses before spending anything when `ANTHROPIC_API_KEY` is
  unset. A reply matching `UNUSABLE` (not logged in, invalid key, authentication error,
  low balance) becomes an ERROR and is excluded from saved rates. With no saved
  baseline, a run with an errored case exits 2 (nothing measured); a failing case
  exits 1.
- `eval-run.py`: `harness_print()` is checked at read time. A mark scored by a
  different harness renders `~` and is excluded from the rate, and the regression
  compare is skipped outright against a stale baseline.
- `eval-run.py`: `--history` flags any case between 0% and 100% as FLAKY.
- `eval-run.py`: the saved baseline is machine-local; a later run exits non-zero only
  on a regression against it.
- `hook-test-run.py`: sorts hook invocations into deny, warn, or silent, and adds
  `expect_files` for a hook whose product is a written record, plus a `pre` field to run
  a shell command in the case's temp dir for git-dependent hooks.
- `invariant-check.py`: `enforcing-hook-has-test` selects hooks by what they emit
  (`permissionDecision`, `systemMessage`, `additionalContext`), not by name.
- `hook-test-cases.py`: what arms each stateful hook:

  | hook | armed by |
  |---|---|
  | `block-redundant-read` | a per-session read ledger; the 1,200-line ceiling is payload-shaped |
  | `require-store-bootstrap` | a transcript that exists and holds no bootstrap call, with no `transcript_path` it reports "unknown, not no" and allows |
  | `require-working-lsp` | a per-session flag file written by `lsp-failure-tripwire`, not an lsp verdict json |

  `LSP_DOWN_STATE_DIR` overrides `require-working-lsp`'s state dir only so its refusal
  path is reachable from a test; the default is unchanged in production.
- `eval-selftest.py` / `eval-cases.py`: `judge_led` is the plain escape for a case
  whose correct outcome is inaction, where no mechanical check can tell doing nothing
  apart from doing it right.
- `global/hooks/require-worktree-edit-bash.py`: blocks a raw disk write to a store entity from Bash
  (redirects, `tee`, `sed -i`, `cp`/`mv`, and an interpreter heredoc writing a store
  path).
- `invariant-check.py`: invariants `stat-portability` (both `stat -f %m` and `stat -c`
  spellings must be present at a site that reads mtime), `stop-hook-reaches-the-model`
  (a Stop hook advising only via `systemMessage` is flagged unless it also blocks, or
  says in its header the audience is the human), `hook-wired-to-its-declared-event`
  (checks the actual registered event, not the sidecar or description).

### One sample cannot evaluate a scaffold change

Budget for `--repeat 3` when a change needs a decision, or let comparisons accumulate
across later runs and read the trend. Do not read a one-run flip as a fix, in either
direction.

### What this system does not do

- **A single eval run is a sample, not a measurement.** At `--repeat 1` a 19 and a 17 are
  not distinguishable. Use `--repeat 3` before believing any individual case moved.
- **It cannot measure an intervention that only affects the next turn.** `eval-run` drives
  `claude -p`, which has one turn per case; a `UserPromptSubmit` correction has nowhere to
  land. Multi-turn cases exist for this and are the only place such a change is visible.
- **Subagent behavior is partly invisible.** `verification-claim-check` stands down
  whenever a subagent was spawned, so a turn that delegates and then overclaims is
  uncaught.
- **The lazy memory tier is still a live hazard.** A memory that must be read before an
  irreversible action cannot be one the agent has to know to ask for. The push-to-deploy
  gate removes the consequence for one class; it does not make the tier safe.
- **The eval measures the scaffold, not this model.** A better number after an instruction
  change is evidence about the instruction. It says nothing about a day's work.

## The diagnosis

The complaint was not about capability. It was about **variance** — the same rule
honored one turn and dropped the next. Three causes:

1. **Instruction density.** ~2,300 words of always-loaded rules plus 24 memory
   descriptions, well past the ~150-200 instructions frontier models follow reliably.
   Past that threshold rules are dropped *broadly*, not just the newest.
2. **Conflicting sources.** The harness note, the global instruction and a per-turn hook
   all argued about tool discipline. Claude mis-resolves implicit conflicts ~23.6% of
   the time, silently.
3. **Half-applied invariants.** A lesson learned at one site and never applied to its
   siblings.

## The four instruments

| Instrument | Question it answers | Trigger |
|---|---|---|
| `invariant-check.py` | is every lesson we paid for applied at every site? | SessionStart probe, pre-commit, ralph loop |
| `eval-run.py` + `eval-cases.py` | did that scaffold change make the work better? | manual, before/after a change |
| `scaffold-regression.py` | did it make the work more expensive or thrashier? | manual with `--at`, or `--health` |
| `failure-trace.py` | why did that specific turn go wrong? | PostToolUse, always on |

**They are complementary and none substitutes for another.** invariant-check is static
and catches classes; eval-run is behavioral and catches regressions in outcome;
scaffold-regression catches the non-functional drift that CI is blind to; failure-trace
is the forensic record for when something still goes wrong.

## Why non-functional metrics count as quality

An empirical study of coding-agent scaffolds tracked 10-18 releases a week and found
**effectiveness flat while resource consumption nearly doubled** — complexity grew,
quality did not. Two findings drive `scaffold-regression.py`:

- **Context Management is the most regression-prone layer.** It is also the layer this
  store edits most.
- **Failed tasks burn ~2.7x the tokens of successful ones**, because a stuck agent loops
  on edit-test. That makes token weight a *leading indicator of quality*, not just a cost
  line — which is why a token regression is worth investigating even when nothing looks
  broken.

## Rules for keeping these honest

- **Every eval case is grounded in a failure that happened.** An invented case measures
  an imagined agent and gives false confidence when it passes.
- **A check that produces a false positive is fixed or deleted, never tolerated.** A
  noisy check is the one people learn to skim.
- **Report inconclusive as inconclusive.** An LLM judge that returns no verdict is not a
  failure and not a pass. Counting it either way corrupts the number.
- **A permanently-green check nobody runs is worse than no check** — it answers
  "healthy" for something nobody has looked at. `--verify` exists for exactly this, on
  the allowlists.
