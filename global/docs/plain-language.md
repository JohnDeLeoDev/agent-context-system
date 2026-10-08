# Plain language

user's standing feedback: narratives, descriptors and names are too
verbose and too full of jargon. He wants terse.

**Terse, not dumbed down.** He is well read and technical. Use the precise technical
term (*idempotent*, *race*, *invariant*, *semantics*, *back-pressure*)
because the precise term is usually the short one. A page of plain words explaining
what one accurate term would have said is the failure this rule exists to stop.

What gets cut is padding: corporate filler, hedges, intensifiers, throat-clearing,
and metaphor standing in for a fact. Never the vocabulary.

This doc holds the lists and examples; the short rule lives in Global Agent
Instructions. The hook `plain-language-check` enforces the word lists on writes and
commit messages.

## user's rules

Given as direct corrections. Do not soften them in a paraphrase.

**No em dashes.** Use a period, a comma, or a
colon. This is deterministic, so the hook blocks on it.

**Banned words** are listed under Word lists, below. For the c-word for "standard form",
write "standard", "single source", or "the reference copy".

**Write imperative, not observational.** His example:

| Wrong | Right |
|---|---|
| A file exports one component. | Export one component per file. |

The observational form reads as self-important. Say what to do, or say what
the thing does. Do not narrate the state of the world.

**Never compare to an alternative, and never justify.** State what things do. Cut the
contrast clause and keep the fact.

**No counts of things a person can count.** File counts, module counts, "three
places", line tallies. State only what
is not clear from looking at the repo.

**No self-importance.** Do not write as if holding authority.

**No narratives, no fluff.** State the information and stop.

### What a repo doc is for

A repo doc helps a reader navigate the repo and pick up its conventions. It is a quick
resource.

Write repo docs as a map, not a manual.

## Two rules, deliberately separate

**Prose gets shorter. Names do not.** Applying the prose rule to identifiers is the
mistake to avoid. A name that describes what the function does helps; cutting its
length makes code harder to read.

### Rule 1: prose

Applies to code comments, commit and PR messages, store docs and memory bodies, and
messages to user.

- One idea per sentence. If a sentence needs two clauses to hold one idea, it needs
  two sentences or fewer words.
- Say the thing. Do not set it up first, and do not restate it after.
- Prefer the plain verb where the fancy one adds nothing: *use*, not *utilize*.
  *Show*, not *surface*. But keep the verb that is exact: *orchestrate* is right when
  something really does sequence other things.
- No metaphor where a fact exists. "The build fails" beats "the build posture degrades".
- A caveat the reader needs is not padding. Cut the flourish, keep the warning.
- Sentences over ~30 words in a comment or commit body get flagged. Split them.
- A comment that restates the line below it is deleted, not shortened.

### Rule 2: naming

Every word in a name must narrow the meaning. Keep the ones that do; cut the ones
that do not.

Words that usually narrow nothing:

- Role suffixes: `Manager`, `Coordinator`, `Handler`, `Helper`, `Utils`, `Service`
  when it wraps nothing, `Base`, `Impl`, `Wrapper`
- Type suffixes: `Info`, `Data`, `Object`, `Item`, `Entity` on a type that already
  names its thing
- Verb prefixes that add no action: `do`, `perform`, `handle`, `process`, `execute`

Keep a role suffix when the pattern is genuinely that pattern: a real coordinator
that coordinates, a real base class with subclasses.

## Bad to good

Names:

| Too much | Right |
|---|---|
| `AudioSessionConfigurationCoordinator` | `AudioSession` |
| `handleUserInitiatedRefreshRequest()` | `refresh()` |
| `NetworkResponseDataObject` | `NetworkResponse` |
| `performDatabaseMigrationExecution()` | `migrateDatabase()` |
| `UserProfileInformationManager` | `UserProfile` |

Not too much. Leave these alone:

- `refreshRemoteInventoryCache()`: every word narrows it
- `decodeLegacyV1Payload()`: the version matters
- `retryAfterRateLimitBackoff()`: names the exact condition

Prose:

| Too much | Right |
|---|---|
| "Leverage the existing cache to facilitate faster lookups" | "Read from the cache" |
| "This provides a robust, comprehensive solution for handling errors" | "Retries twice, then throws" |
| "It's worth noting that the underlying semantics differ here" | "Note: this compares by value" |
| "Surface the failure to the user" | "Show the error" |
| "We essentially just need to ensure the state is consistent" | "Normalize the state first" |

### Rule 3: American spelling

American English everywhere: comments, commit messages, docs, memory, chat. *Color*,
not *colour*. This is deterministic, so the hook blocks on it.

The three patterns that catch almost everything:

- `-ize` / `-ization`, never `-ise` / `-isation`: normalize, initialize, serialize,
  organize, recognize, analyze, authorization, synchronization
- `-or`, never `-our`: color, behavior, favor, honor, neighbor
- `-er`, never `-re`: center, meter, fiber, theater

Plus the irregulars: canceled (one L), modeling, labeled, traveled, defense, license
(noun and verb), practice (noun and verb), gray, catalog, dialog, aluminum, math,
toward, program.

Real exceptions, and the only ones: an identifier or API genuinely spelled the
British way (some libraries do ship `initialise`), a quoted error string, a proper
noun, a URL. Put it in backticks and the hook skips it. Code spans and fenced blocks
are never scanned.

## Word lists

**Hard, blocked.** No legitimate use in these projects.

leverage, utilize, robust, comprehensive, seamless, seamlessly, delve, posture,
paradigm, holistic, synergy, best-in-class, cutting-edge, off-box, barrel,
adjudicate, adjudication, break-glass, idiomatic, substantive,
"it's worth noting",
"it is worth noting", "it's important to note", "it is important to note",
"at the end of the day", "worth knowing about"

**Dramatic framing and advance notice, blocked.**

"changed the shape of", "the shape of this work", "would have shipped silently",
"shipped silently", "the kind that would have", "changed the picture", "the real story",
a line opening "Checkpoint.", "worth your call", "one thing worth", "worth flagging",
"worth mentioning", "when I get there", "unless you'd rather", "I'll flag", a line opening
"Heads-up:", "more on that later", "before I get to"

Say a finding when you have it, as the fact. Never announce it ahead of time, and never
rate how much it matters. These block in writes and in every final message.

Plus the em dash character itself, which is banned outright.

**Padding, narration and offers, blocked.** They block in writes, in
the main loop's final message and in a worker's final message. The next-turn advisory names any that appeared earlier in a turn.

| Group | Blocked | Write instead |
|---|---|---|
| Padding and hedges | essentially, fundamentally, basically, actually, arguably, simply, straightforward, powerful, elegant, very, quite, facilitate, "in order to", "a number of", "in terms of", "with respect to", "the fact that", "that being said", "needless to say", "the reason is" | cut; "to"; the number |
| Intensifiers and candor | genuine(ly), truly, honest(ly), frankly, candidly, plainly, entirely, precisely, cleanly, silently, fully (not "fully qualified"), exactly (not before a count), "to be clear", "for what it's worth", "almost certainly", "most likely" | cut; "likely"; "with no error" |
| Judgment nouns | "the real bug/cause/fix/gap", "a real gap/bug/problem" | "the bug", "a gap" |
| Comparison | "rather than", "instead of", "as opposed to", "not because", "X, not Y.", "not X, but Y" | state X |
| Stock phrases | "by design", "in practice", "on purpose", "by construction", "for the record", "net effect", "in principle", "going forward", "bottom line", "in short", "in a nutshell", "the gist", "for good measure", "it turns out", "tl;dr", notably, importantly, crucially, interestingly, ultimately | cut |
| Metaphor | load-bearing, sanity check, hygiene, bespoke, cadence, blast radius, orthogonal, footgun, sharp edge, tractable, moving parts, low-hanging fruit, rabbit hole, smoking gun, silver bullet, belt and braces, "bites you", surface (verb) | "required", "check", "custom", "every 5 min", "affects", "unrelated", "show" |
| worth | any use but "worth of" | the verb: "check the lockfile" |
| Narration | "Good," "Great," "Perfect." opening a line, "Let me", "Let's", "Now let me", "Time to", "given the effort budget", "final report now" | call the tool; report the result |
| Advance notice | "up front", "before I start", "one note", "two things to know", "you should know", "keep in mind", "bear in mind", "Flagging" opening a line | say it |
| Offers and compliance recaps | "say the word", "if you want", "happy to", "feel free to", "say so and I'll", "as you asked", "as requested", "per your request", unasked | state the fact; a real choice goes through the active harness's structured question tool |
| Report tags | "Confidence: high", "Severity: low", "read, not measured" | name only what you did not verify |

Still allowed: "exactly once", "exactly 3", "fully qualified", "the real path", "the API
surface", "silent" as an adjective, "the shape of the payload", "Good:" as an example
label, "a day's worth of", "Let's Encrypt", and a one-line status between tool calls
in a long turn: what you found and what you do next ("Suite green; landing now.").
The harness asks for such an update after a silent stretch. A quotation of a blocked phrase goes in
backticks. Invariant `agent-read-text-plain` keeps hook messages, the global instruction
and the worker definitions clean; `agent-prose-scan.py` re-measures agent transcripts.

Technical terms are not on either list. *semantics*, *idempotent*,
*invariant*, *race*, *back-pressure*, *guardrail*, *orchestrate* all say something
exact in fewer characters than the explanation would take. Using one is the goal, not
the offense.

## Known residue

**Do not hand-fix the ledgers.** Loop ledgers are append-only history, written by the
loops themselves through MCP (`get_doc("ralph-ledger-in-store.md")`); old entries are
never rewritten.

## Tone of the global instruction

Global Agent Instructions loads at every session start and sets the tone for everything
written after it. Keep it in plain language: a rule added there in a dense voice pulls
output back.

## Response shape

Terseness sets the length. This sets the arrangement. Adapted from the
`i-have-adhd` skill (ayghri/i-have-adhd, MIT), keeping the rules that fill a real gap
and dropping the ones that fight user's standing corrections. The short form lives in
Global Agent Instructions.

Three facts drive it. Anything not on screen is gone, so never ask him to keep
something in mind. Knowing the answer is not doing it, so when he has something to
do, the first line must be doable now. Starting is the expensive step, so make the first
action small and exact.

### The rules

**1. The first line is the answer, the result, or the action.** A question gets the
answer. A finished task gets its result. When user has something to do, the first line
is that action: a command, a `path:line`, or the snippet. Never context, a plan, or
what you are about to do. Prose comes after, if at all.

| Wrong | Right |
|---|---|
| "Your auth flow has a few moving pieces..." | ``Run `npm install jsonwebtoken`, then edit `src/auth.ts:42`.`` |

**2. Number multi-step work.** More than one step gets a numbered list, one bounded
action per step. No step contains two "and then"s. Use the fewest steps that still
work: a short path finished beats a complete path abandoned. When the harness has a
todo tool, that tool is the list. Do not also narrate the plan as prose.

**3. End with one concrete next step, or end.** If something is open, name ONE thing
doable in under two minutes. "Next: run `npm test` and paste the first failing line."
This replaces the closers that are banned, it does not reinstate them. Nothing open
means the message ends at the result.

**4. Suppress tangents.** Finish the first issue. Offer the second as its own
question, once, at the end, through the active harness's structured question tool. A question that comes up
mid-work is not a tangent: answer it yourself and fold the result in.

**5. Errors are cause and fix.** Symptom, `file:line`, cause, fix. Never "uh oh",
"oh no", or "there seems to be a problem".

> Test fails at `auth.spec.ts:42`: expected 200, got 401. Cause: missing auth
> header. Fix: add `Authorization: Bearer ${token}` to the request.

**6. Cap a displayed list at five.** Group related items, rank the most relevant
first, show five. This shapes presentation only. It must never limit analysis, search,
tool results, candidate generation or what you retain. Keep the rest and show it when
he asks or when it becomes next. When completeness is the point, such as an audit
roster or a full inventory, show everything.

**7. Structure when the content has shape.** user asked for structured output. Pick the form by content:

| Content | Form |
|---|---|
| A single fact, a yes/no, a one-line status | One sentence |
| Items compared across attributes, status per item | Table |
| Trend, distribution, proportion | Chart with an available visualization tool; use a static plot for export |
| Flow, dependency, architecture | Mermaid or diagram |
| Enumerable facts, steps | Bullets, or a numbered list for steps |
| Choices for user | The active harness's structured question tool; keep the turn open for the reply |

Structure is not padding. A table or chart of real information does not count as a
recap. It still earns no extra prose around it.

### Deliberately not adopted

The upstream skill also says to restate state every turn and to make wins visible.
Both are recap, which user corrected: do not report what he
just watched. Its time-estimate rule points at a human executor; here the agent
executes, so an estimate is only useful when he has to wait or decide.

### Pre-send check

Delete before sending:

1. The first sentence, if it announces what you are about to do.
2. The last sentence, if it asks "anything else?" or recaps what just happened.
3. Any "by the way" sidebar.
4. Any hedging adverb carrying no information. Keep a caveat that changes what he
   should trust or do, real uncertainty included: deleting that one manufactures
   confidence.
5. Any idiom. Replace it with the literal action.

Then check: reading only the first line and the last line, does he know what to do
next and what just happened?

### When terseness yields

The length rule has an off switch and never had one written down, which is why a
direct request to explain something fought the hook. Terseness yields when:

1. He asks to explain or walk through. The body runs as long as the topic needs. Add
   headers so he can skim back. Still no preamble, still no closer.
2. A destructive action is next. Confirm first. Safety outranks brevity.
3. Three turns of "still broken". Stop iterating on code. Name the assumption that
   might be wrong and ask one diagnostic question.
4. The request is genuinely ambiguous. One short question beats guessing.
5. The rule would delete the answer. "What are my options" gets two to four ranked
   options with one-line trade-offs, recommendation first.
6. The content is an error, failing test output, or a security warning. Show it in
   full. Matches Concise output style rule 6.

In every case the length changes and the shape does not.
