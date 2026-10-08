Every ralph loop's ledger (LEDGER.md, BACKLOG.md, COVERAGE.md, a worklist, a
log, and the rest) is a project doc in the agent-context store, never a file
under `.agents/` or `.claude/` on disk. This doc is the shared read/write
procedure; a loop's own prompt names its own doc paths and points here for the
mechanics, without repeating them.

## Reading a ledger

A ledger doc can run large (some have passed 500 KB), so never pull the whole
body on a routine read.

1. Call `get_doc(path, project=...)` with no `section`. A body over 16 KB
   returns only its `toc` (heading, level, byte size) in place of the full
   body: that is enough to see the doc's shape and where the most recent
   entries are.
2. Call `get_doc(path, project=..., section="<heading>")` for the one section
   the step needs (the most recent iteration, or one area's COVERAGE entry).
   A miss returns the same `toc` so the next call can use the right heading.
3. If the doc does not exist yet, `get_doc` errors; treat that as "no ledger
   yet" and create it on first write with `upsert_doc`.

## Writing a new iteration

**Append-only history** (LEDGER.md, CLEANUP_LOG.md, a log doc): a new
iteration is always the newest entry, so it always belongs at the end of the
doc.

1. Check the `toc` first (Reading, step 1) so the append lands after the last
   existing heading, never mid-document. An append that lands out of order
   loses the loop's true iteration count.
2. Append with `upsert_doc(path, project=..., body="<new iteration block>",
   append=True)`. `append=True` adds the given body to the end of an existing
   doc on a fresh line and takes no other field, so nothing already recorded
   can be reordered by an append.
3. If the doc does not exist yet, create it first with a plain
   `upsert_doc(path, project=..., body="<header>")` (no `append`), then append
   every iteration after that.

**A field that gets replaced, not appended** (BACKLOG.md, COVERAGE.md, a
worklist row's status, a live checklist):

- A small, well-isolated change: `edit_body(kind="doc", key=path,
  project=..., old_string=..., new_string=...)` against the current unique
  text.
- A change touching many rows or several docs at once: `bulk_edit`, one
  `edits` entry per doc.
- Read the section about to be replaced first (Reading) so `old_string`
  matches what is there, not a stale copy from earlier in the conversation.

## Why the store and not disk

A ledger on disk needs a second, synced copy to be durable, and the two copies
drift. Writing to the store leaves one copy and no sync step.
