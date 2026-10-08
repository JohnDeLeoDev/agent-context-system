# Plain-language glossary — operator-facing text

> **Nothing enforces this file.** `plain-language-check` reads its word lists from
> `plain-language.md` and never opens this glossary. Every
> pin below is honored by convention, by a reader who opened it. A term
> that must be enforced belongs in [[global/docs/plain-language|plain-language.md]], where the hook will see it.

Every string a human reads — workflow titles, job and step names, dispatch-form
descriptions, run summaries, console output, error messages — is written for that
reader. This file is the single source of that vocabulary so it does not drift.

**Applies to:** workflow files, deploy/build scripts, and operator-facing runtime
messages. Not to code comments' technical accuracy, identifiers, or `Debug`-level
diagnostics — those are developer-facing and stay technical.

**Extending it:** add shared vocabulary to this doc. A project's structural tokens and
pinned strings go in that project's pins doc (the scoped `get_doc` call is listed below), which says why wherever it
differs from this doc. Two glossaries for the same words is the same failure as two dialects.

# PART 1 — The rules (universal)

1. **One word per concept, and that word means nothing else.** This rule matters most.
   **Plain is not the same as distinct. Names must differ by PURPOSE, not just be readable.**
2. **Descriptors, not narrative. Simple words, but adult ones.**
   What gets cut is SENTENCE LENGTH, not vocabulary:

   | Wrong (narrative) | Wrong (childish) | Right |
   |---|---|---|
   | `Needs a look, but did not block anything: 19` | | `Warning: 19` |
   | `Results: is this deploy safe to leave live?` | | `Results: Should this deployment remain active?` |
   | `Switch off automatic deploys before undoing this one` | | `Disable automatic deploys` |
   | `Raise an alarm if automatic deploys could not be switched off` | | `Alert if automatic deploys are still enabled` |
   | `Work out what this deploy is and where its log goes` | | `Identify this deploy` |
   | | `Test the app the way a phone app would use it` | `Mobile app checks` |
   | | `Let other deploys use this server again` | `Free the server` |

   Where the ordinary professional word is ALSO the plain one — disable, alert, identify,
   confirm, record, inactive — use it. Do not trade a precise ordinary word for a longer
   folksy explanation of it. That is what the first pass did wrong in the other direction.
3. **A failure message still says what happened, what it means and what to do next — on
   SEPARATE LINES.** Rule 2 does not delete the guidance; it moves it off the label. The
   first line is a short descriptor; the advice is an indented continuation under it:

   ```
   ✗   Data load did not finish within 300s
           Still warming up or stuck. Check the server logs.
   ```

   not

   ```
   ✗   The app's data did not finish loading within 300s. It may still be warming up or
       stuck; check the server logs before treating this deploy as healthy.
   ```

## The trap that costs the most

**Renaming a string that something matches on silently disarms it.** Before renaming
anything, grep the repo's tests, scripts and tooling for assertions on step names, job
names, task names and log lines. Move both sides in the SAME commit.

**A guard that reads well and a guard that is checkable are not the same thing, and
improving the prose of a refusal is exactly how you delete the refusal.**

# PART 2 — Shared vocabulary

## The core verb

**`deploy`** = put a version of the app onto a server. **It never means anything else,
and nothing else means this.**

**`release`** = the overall event ("the Tuesday release"). NEVER a verb for the act of
deploying.

Banned as synonyms for deploy: ship, publish, push out, roll out, install, promote*.

\* `promote` survives ONLY in the narrow sense of "send the same packaged build on to
the next environment without rebuilding". Prefer spelling that out.

**Projects without servers:** if a project has no act of "putting a version onto a
server" — an app distributed to a store or a test track, say — do not force this word
onto it. Pick the one word that fits THAT act, define it in the project section below,
and hold it just as strictly. Note also that in such a project "release" likely means
something stronger and more specific (a shipped version, with a version code, that
cannot be un-shipped); that stronger meaning wins, and the difference gets written down.

## Environments

| Write | Short id (code, paths, URLs) | What it is |
|---|---|---|
| **Development** | `dev` | Developers try things. No real users. |
| **Testing** | `test` | Final checks before Production. |
| **Production** | `prod` | Real people, real traffic. |

BANNED: "the sandbox", "the developer sandbox", "the test site", "the live site",
"which site", "this site". Say "environment", or name it.

**Short ids are acceptable in job names** (`Deploy to dev`), because a job name sits
under a title that already gives context and is read in a cramped column. Prose —
descriptions, summaries, messages — uses the full names.

Where a project has build variants, signing configurations or distribution tracks
rather than server environments, name those by the same principles rather than
inventing three environments to match.

## The build

| Instead of | Write |
|---|---|
| artifact | packaged build |
| stage an artifact | package this build |
| prune artifacts | delete old packaged builds |
| sha / commit / ref | version of the code |
| build once, promote the same artifact | build it once, then deploy that same package everywhere |

## The checks

| Instead of | Write |
|---|---|
| gate | check |
| CI | the automated checks |
| preflight | check the server is ready |
| format check | check the code is formatted consistently |
| inspection ratchet | check code quality |
| unit suite | the automated tests |
| coverage gate | check test coverage |
| smoke suite | second, independent health checks |
| client journey suite | mobile app checks |
| flake / flake ledger | unreliable tests (they pass and fail unpredictably) |
| verify endpoints | check the app answers requests |
| seeded defect drill | practice drill (deliberately break something so a check has to catch it) |

## Deploy mechanics

| Instead of | Write |
|---|---|
| blue / green / colour | the two copies of the app on the server |
| standby / idle / retained colour | the spare copy (not serving anyone) |
| serving colour | the copy people are using |
| flip / cutover / traffic switch | switch people over to the new copy |
| canary | first-look check on the new copy |
| warmup | warm up the new copy before anyone uses it |
| drain | let requests that are already running finish |
| orchestrator | the deploy program |
| edge / router / YARP | the traffic router |
| box deploy lock / lease | reserve the server (and: free the server) |
| continuity prober | check whether the app ever stopped answering |
| watch window | watch for problems |
| auto-revert | automatically undo the deploy |
| rollback | undo a deploy / go back to an earlier version |
| offline schema migration | database change that requires taking the app offline |
| DPAPI | encrypted so only this server can read it |

## The expected-responses check ("golden")

| Instead of | Write |
|---|---|
| golden baseline | the saved record of what the app is expected to return |
| golden parity gate | comparison against the saved record of expected responses |
| recapture golden | replace the saved record of expected responses |
| epoch | which data feeds have refreshed |
| rule engine | the correctness rules |
| ABSTAINED | not checked (NOT the same as passed) |
| stability probe | check the answer is steady, not just changed |

## Verdict words — plain sentences, not adjectives

| Instead of | Write |
|---|---|
| CLEAN | no problems seen |
| DEGRADED | measurably worse than before, though not a failure |
| BREACHED | worse than the agreed limit |
| UNKNOWN | could not tell |
| enforced | compared, and it matched |
| RECAPTURED | replaced - this deploy was not compared against anything |
| not armed / DISARMED / no-op | **inactive** |
| observe-only | watching only; nothing is changed automatically |

Do not confuse this with the ALL-CAPS pinned token `STILL ARMED`, which is a refusal
keyword matched by code and never reworded.

# PART 3 — Workflow title pattern (universal)

Numbered by lifecycle stage, so the run list itself teaches the order. Grouping
prefixes for everything outside the main sequence:

```
1. <the checks>
2. <deploy to the early environments>
3. <deploy to Production>
Recover: <undoing / emergency shapes>
Shared - <called by other workflows, not run directly>
Trial:  <evaluates something and deliberately acts on nothing>
```

A workflow nobody should run by hand must make that obvious — in the title where it
fits, otherwise in its header comment.

# PART 4 — Project sections
