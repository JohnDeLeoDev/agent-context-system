# Token usage tracking

Answers "what is actually spending my tokens" — by conversation, project, model,
effort, main-vs-subagent, day, and tool.

## Why it has to exist at all

Claude Code already records everything needed. Every assistant message in
`~/.claude/projects/**/*.jsonl` carries a full `usage` block — `input_tokens`,
`cache_creation_input_tokens` (split 5m/1h), `cache_read_input_tokens`,
`output_tokens` — next to `sessionId`, `cwd`, `gitBranch`, `model`, `effort`,
`isSidechain` and `requestId`. Subagents get their own
`subagents/agent-*.jsonl`, so main-vs-worker splits cleanly.

And then it throws it away. Transcripts are swept at `cleanupPeriodDays`
(default 30). `~/.claude/metrics/costs.jsonl` and `~/.claude/stats-cache.json`
are written by older code paths, carry no token counts, and were already months
stale when this was built — they are not substitutes. **Anything not rolled up
before the sweep is unrecoverable.** That, not curiosity, is why collection runs
on hooks rather than on demand.

## Shape

    transcripts ──▶ token-usage-collect.py ──▶ SQLite (machine-local, ~65 MB/mo)
                          │                       └─ per request, per tool result
                          └──────────────────▶ machines/<uuid>/token-usage/YYYY-MM.json
                                                  └─ daily aggregates, git-synced

    get_usage_report(report="tokens") ◀── fleet rollups (default)  +  local SQLite (drill-down)

Same discipline as `machines/<uuid>/usage.json`: one writer per file, so the
fleet merge never conflicts, and the evidence window is the fleet's rather than
one laptop's.

**The split is not arbitrary.** Rollups are small, git-friendly and fleet-wide,
but only carry what was pre-aggregated. The SQLite holds one row per request —
too big for git, and the only thing that can answer a session-level question or
compute amortization. A report never silently mixes them: `source` says which
one answered, and anything from the local DB is labeled this-machine-only.

## Pieces

| Piece | Where |
|---|---|
| Collector (writes) | store script `token-usage-collect` → `~/.agent-context/global/scripts/token-usage-collect.py` |
| Hook (all three events) | store hook `token-usage-collect` → `~/.agent-context/global/hooks/token-usage-collect.py` |
| Hook wiring | `home-settings-sync` MANAGED + `global-settings-json` |
| Reporter (reads) | `server/src/agent_context/tokens.py`, MCP tool `get_usage_report(report="tokens")` |

The collector is stdlib-only and standalone on purpose: it runs from hooks under
a bare `python3`, with no access to the server's venv. That is also why it
re-derives `state_dir()` instead of importing `paths.py`.

### Three triggers, and why there is no timer

**Collection never requires a session to end.** The collector reads each
transcript incrementally by byte offset and has no concept of a session being
finished — it consumes whatever has been appended since its last run, refusing
only a partial trailing line (picked up whole next pass). Open sessions are
collected exactly like closed ones.

| Event | Interval | Job |
|---|---|---|
| `Stop` | `--min-interval 30` | Fires every assistant turn — this is what makes the numbers near-real-time, including inside a session that runs for days |
| `SessionEnd` | none | Definitive pass: fold in whatever the last `Stop` did not cover |
| `SessionStart` | none | Catches a session that never reached `SessionEnd` or a final `Stop` — closed terminal, crash, `kill -9` |

There is no timer: `Stop` covers live sessions continuously and
`SessionStart` covers anything a crash interrupted, so no window is left
for a timer to catch.

**Staleness is bounded at ~30s while Claude Code is running**, and a crashed
session's tail lands at the next launch. Data is only ever lost if a machine
sits powered off long enough for its transcripts to age past 30 days — and
anything collected before that is already durable.

The `--min-interval` guard matters: `Stop` fires on every turn, so without it a
long session would fork a full transcript walk hundreds of times to
insert a handful of rows each. With it, all but one turn per 30s costs a single
`stat()`. The stamp file is touched on every *completed* run, insertions or not
— keying off the database's mtime would mean an idle scan never advanced it and
the guard would never trip precisely when there was nothing to do.

## Two traps — both silent if you get them wrong

1. **Resumed sessions replay history into a new transcript.** Count
   them and every total inflates. Dedupe is on `requestId`, globally and
   permanently — hence a PRIMARY KEY, not a per-run set.

2. **A `tool_use` and its `tool_result` can land in different incremental
   chunks**, and the `tool_use` may sit on a request dedupe discards. Name
   resolution therefore cannot be an in-memory per-file dict. `tool_calls` is a
   persisted table joined against `tool_results` at report time. An in-memory
   dict leaves most tool results "(unmatched)".

## Reading a report

```
get_usage_report(report="tokens", group_by="project" | "model" | "effort" | "day" |
                          "sidechain" | "tool" | "session",
                 since_days=30, project=None, limit=25, per_machine=False)
```

`group_by="session"` and the amortized tool block come from the local DB and
cover this machine only. Check `days_with_data` / `enough_evidence` before
acting on a zero.

**`cost_usd` is notional.** It prices tokens at Anthropic first-party rates
(cache write 1.25x at 5m / 2x at 1h, cache read 0.1x). Under a subscription no
such invoice exists — the relative ranking is the point, and that holds either
way. An unpriced new model falls back to Opus-tier and the collector names it in
its output.

## Amortization: a ranking, never a decomposition

A tool result is not paid for once. It is paid on the cache-write that admits
it, and again as a cache-read on **every subsequent request in the session that
carries it**. So a 40k-token Read on turn three is enormous and the same Read
late in a session is cheap — and no per-call token count can tell them apart.

`tools_by_amortized_context` therefore computes a marginal cost:
`est_tokens x input_price x (1.25 + 0.1 x tail)`, where `tail` is the number of
requests behind that result in the transcript. Summed over all tools it comes to
roughly 2.3x the billed total, because those tails overlap each other and the
system prompt — each request's cache-read is claimed in full by every result
riding inside it. **Rank by `context_share_pct`; `attributed_usd` is normalized
onto real spend so it is never read as billed.**

The tail is derived at report time from `files.req_count - req_ordinal`, not
stored — it keeps growing after the row is written, and freezing it would bake
in whatever the session length happened to be.

## Gotchas

- `Bash` and `Agent` are relabeled (`Bash:grep`, `Agent:general-purpose`) or
  the rows are too coarse to act on. A leading `cd <dir> &&` is stripped first —
  labeling on it makes `Bash:cd` the top tool and says nothing.
- Worktree paths are folded back to their parent repo, or every feature branch
  reads as its own project.
- The collector takes a single-writer lock (stale after 15 min, then stolen). It
  is an efficiency guard only — WAL plus `INSERT OR IGNORE` already makes a
  concurrent run harmless.
- The rollup folds any (day, project, tool) row under 3 calls into `(other)`:
  most of the rows and a small share of the tokens. Totals stay exact; only resolution is lost.
- Rewriting a rollup is skipped when byte-identical (modulo `generated_at`), so
  an idle machine never dirties the store tree.
- A cold first run on a new machine walks every transcript and takes seconds;
  warm incremental runs are sub-second. The hook detaches so neither is ever felt.
