# Writing less code

The rule is in Global Agent Instructions, "Writing code": write the least code that solves the problem. This doc holds what that section points to. Adapted from dietrichgebert/ponytail (MIT); decision policy in `get_doc("agent-context.md")`.

## The ladder

Understand the problem first: read the task, read the code it touches, trace the real flow end to end. Then stop at the first step that holds.

1. **It does not need to exist.** A need nobody has yet is skipped, and the reply says so in one line.
2. **The codebase already has it.** A helper, type or pattern a few files away is reused. Search before writing.
3. **The standard library has it.**
4. **The platform has it.** A date input over a picker library, CSS over script, a database constraint over application code.
5. **An installed dependency has it.** Check the project's manifest before proposing a library, say what the existing one covers and what gap is left, and propose the smallest addition for that gap.
6. **One line does it.**
7. **Otherwise, the minimum that works.**

When two steps both hold, take the earlier one. When two options are the same size, take the one that is correct on edge cases: less code never means the weaker algorithm.

## Rules

- No abstraction nobody asked for: no interface with one implementation, no factory for one product, no setting for a value that never changes.
- No scaffolding for later.
- Delete before adding. Plain before clever. Few files.
- The shortest working diff wins, once the problem is understood. A small change in the wrong place is a second bug.
- A bug is fixed at its root. A report names a symptom: find every caller of the function about to change, and fix the shared function once.
- A request that looks larger than its need gets the small version, with one line naming what was left out and when to add it. Build the full version when user says to, with no second argument.

## What is never cut

- Understanding the problem.
- Input validation at a trust boundary.
- Error handling that prevents data loss.
- Security measures.
- Accessibility basics.
- Calibration that real hardware needs: a clock drifts and a sensor reads off, so the adjustment stays.
- Anything user asked for.
- One runnable check for logic with a branch, a loop, a parser, or a money or security path. Global Agent Instructions, Verification, sets how much more.

## Marking a shortcut

A deliberate simplification with a known limit (a global lock, a quadratic scan, a naive heuristic) carries a comment that names the limit and what to move to:

    # ponytail: global lock; per-account locks if throughput matters

A marker with no upgrade path is the kind that is never revisited, so each one names its trigger.

## Review tags

A reviewer who finds code that skipped a step reports it as an optional finding with one tag:

- `delete:` dead code, unused flexibility, a feature nobody asked for. Replacement: nothing.
- `stdlib:` hand-written code the standard library ships. Name the function.
- `native:` a dependency or code doing what the platform does. Name the feature.
- `reuse:` an equivalent helper already in the repo. Name the path.
- `dry:` the same logic in two or more places. Name each copy and the one piece to extract. This one is a defect, not optional: `get_doc("store-operations.md")`.
- `yagni:` an abstraction with one implementation, a setting nobody sets, a layer with one caller.
- `shrink:` the same logic in fewer lines. Show the shorter form.

## Audit

A whole-repo pass for code that need not exist. It lists findings and changes nothing.

1. Look for: dependencies the standard library or platform already covers, interfaces with one implementation, factories with one product, wrappers that only delegate, dead flags and settings, hand-written standard-library functions, and helpers that duplicate one already in the repo.
2. Before a `delete:` finding, search the whole tree for the symbol, including tests, fixtures and string or dynamic references.
3. Report one line per finding, numbered and ranked largest cut first: `<N>. <tag> <what to cut>. <replacement>. [path]`. End with the lines and dependencies that could go, or say the repo is already lean.
4. Correctness, security and performance are out of scope: send those to a normal review.
5. Keep the findings in a project doc, `cleanup/less-code-audit.md`, with the commit audited and which cuts have landed, so the next pass starts from it.

## Shortcut ledger

List every `ponytail:` comment in the repo, grouped by file: what was simplified, the limit named, the trigger to revisit. Tag a marker that names no trigger `no-trigger`. End with the count of markers and how many have no trigger.
