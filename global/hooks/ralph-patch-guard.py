#!/usr/bin/env python3

'ralph-patch-guard, SessionStart.\n\nKeeps all local patches (A, B+D, C, E) on the ralph-loop plugin\'s Stop hook. The file\nlives in a git clone under the plugin cache; a plugin update re-clones or fast-forwards\nit and reverts any edit with no error, invisible until the next handoff kills a loop, or\nuntil a loop quietly starts burning its whole context on re-fed prompt text. Same failure\nmode, same remedy, as chrome-mcp-isolated-guard.\n\nEvery patch is defined here and nowhere else, so there is one copy of each. Each\nis applied independently and is idempotent (marker grep). Silent when they are all\nalready in place (the normal case).\n\nPATCH A, session handoff. Upstream `stop-hook.sh` exits 0 whenever the state\nfile\'s `session_id` differs from the session hitting Stop, sane in intent (don\'t let\nsession B drive a loop session A owns), but it does not distinguish "another session owns\nthis" from "the owning session is gone". Any handoff (/clear, resume, a compaction-driven\ntakeover) disowns the loop: no error, no log line, `active: true` still sitting\nin the file, and the loop never re-fires. The patch treats a differing session_id as\nownership only while that owner is alive (its transcript touched in the last 10\nminutes); if the owner is gone it adopts the loop.\n\nPATCH C, state file resolved from the project root, not the cwd. Upstream resolves\n`.claude/ralph-loop.local.md` relative to the hook\'s working directory. The worktree\nmandate means any loop that edits source spends most of its turns inside\n.agents/worktrees/<name> (or legacy .claude/worktrees/<name>), where that relative path does not exist, so the hook takes\nits "no active loop" exit and the loop dies with no error, `active: true` still in the file.\n\nPATCH B, re-feed policy (a pointer every fire; the full prompt only on request).\nUpstream inlines the whole prompt on every fire, so the loop dies of context exhaustion\nre-reading its own instructions. The prompt already lives in the\nstate file, so this hook does not re-send it: every fire carries a short pointer.\n`full_prompt_every: N` opts back into periodic re-anchoring (1 = every fire); absent or 0\nmeans never. The default is never: re-feeding the whole prompt fills the session\ncontext. Also folds in the ROUND line (former PATCH D) and the waiting-check stand-down:\nwhen every lane is busy, the loop writes a WAITING marker and this hook exits 0 without\nre-feeding, keeping the old iteration value so a stand-down never advances the counter\nfor a turn that re-fed nothing.\n\nPATCH E, stand down while this session\'s subagents are still running. The\nAgent tool is asynchronous: an orchestrator\'s turn ends the instant it spawns workers.\nThis hook counts turns, so "waiting on four implementers" looks like "iteration\nfinished", and a loop that fans out burns an iteration, and stacks another Stop-hook\nblock in the transcript, every few seconds for the whole time its workers run. Keys on\na subagent transcript touched in the last 2 minutes, and stands down only when that\ntranscript is newer than the orchestrator\'s own: freshness alone also covers the tail\nof an iteration, and standing down on the turn that should re-feed stops the loop.\n\nMarker naming. A marker must not match an earlier draft of itself: a marker such as\n`_ROUND_LINE` matches `_ROUND_LINE_OLD` by substring, and the fast path then skips a\nhook that no longer has the stand-down.\n\nScope, same note the dedicated test carries: this only pins the decision to patch, not\nthe rewrite itself, since the rewrite transforms specific upstream code, so a stub would\nonly prove it fails against a file that is not the real one.'
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import harness_paths as hp

MARKER_A = "LOCAL PATCH A (agent-context, audit policy)"
MARKER_B = "LOCAL PATCH B (agent-context, base10 intervals)"
MARKER_C = "LOCAL PATCH C (agent-context, worktree cwd)"



MARKER_D = "waiting-check"
MARKER_E = "LOCAL PATCH E (agent-context, 2026-09-14)"
STAND_DOWN_MARKER = "STAND-DOWN KEEPS ITERATION"

CACHE_MAX_AGE_MINUTES = 1440


def find_stop_hook(home):
    base = os.path.join(hp.claude_home(home), "plugins", "cache")
    if not os.path.isdir(base):
        return ""
    candidates = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if fn != "stop-hook.sh":
                continue
            full = os.path.join(root, fn)
            if "ralph-loop" in full:
                candidates.append(full)
    candidates.sort()
    return candidates[0] if candidates else ""


def resolve_hook_path(home):
    'The plugin cache is large, and walking it on every SessionStart is expensive.\n    Remember where the hook was; re-walk only when the cached path has vanished (plugin\n    removed or re-cloned elsewhere) or once a day, so an update under a new versioned\n    directory is still found.'
    cache_path = os.path.join(hp.state_dir(home), "ralph-patch-guard.hook-path")
    hook = ""
    if os.path.isfile(cache_path):
        try:
            age_minutes = (time.time() - os.path.getmtime(cache_path)) / 60.0
        except OSError:
            age_minutes = None
        if age_minutes is not None and age_minutes <= CACHE_MAX_AGE_MINUTES:
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    hook = fh.read().strip()
            except OSError:
                hook = ""
    if not hook or not os.path.isfile(hook):
        hook = find_stop_hook(home)
        if hook:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as fh:
                    fh.write(hook)
            except OSError:
                pass
    return hook


def already_patched(text):
    return (MARKER_A in text and MARKER_B in text and MARKER_C in text
            and MARKER_D in text and MARKER_E in text
            and STAND_DOWN_MARKER in text)


def apply_patches(text):
    'Returns (new_text, applied, failed). Each patch has an anchor, its patched\n    text, and a fallback for an older marker.'
    applied = []
    failed = []

    
    ANCHOR_A = '''if [[ -n "$STATE_SESSION" ]] && [[ "$STATE_SESSION" != "$HOOK_SESSION" ]]; then
  exit 0
fi'''

    PATCHED_A = '''# LOCAL PATCH A (agent-context, audit policy), do not drop on plugin update; the
# ralph-patch-guard SessionStart hook re-applies it.
# Upstream exits 0 whenever the state session differs, which DISOWNS a loop
# on any handoff (/clear, resume, compaction takeover), leaving active: true in the
# file and no error anywhere. A differing session_id means "another session is
# driving it" ONLY while that session is alive; after a handoff it means "this
# session inherited it". Take over a state file whose owning session is gone, and
# keep deferring while it is still running.
if [[ -n "$STATE_SESSION" ]] && [[ "$STATE_SESSION" != "$HOOK_SESSION" ]]; then
  _owner_alive=0
  if find "$HOME/.claude/projects" -name "${STATE_SESSION}.jsonl" -mmin -10 2>/dev/null | grep -q .; then
    _owner_alive=1
  fi
  if [[ "$_owner_alive" == "1" ]]; then
    exit 0                      # another LIVE session owns this loop
  fi
  echo "Ralph loop: adopting loop from ended session ${STATE_SESSION}" >&2
  if [[ -n "$HOOK_SESSION" ]] && [[ -f "$RALPH_STATE_FILE" ]]; then
    sed -i '' "s/^session_id: .*/session_id: ${HOOK_SESSION}/" "$RALPH_STATE_FILE" 2>/dev/null \\
      || sed -i "s/^session_id: .*/session_id: ${HOOK_SESSION}/" "$RALPH_STATE_FILE" 2>/dev/null || true
  fi
fi'''

    
    OLD_MARKER_A = "LOCAL PATCH (agent-context, audit policy)"

    if MARKER_A in text:
        pass                                    
    elif OLD_MARKER_A in text:
        text = text.replace(OLD_MARKER_A, MARKER_A, 1)
        applied.append("A (marker renamed)")
    elif ANCHOR_A in text:
        text = text.replace(ANCHOR_A, PATCHED_A, 1)
        applied.append("A (session handoff)")
    else:
        failed.append("A: the session-isolation block no longer matches")

    
    ANCHOR_B = '''# Output JSON to block the stop and feed prompt back
# The "reason" field contains the prompt that will be sent back to Claude
jq -n \\
  --arg prompt "$PROMPT_TEXT" \\'''

    PATCHED_B = '''# LOCAL PATCH B (agent-context, base10 intervals), do not drop on plugin update; the
# ralph-patch-guard SessionStart hook re-applies it.
# Upstream re-sends the whole prompt on every fire. The prompt lives in the state file,
# so each fire carries a one-line pointer to it. `full_prompt_every: N` in the frontmatter
# re-sends the full prompt every N fires (1 = every fire); absent or 0 means never.
# Keep the pointer to one line: it is rendered on every fire.
#
# `|| true` is required: the key is OPTIONAL, and under `set -e` a grep that matches
# nothing would abort the hook with no output at all, which looks like a dead loop.
FULL_EVERY=$(echo "$FRONTMATTER" | grep '^full_prompt_every:' | sed 's/full_prompt_every: *//' || true)
[[ "$FULL_EVERY" =~ ^[0-9]+$ ]] || FULL_EVERY=0
FULL_EVERY=$((10#$FULL_EVERY))

STATE_ABS="$RALPH_STATE_FILE"
[[ "$STATE_ABS" = /* ]] || STATE_ABS="$PWD/$RALPH_STATE_FILE"

SEND_FULL=0
if [[ $FULL_EVERY -eq 1 ]]; then
  SEND_FULL=1
elif [[ $FULL_EVERY -gt 1 ]] && [[ $(( (NEXT_ITERATION - 1) % FULL_EVERY )) -eq 0 ]]; then
  SEND_FULL=1
fi

# The ROUND line (PATCH D). This hook counts turns; the round counts batches of work
# carried to verified-and-landed. `round.py --line` prints one line appended to each fire.
# If the script is missing or fails, nothing is appended.
_ROUND_LINE=""
_REPO_ROOT="$(dirname "$(dirname "$STATE_ABS")")"
if [[ -x "$_REPO_ROOT/.agents/scripts/round.py" ]]; then
  # Stand down while the loop is waiting on work: when every lane is busy the loop writes
  # a WAITING marker and this hook exits 0; the background agent's completion wakes it.
  if _WAIT="$("$_REPO_ROOT/.agents/scripts/round.py" --waiting-check 2>/dev/null)"; then
    # STAND-DOWN KEEPS ITERATION (agent-context). Upstream already wrote NEXT_ITERATION
    # to the state file; restore the old value so a stand-down does not advance it.
    _KEEP_TMP="${RALPH_STATE_FILE}.keep.$$"
    sed "s/^iteration: .*/iteration: $ITERATION/" "$RALPH_STATE_FILE" > "$_KEEP_TMP" \\
      && mv "$_KEEP_TMP" "$RALPH_STATE_FILE" || rm -f "$_KEEP_TMP"
    echo "Ralph: standing down, $_WAIT. The completion notification wakes the loop." >&2
    exit 0
  fi
  _ROUND_LINE="$("$_REPO_ROOT/.agents/scripts/round.py" --line 2>/dev/null | head -1 || true)"
fi

if [[ $SEND_FULL -eq 1 ]]; then
  REFEED="$PROMPT_TEXT"
else
  REFEED="Ralph $NEXT_ITERATION, continue, do not restart. $STATE_ABS"
  [[ -n "$_ROUND_LINE" ]] && REFEED="$REFEED
$_ROUND_LINE"
fi

# Output JSON to block the stop and feed the prompt (or the pointer) back.
jq -n \\
  --arg prompt "$REFEED" \\'''

    B_END = '  --arg prompt "$REFEED" \\'
    FE_ANCHOR = "FULL_EVERY=$(echo"

    
    
    
    
    if FE_ANCHOR in text:
        fe = text.index(FE_ANCHOR)
        head = text[:fe].split("\n")
        j = len(head) - 1
        while j > 0 and head[j - 1].startswith("#"):
            j -= 1
        b_start = len("\n".join(head[:j])) + (1 if j else 0)
        b_end = text.index(B_END, fe) + len(B_END)
        if text[b_start:b_end] != PATCHED_B:
            text = text[:b_start] + PATCHED_B + text[b_end:]
            applied.append("B (re-feed policy, updated)")
    elif ANCHOR_B in text:
        text = text.replace(ANCHOR_B, PATCHED_B, 1)
        applied.append("B (re-feed policy)")
    else:
        failed.append("B: the jq re-feed block no longer matches")

    
    ANCHOR_C = 'RALPH_STATE_FILE="' + hp.CLAUDE_DIRNAME + '/ralph-loop.local.md"'

    PATCHED_C = '''# LOCAL PATCH C (agent-context, worktree cwd), do not drop on plugin update; the
# ralph-patch-guard SessionStart hook re-applies it.
# Upstream resolves this path RELATIVE to the hook's working directory. Every loop
# that edits source works inside .agents/worktrees/<name> (or legacy .claude/worktrees/<name>), where it does not exist,
# so the hook takes its "no active loop" exit below and the loop dies with
# `active: true` still in the file. Walk up instead: from any depth inside the
# project, worktree included, find the one state file at its root.
RALPH_STATE_FILE=".claude/ralph-loop.local.md"
if [[ ! -f "$RALPH_STATE_FILE" ]]; then
  _probe="$PWD"
  while [[ -n "$_probe" ]] && [[ "$_probe" != "/" ]]; do
    if [[ -f "$_probe/.claude/ralph-loop.local.md" ]]; then
      RALPH_STATE_FILE="$_probe/.claude/ralph-loop.local.md"
      break
    fi
    _probe="$(dirname "$_probe")"
  done
fi'''

    if MARKER_C in text:
        pass
    elif ANCHOR_C in text:
        text = text.replace(ANCHOR_C, PATCHED_C, 1)
        applied.append("C (state file from project root)")
    else:
        failed.append("C: the state-file assignment no longer matches")

    
    ANCHOR_E = '''# Validate numeric fields before arithmetic operations'''

    PATCHED_E = '''# LOCAL PATCH E (agent-context, 2026-09-14), do not drop on plugin update; the
# ralph-patch-guard SessionStart hook re-applies it.
# STAND DOWN while this session's own subagents are still working.
# The Agent tool is ASYNCHRONOUS in this harness: the orchestrator's turn ends the
# instant it spawns workers, and this hook counts TURNS, so "waiting on four
# implementers" is indistinguishable from "iteration finished". A loop that fans out
# therefore burns one iteration, and one Stop-hook block in the transcript, every few
# seconds for the entire time its workers run, which is the churn the ROUND
# comment below was already fighting. The `round.py --waiting-check` path further down
# solves this, but only for a project that ships that script; most do not, so the check
# never runs and the re-feed fires anyway.
# This is the same idea keyed on the invariant that is ALWAYS true: a live subagent
# transcript belonging to THIS session. It needs no
# cooperation from the loop prompt and works in every project.
# Self-limiting: the window is 2 minutes of *file mtime*, so a subagent that
# hangs or dies goes stale and the loop resumes on its own.
# `-mmin -2` is an INTEGER by choice. The first draft used `-mmin -1.5` for a 90s window;
# BSD/bfs find rejects a fractional argument ("1.5 is not a valid integer"), the `2>/dev/null`
# swallowed the error, and the check never matched, the exact failure this patch
# exists to prevent, hiding inside the patch itself. Do not reintroduce a fractional value.
#
# FRESHNESS ALONE KILLED THE LOOP (fixed 2026-09-04, measured). "A subagent file was
# written in the last 2 minutes" is also true of the TAIL of an iteration: the
# orchestrator spends its closing turn applying what the workers just returned, so
# their files are still fresh when that turn ends. The hook stood down on the exact
# turn that should have re-fed, and nothing ever woke the session again, because no
# subagent was left to send a completion notification. Observed here: the loop re-fed
# once and then sat at iteration 2 while its own state file still said active: true,
# which reads like a loop that finished. The old comment's assurance that
# "the completion notification wakes the session" is true only while a worker is
# still running; it is false for the last turn, which is the one that matters.
#
# So compare: stand down only if the newest fresh
# subagent transcript is NEWER than the ORCHESTRATOR's own transcript. While a worker
# runs the orchestrator is idle and writes nothing, so the worker's file is newest and
# the stand-down still does its job. Once the workers are done and the orchestrator is
# writing a full turn of its own, the orchestrator's file is newest and the loop
# re-feeds. A missing orchestrator transcript falls back to standing down, so an
# unreadable state can never re-feed over a live worker.
#
# THIS IS A TRADE, not a clean fix, and the next person to touch it should know which
# way it leans. The harness exposes no "is a subagent in flight" signal to a Stop hook,
# only file mtimes, and at the instant a turn ends the orchestrator has just written,
# so a turn that SPAWNS workers and stops looks the same as one that finished them. The
# loop can therefore still burn an iteration while workers run. That is the churn the
# 2026-08-31 draft set out to kill, and it is deliberately accepted here: a wasted
# iteration is cheap and self-correcting, while the deadlock it replaced was neither,
# the loop stopped, looking like a loop that had finished. A re-fed
# iteration that arrives mid-flight is also survivable, because the audit
# loop records its chosen layer in the ledger before it fans out, so the re-fed turn
# continues that layer.
_ralph_mtime() {
  # The old probe (`stat -f '%m' /dev/null`) returns 0 on GNU too, it reads the format
  # as a missing filename, then stats /dev/null as a FILESYSTEM, so this picked the BSD
  # branch on every Linux host and emitted `File: ...` where an epoch belonged. Decide on the
  # VALUE: GNU spelling first (it fails on BSD), accepted only if all digits.
  # Inline since 2026-09-14: the shared stat helper script it used to source was retired
  # with the store's shell scripts, and a failed source here left every mtime empty, so
  # the stand-down never fired and nothing said why. A missing file prints nothing, never 0.
  local m
  m="$(stat -c %Y "$1" 2>/dev/null)"
  case "$m" in ''|*[!0-9]*) m="$(stat -f %m "$1" 2>/dev/null)" ;; esac
  case "$m" in ''|*[!0-9]*) m="" ;; esac
  printf '%s' "$m"
}
if [[ -n "$HOOK_SESSION" ]]; then
  _NEWEST_SUB=""
  while IFS= read -r _f; do
    _t="$(_ralph_mtime "$_f")"; [ -n "$_t" ] || continue
    if [ -z "$_NEWEST_SUB" ] || [ "$_t" -gt "$_NEWEST_SUB" ]; then _NEWEST_SUB="$_t"; fi
  done < <(find "$HOME/.claude/projects"/*/"$HOOK_SESSION"/subagents -name '*.jsonl' -mmin -2 2>/dev/null)
  if [ -n "$_NEWEST_SUB" ]; then
    _ORCH_PATH="$(echo "$HOOK_INPUT" | jq -r '.transcript_path // ""')"
    _ORCH="$(_ralph_mtime "$_ORCH_PATH")"
    if [ -z "$_ORCH" ] || [ "$_NEWEST_SUB" -gt "$_ORCH" ]; then
      echo "Ralph: standing down, subagents in flight. Their completion wakes the loop." >&2
      exit 0
    fi
  fi
fi

# Validate numeric fields before arithmetic operations'''

    
    
    
    
    
    
    
    
    OLD_MARKERS_E = ("# LOCAL PATCH E (agent-context, 2026-08-31)",
                     "# LOCAL PATCH E (agent-context, 2026-09-04)")

    if MARKER_E in text:
        pass
    else:
        for old_marker in OLD_MARKERS_E:
            if old_marker in text and ANCHOR_E in text:
                s = text.index(old_marker)
                e = text.index(ANCHOR_E, s) + len(ANCHOR_E)
                text = text[:s] + ANCHOR_E + text[e:]
        if ANCHOR_E in text:
            text = text.replace(ANCHOR_E, PATCHED_E, 1)
            applied.append("E (stand down only while a subagent is ahead of "
                            "the orchestrator)")
        else:
            failed.append("E: the numeric-validation anchor no longer matches")

    return text, applied, failed


def main():
    try:
        json.load(sys.stdin)
    except (ValueError, OSError):
        pass

    home = os.environ.get("HOME") or os.path.expanduser("~")
    hook = resolve_hook_path(home)
    if not hook or not os.path.isfile(hook):
        return 0                       

    try:
        with open(hook, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return 0

    if already_patched(text):
        return 0

    new_text, applied, failed = apply_patches(text)

    if applied:
        try:
            with open(hook, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            sys.stderr.write(
                "ralph-patch-guard: re-applied patch(es) %s to %s (a plugin update had "
                "reverted them)\n" % (", ".join(applied), hook))
        except OSError:
            pass

    for f in failed:
        
        
        sys.stderr.write(
            "ralph-patch-guard: upstream changed, %s in %s; re-derive that patch by "
            "hand.\n" % (f, hook))

    
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                  
        sys.exit(0)
