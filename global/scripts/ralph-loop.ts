/**
 * Ralph Loop for pi.
 *
 * Claude Code drives ralph loops from a Stop hook (the ralph-loop plugin).
 * pi has no equivalent hook, so a loop armed by `setup-ralph-loop.sh` arms a
 * state file nothing ever reads and the loop dies silently on the first turn.
 *
 * This extension is the pi half. It reads the SAME state file
 * (`<root>/.claude/ralph-loop.local.md`) with the same frontmatter, so a loop
 * can be started in either harness and continued in the other.
 *
 * Semantics deliberately mirror the locally-patched stop-hook.sh:
 *   - walk up from cwd to find the state file (worktrees live below the root)
 *   - honour `completion_promise`: <promise>TAG</promise> in the last assistant
 *     text block ends the loop and removes the state file
 *   - honour `max_iterations` (0 = infinite)
 *   - re-feed a ONE-LINE pointer by default, not the whole prompt
 *     (`full_prompt_every: N` opts back into periodic full re-feeds)
 *   - corruption is fatal-but-quiet: notify, remove the file, stop looping
 *
 * pi differences that matter:
 *   - `agent_settled` is the honest "pi will not continue on its own" signal,
 *     so there is no turn/subagent race to defend against; pi's subagent tool
 *     is synchronous, unlike Claude Code's detached Agent tool.
 *   - the re-feed is a real user message (`pi.sendUserMessage`), which triggers
 *     the next turn directly instead of blocking a stop decision.
 *
 * NOTHING on the Claude Code side is modified by this file, and nothing here
 * may change what that side reads. Two rules keep it that way:
 *   1. Only keys the Claude hook already greps for are ever WRITTEN back
 *      (`iteration`). pi's own bookkeeping goes in `pi_*` keys the hook does
 *      not look at, and its frontmatter parser ignores unknown keys.
 *   2. pi STANDS DOWN when `session_id` names a Claude Code session whose
 *      transcript is still warm — the same 10-minute liveness probe the hook
 *      itself uses to decide whether another session owns the loop. Without
 *      this, a project open in both harnesses would have two drivers feeding
 *      the same loop and double-counting its iterations.
 */

import { existsSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

/** The only surface `stop` needs — satisfied by ExtensionContext and ExtensionCommandContext alike. */
type UiHost = {
  ui: {
    notify(message: string, type?: "info" | "warning" | "error"): void;
    setStatus(key: string, text: string | undefined): void;
  };
};

const STATE_REL = join(".claude", "ralph-loop.local.md");

/** Matches the stop hook's own `-mmin -10` liveness window. */
const CLAUDE_OWNER_WARM_MS = 10 * 60 * 1000;

interface RalphState {
  path: string;
  iteration: number;
  maxIterations: number;
  completionPromise: string | null;
  fullPromptEvery: number;
  sessionId: string | null;
  prompt: string;
  raw: string;
}

/**
 * True when `sessionId` belongs to a Claude Code session that has written its
 * transcript recently. Claude Code lays transcripts out as
 * `~/.claude/projects/<slug>/<session-id>.jsonl`, so this is one shallow listing
 * plus a stat, never a recursive walk.
 */
function claudeOwnerIsWarm(sessionId: string): boolean {
  const projects = join(homedir(), ".claude", "projects");
  let slugs: string[];
  try {
    slugs = readdirSync(projects);
  } catch {
    return false; // no Claude Code on this machine — nothing to defer to
  }
  const cutoff = Date.now() - CLAUDE_OWNER_WARM_MS;
  for (const slug of slugs) {
    try {
      if (statSync(join(projects, slug, `${sessionId}.jsonl`)).mtimeMs > cutoff) return true;
    } catch {
      /* not in this project dir */
    }
  }
  return false;
}

function findStateFile(cwd: string): string | null {
  let probe = resolve(cwd);
  for (;;) {
    const candidate = join(probe, STATE_REL);
    if (existsSync(candidate)) return candidate;
    const parent = dirname(probe);
    if (parent === probe) return null;
    probe = parent;
  }
}

function frontmatterValue(frontmatter: string, key: string): string | null {
  for (const line of frontmatter.split("\n")) {
    if (!line.startsWith(`${key}:`)) continue;
    let value = line.slice(key.length + 1).trim();
    if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1);
    }
    return value;
  }
  return null;
}

/** Returns the parsed state, or a string describing why the file is unusable. */
function parseState(path: string): RalphState | string {
  const raw = readFileSync(path, "utf8");
  const lines = raw.split("\n");
  if (lines[0]?.trim() !== "---") return "no frontmatter";

  let close = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === "---") {
      close = i;
      break;
    }
  }
  if (close === -1) return "unterminated frontmatter";

  const frontmatter = lines.slice(1, close).join("\n");
  const prompt = lines.slice(close + 1).join("\n").trim();
  if (!prompt) return "no prompt text";

  const iterationRaw = frontmatterValue(frontmatter, "iteration") ?? "";
  const maxRaw = frontmatterValue(frontmatter, "max_iterations") ?? "";
  if (!/^\d+$/.test(iterationRaw)) return `'iteration' is not a number (got '${iterationRaw}')`;
  if (!/^\d+$/.test(maxRaw)) return `'max_iterations' is not a number (got '${maxRaw}')`;

  const promiseRaw = frontmatterValue(frontmatter, "completion_promise");
  const fullEveryRaw = frontmatterValue(frontmatter, "full_prompt_every") ?? "";
  const sessionRaw = frontmatterValue(frontmatter, "session_id");

  return {
    path,
    iteration: Number(iterationRaw),
    maxIterations: Number(maxRaw),
    completionPromise: promiseRaw && promiseRaw !== "null" ? promiseRaw : null,
    fullPromptEvery: /^\d+$/.test(fullEveryRaw) ? Number(fullEveryRaw) : 0,
    sessionId: sessionRaw && sessionRaw !== "null" ? sessionRaw : null,
    prompt,
    raw,
  };
}

/**
 * Only ever rewrites `iteration` — the one field both harnesses agree on.
 * Deliberately does NOT touch `session_id`: stamping a pi session id there
 * would make the Claude hook believe an ended session owned the loop and adopt
 * it, which is exactly the ping-pong this extension is built to avoid.
 */
function writeIteration(state: RalphState, next: number): void {
  const updated = state.raw.replace(/^iteration: .*$/m, `iteration: ${next}`);
  const tmp = `${state.path}.tmp.${process.pid}`;
  writeFileSync(tmp, updated, "utf8");
  renameSync(tmp, state.path);
}

/** Last assistant text block on the active branch, or "" for a tool-only turn. */
function lastAssistantText(ctx: Pick<ExtensionContext, "sessionManager">): string {
  const entries = ctx.sessionManager.getEntries();
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i] as { type?: string; message?: { role?: string; content?: unknown } };
    if (entry?.type !== "message" || entry.message?.role !== "assistant") continue;
    const content = entry.message.content;
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) continue;
    for (let b = content.length - 1; b >= 0; b--) {
      const block = content[b] as { type?: string; text?: string };
      if (block?.type === "text" && typeof block.text === "string") return block.text;
    }
  }
  return "";
}

/** First <promise>…</promise> payload, whitespace-normalized. */
function extractPromise(text: string): string | null {
  const match = /<promise>([\s\S]*?)<\/promise>/.exec(text);
  if (!match) return null;
  return match[1].trim().replace(/\s+/g, " ");
}

function stop(ctx: UiHost, path: string, message: string, level: "info" | "warning"): void {

  // A STOPPED LOOP MUST NOT LOOK LIKE A FINISHED ONE (policy).
  //
  // Every branch that ends a loop routes through here, and all of them did the same
  // two things: delete the state file, then notify. That makes a parse failure, a
  // corrupt file, a completed promise and an exhausted iteration cap byte-identical
  // after the fact -- the file is simply gone. The notify is not a record either: it
  // is gone the moment the pane scrolls, and a session spent measuring this could not
  // recover a single "Ralph loop:" line from a captured pane across four runs, so the
  // branch that fired could not be named from outside the process.
  //
  // So write down why, before destroying the evidence. The tombstone sits beside the
  // state file, is overwritten by the next stop, and is never read back by this
  // extension -- it exists purely so a human or a later session can ask what happened.
  // The catch REPORTS rather than swallows, and that is the point of it (policy).
  // Measured on laptop 2026-09-05: on a clean max-iterations stop the state file
  // disappears and no `.stopped` file is ever created, even though this code is the
  // loaded extension and there is no second copy. That leaves exactly two candidate
  // causes -- the write throws in here, or something other than stop() is removing
  // the state file -- and a silent catch makes them indistinguishable, so two
  // consecutive sessions reached opposite wrong conclusions from the tombstone's
  // absence. A swallowed failure is the fleet's most expensive recurring shape, and
  // it cost this instrument its whole reason to exist. Still non-fatal: bookkeeping
  // must never keep a loop alive that asked to stop.
  let tombstoneError: string | null = null;
  try {
    writeFileSync(
      `${path}.stopped`,
      `${JSON.stringify({ stoppedAt: new Date().toISOString(), level, message }, null, 2)}\n`,
      "utf8",
    );
  } catch (err) {
    tombstoneError = err instanceof Error ? err.message : String(err);
  }
  try {
    rmSync(path, { force: true });
  } catch {
    /* the notify below is what the user acts on */
  }
  ctx.ui.setStatus("ralph", undefined);
  // If this fires, the write is the cause. If the tombstone is still missing with
  // NOTHING reported here, stop() is not what deleted the state file and the real
  // exit path is still unknown -- which is the finding, not a reason to retry.
  ctx.ui.notify(
    tombstoneError
      ? `${message}\n(could not write the stop record beside the state file: ${tombstoneError})`
      : message,
    level,
  );
}

export default function (pi: ExtensionAPI) {
  // Guards re-entrancy: sendUserMessage triggers a turn, whose settle fires this
  // handler again. Without the flag a slow filesystem could double-feed.
  let feeding = false;

  pi.on("agent_settled", async (_event, ctx) => {
    if (feeding) return;
    if (!ctx.isIdle()) return;
    if (ctx.hasPendingMessages()) return;

    const path = findStateFile(ctx.cwd);
    if (!path) return;

    let state: RalphState | string;
    try {
      state = parseState(path);
    } catch (error) {
      stop(ctx, path, `Ralph loop: cannot read state file (${String(error)}). Loop stopped.`, "warning");
      return;
    }
    if (typeof state === "string") {
      stop(ctx, path, `Ralph loop: state file corrupted — ${state}. Loop stopped.`, "warning");
      return;
    }

    if (state.sessionId && claudeOwnerIsWarm(state.sessionId)) {
      // A live Claude Code session is driving this loop. Standing down is not a
      // failure state and must not be noisy: it is the normal outcome whenever
      // a project is open in both harnesses at once.
      ctx.ui.setStatus("ralph", `ralph · standing down (Claude session ${state.sessionId.slice(0, 8)} owns it)`);
      return;
    }

    if (state.completionPromise) {
      const promise = extractPromise(lastAssistantText(ctx));
      if (promise !== null && promise === state.completionPromise) {
        stop(ctx, path, `Ralph loop complete: <promise>${state.completionPromise}</promise>`, "info");
        return;
      }
    }

    if (state.maxIterations > 0 && state.iteration >= state.maxIterations) {
      stop(ctx, path, `Ralph loop: max iterations (${state.maxIterations}) reached.`, "info");
      return;
    }

    const next = state.iteration + 1;
    try {
      writeIteration(state, next);
    } catch (error) {
      stop(ctx, path, `Ralph loop: cannot update state file (${String(error)}). Loop stopped.`, "warning");
      return;
    }

    // Default is a ONE-LINE pointer. The prompt already lives in the state file
    // and the agent can re-read it; inlining it every fire is what exhausts a
    // long loop's context. `full_prompt_every: N` opts back in (1 = every fire).
    const sendFull =
      state.fullPromptEvery === 1 ||
      (state.fullPromptEvery > 1 && (next - 1) % state.fullPromptEvery === 0);
    const refeed = sendFull ? state.prompt : `Ralph ${next} — continue, do not restart. ${state.path}`;

    ctx.ui.setStatus(
      "ralph",
      state.completionPromise
        ? `ralph ${next}${state.maxIterations > 0 ? `/${state.maxIterations}` : ""} · promise ${state.completionPromise}`
        : `ralph ${next}${state.maxIterations > 0 ? `/${state.maxIterations}` : ""} · uncapped`,
    );

    feeding = true;
    try {
      pi.sendUserMessage(refeed);
    } finally {
      feeding = false;
    }
  });

  pi.registerCommand("ralph-status", {
    description: "Show the active Ralph loop's state (iteration, cap, completion promise)",
    handler: async (_args, ctx) => {
      const path = findStateFile(ctx.cwd);
      if (!path) {
        ctx.ui.notify("No active Ralph loop.", "info");
        return;
      }
      const state = parseState(path);
      if (typeof state === "string") {
        ctx.ui.notify(`Ralph state file corrupted — ${state}: ${path}`, "warning");
        return;
      }
      ctx.ui.notify(
        [
          `Ralph loop active: ${state.path}`,
          `iteration ${state.iteration}${state.maxIterations > 0 ? ` / ${state.maxIterations}` : " (uncapped)"}`,
          `completion promise: ${state.completionPromise ?? "none"}`,
          `full prompt every: ${state.fullPromptEvery || "never (pointer only)"}`,
        ].join("\n"),
        "info",
      );
    },
  });

  pi.registerCommand("cancel-ralph", {
    description: "Cancel the active Ralph loop by removing its state file",
    handler: async (_args, ctx) => {
      const path = findStateFile(ctx.cwd);
      if (!path) {
        ctx.ui.notify("No active Ralph loop.", "info");
        return;
      }
      stop(ctx, path, `Ralph loop cancelled: ${path}`, "info");
    },
  });
}
