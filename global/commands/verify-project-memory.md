---
uuid: "f544f56d-e1e6-5e28-900f-158e04f0f4e4"
type: "command"
name: "verify-project-memory"
description: "Verify memories against code and stamp verified_at; fix proven stale claims, queue the rest. Project by hook, global or workspace by hand."
allowed_tools: ["Bash", "Read", "Grep", "Glob", "mcp__agent-context__get_session_context", "mcp__agent-context__resolve_project", "mcp__agent-context__list_entities", "mcp__agent-context__get_memory", "mcp__agent-context__edit_body", "mcp__agent-context__bulk_edit", "mcp__agent-context__check_integrity", "mcp__agent-context__add_audit_observation"]
disable_model_invocation: true
argument_hint: "[global | workspace <name>]"
---
# Verify project memory bodies against the code (background, conservative)

You run unattended in a **detached background session** launched by the
`project-memory-verify-autorun` SessionStart hook when a session opened inside a registered
project. Your job: check that this project's memory **bodies** still match the current
codebase, correct the ones that are **high-confidence stale**, and queue everything else for
a human. There is no human in the loop, and the user's foreground session is running
independently: do not assume anything about it.

You were launched with `PROJECT_MEMORY_VERIFY_RUN=1` (recursion guard; do not unset) and, in
`PROJECT_MEMORY_VERIFY_STATE`, the path to this project's run-state file (for §4).

> **Guiding rule: when in doubt, queue; never edit.** You are editing memory bodies
> autonomously; a wrong "correction" corrupts real knowledge. Only auto-edit a claim you can
> check against the code, that is unambiguously wrong, and whose fix is unambiguous.

## 0. Resolve + scope
- `get_session_context(cwd)` to resolve the project. If **no project resolves**, go straight to
  §4 and stop (nothing to do).
- Work only on the resolved project's memories.
- **Argument `global` or `workspace <name>`.** user invokes this form; no hook launches it (policy). Work on that scope's memories, not a project's, and skip the project resolution above. Check each claim against what this machine can read: the store tree (`~/.agent-context`), the chezmoi source (`~/.local/share/chezmoi`), installed files and configs, and the other memories and docs in the same scope. A claim about another host cannot be checked here: skip it, and do not queue it. Two entities that state opposite facts get one observation naming both (`scope="universal"`). The language-server rule in §2 applies only to code symbols; path, config and cross-entity checks need no server. In §4, log `project=global` or `project=ws:<name>` and skip the state file.
- **Budget: verify at most 8 memories this run, or 25 in the `global` or `workspace` form**, which user runs by hand in the foreground. This runs in the background on qualifying
  sessions; keep it cheap and bounded. Prefer memories whose bodies make concrete, checkable
  claims and the least-recently-updated ones.

## 1. Verify each selected body against current code
`list_entities("memory", project, limit=0)`, then for each selected memory `get_memory(slug, project)` and read the
body. Extract its checkable claims: file paths, symbol/class/function names, endpoints, config
keys/flags, DB objects, `Fixed/Landed/Deployed <sha>` status, stated invariants. Verify against
the current code, read-only, using Code Discovery in order: **LSP** (does the symbol exist? where?)
then **Grep/Glob/Read** for strings/paths/flags. Prefer the cheapest check that settles it.
Classify each memory:
- **CURRENT**: its claims still hold → no edit.
- Stamp every memory you classify as CURRENT, and every one you correct, with today's date: `bulk_edit(edits=[{"kind": "memory", "key": slug, "project": project, "fields": {"verified_at": "YYYY-MM-DD"}}])`, one call for the whole run. A memory you queue or could not check gets no stamp. Start each run with the memories that have no `verified_at`, then the oldest stamps; `check_integrity(summary=True)` lists `verification_overdue` and `missing_sources`. A memory's `sources` name the files to check first, and its `hosts` say which machine can check it.
- **STALE, HIGH-CONFIDENCE**: a named file/symbol/flag/path is definitively gone, or the code now
  does the opposite, and the correct value is unambiguous from the code → correct the body with
  `edit_body("memory", slug, old, new, project)` (unique-replace only the wrong span; minimal, surgical patch).
- **STALE, UNCERTAIN / judgment**: likely off but the fix isn't unambiguous, or it's design-intent
  not checkable from code → `add_audit_observation(scope="project", project=<project>, ...)`; do not edit.

## 3. Systemic drift
If many memories are stale because of one big migration (mass rename, moved/renamed files, an
API cutover), do not hand-fix dozens. Queue one `add_audit_observation` summarizing the drift +
pattern (with a few example slugs) and stop editing; a human should drive a sweep like that.

## 4. Finalize (always, even on zero changes)
Stamp the run, release the lock, and append one log line:

```bash
ST="${PROJECT_MEMORY_VERIFY_STATE:-}"; NOW=$(date +%s)
if [ -n "$ST" ] && command -v jq >/dev/null 2>&1 && [ -f "$ST" ]; then
  t="$ST.tmp.$$"
  jq --argjson n "$NOW" '.last_run=$n | .running_since=null | .running_host=null' "$ST" > "$t" && mv "$t" "$ST"
fi
LOG="$HOME/.local/state/agent-context/mem-verify/history.log"; mkdir -p "$(dirname "$LOG")"
printf '%s host=%s project=%s checked=%d corrected=%d queued=%d capped=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(hostname -s 2>/dev/null || echo '?')" \
  "$PROJECT" "$CHECKED" "$CORRECTED" "$QUEUED" "$CAPPED" >> "$LOG"
```

Substitute real values (`$PROJECT`, counts; `CAPPED`=yes if the 6-cap was hit else no). Then stop
with a 2-3 line summary (checked N, corrected M bodies, queued K). Unattended: ask nothing.
