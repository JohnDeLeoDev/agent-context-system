/**
 * agent-context-mcp: bridges the `agent-context` MCP server into pi as native tools.
 *
 * pi has no built-in MCP client ("No MCP" in pi's README/usage.md).
 * This extension spawns the agent-context stdio MCP server, enumerates its tools, and
 * registers each one as a native pi tool so the LLM can call `get_session_context`,
 * `get_memory`, `search_all`, etc. as the shared AGENTS.md instructs.
 *
 * Zero npm dependencies: speaks MCP's newline-delimited JSON-RPC over stdio
 * directly, so it works on any machine (synced via ~/.agents) with only `uv`
 * and pi's bundled Node available. The agent-context directory is resolved relative
 * to $HOME, so it is portable across machines with different usernames.
 *
 * `CANONICAL SOURCE IS THE AGENT-CONTEXT STORE`, as of 2026-09-04. This header used
 * to name ~/.agents/pi-extensions/ (Syncthing-shared, symlinked in by
 * ~/.agents/setup.sh); neither that directory nor that script exists on any
 * machine any more, so the file was surviving as a loose copy on exactly one
 * host with no source behind it, which is why it never reached the others, and
 * why the laptop's pi had no agent-context tools at all. It is now a store
 * script, projected into ~/.pi/agent/extensions/ by harness-materialize's
 * materialize_pi_extensions step. Edit it in the store (upsert_script /
 * edit_body), never here: this copy is overwritten at SessionStart.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { accessSync, constants, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const AGENT_CONTEXT_DIR = join(homedir(), ".agent-context", "server");
const RELAY_PATH = join(homedir(), ".local", "bin", "agent-context");
const PROTOCOL_VERSION = "2024-11-05";
const REQUEST_TIMEOUT_MS = 60_000;
// Inter-agent messaging (policy), server side: peer_wake.py.
const PEER_MESSAGE_METHOD = "notifications/agent-context/peer_message";
const PEER_CLIENT_ENV = "AGENT_CONTEXT_PEER_CLIENT";

// The standalone relay outlives a retired clone, so it wins when it is installed and runnable.
// Chosen at every spawn: a relay installed mid-session is used from the next reconnect. The
// relay reads its host and token from the environment or ~/.config/agent-context/env itself;
// this extension reads neither.
function installedRelay(): string | null {
  try {
    if (!statSync(RELAY_PATH).isFile()) return null;
    accessSync(RELAY_PATH, constants.X_OK);
    return RELAY_PATH;
  } catch {
    return null;
  }
}

interface McpTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

export default function (pi: ExtensionAPI) {
  let child: ChildProcessWithoutNullStreams | null = null;
  let startPromise: Promise<void> | null = null;
  let stdoutBuf = "";
  let nextId = 1;
  const pending = new Map<
    number,
    { resolve: (v: any) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();
  const registered = new Set<string>();

  function cleanupChild(reason: string) {
    for (const { reject, timer } of pending.values()) {
      clearTimeout(timer);
      reject(new Error(`agent-context connection closed: ${reason}`));
    }
    pending.clear();
    child = null;
    startPromise = null;
    stdoutBuf = "";
  }

  function handleLine(line: string) {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg: any;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      return; // ignore non-JSON noise on stdout
    }
    if (msg.id != null && pending.has(msg.id)) {
      const { resolve, reject, timer } = pending.get(msg.id)!;
      clearTimeout(timer);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message ?? JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
    // A peer message (policy). The bridge forwards the daemon's push because this extension
    // named itself when it started the bridge (PEER_CLIENT_ENV); the text is the envelope the
    // agent reads. An idle session starts a turn on it; a busy one takes it after its turn.
    if (msg.method === PEER_MESSAGE_METHOD && typeof msg.params?.text === "string" && msg.params.text) {
      try {
        pi.sendUserMessage(msg.params.text, { deliverAs: "followUp" });
      } catch {
        // a wake that cannot be made must not take the bridge's reader down
      }
    }
  }

  function write(obj: unknown) {
    if (!child) throw new Error("agent-context process not running");
    child.stdin.write(JSON.stringify(obj) + "\n");
  }

  function rpc(method: string, params?: unknown): Promise<any> {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id);
          reject(new Error(`agent-context request timed out: ${method}`));
        }
      }, REQUEST_TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
      try {
        write({ jsonrpc: "2.0", id, method, ...(params ? { params } : {}) });
      } catch (e) {
        clearTimeout(timer);
        pending.delete(id);
        reject(e as Error);
      }
    });
  }

  function notify(method: string, params?: unknown) {
    write({ jsonrpc: "2.0", method, ...(params ? { params } : {}) });
  }

  // (Re)spawn the server and complete the MCP handshake. Idempotent.
  function ensureStarted(): Promise<void> {
    if (startPromise) return startPromise;
    startPromise = new Promise<void>((resolve, reject) => {
      let proc: ChildProcessWithoutNullStreams;
      try {
        const relay = installedRelay();
        // Naming this client makes the bridge declare the `pi` wake route and forward the push.
        const bridgeEnv = { ...process.env, [PEER_CLIENT_ENV]: "pi" };
        proc = relay
          ? spawn(relay, [], { stdio: ["pipe", "pipe", "pipe"], env: bridgeEnv })
          : spawn("uv", ["run", "--directory", AGENT_CONTEXT_DIR, "agent-context"], {
              stdio: ["pipe", "pipe", "pipe"],
              env: bridgeEnv,
            });
      } catch (e) {
        startPromise = null;
        reject(e as Error);
        return;
      }
      child = proc;

      proc.stdout.setEncoding("utf8");
      proc.stdout.on("data", (chunk: string) => {
        stdoutBuf += chunk;
        let i: number;
        while ((i = stdoutBuf.indexOf("\n")) >= 0) {
          const line = stdoutBuf.slice(0, i);
          stdoutBuf = stdoutBuf.slice(i + 1);
          handleLine(line);
        }
      });
      proc.stderr.setEncoding("utf8");
      proc.stderr.on("data", () => {
        /* agent-context logs INFO to stderr; drain and ignore */
      });
      proc.on("error", (err) => {
        cleanupChild(err.message);
        reject(err);
      });
      proc.on("exit", (code, sig) => {
        cleanupChild(`exited (code=${code}, signal=${sig})`);
      });

      // Handshake: initialize -> notifications/initialized.
      rpc("initialize", {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: "pi-agent-context-bridge", version: "1.0.0" },
      })
        .then(() => {
          notify("notifications/initialized");
          resolve();
        })
        .catch((e) => {
          startPromise = null;
          reject(e);
        });
    });
    return startPromise;
  }

  function toToolResult(result: any) {
    const content = Array.isArray(result?.content)
      ? result.content.map((c: any) =>
          c?.type === "text" ? { type: "text", text: c.text ?? "" } : { type: "text", text: JSON.stringify(c) },
        )
      : [{ type: "text", text: typeof result === "string" ? result : JSON.stringify(result ?? {}) }];
    return { content, details: {}, isError: result?.isError === true };
  }

  async function registerTools(notifyUi?: (msg: string, level: "info" | "error") => void) {
    await ensureStarted();
    const list = await rpc("tools/list", {});
    const tools: McpTool[] = list?.tools ?? [];
    let added = 0;
    for (const tool of tools) {
      if (registered.has(tool.name)) continue;
      registered.add(tool.name);
      added++;
      const schema = tool.inputSchema ?? { type: "object", properties: {} };
      pi.registerTool({
        name: tool.name,
        label: tool.name,
        description: tool.description ?? `agent-context: ${tool.name}`,
        // MCP inputSchema is standard JSON Schema; wrap it so typebox treats it as-is.
        parameters: Type.Unsafe(schema),
        async execute(_toolCallId, params) {
          await ensureStarted();
          const result = await rpc("tools/call", { name: tool.name, arguments: params ?? {} });
          return toToolResult(result);
        },
      });
    }
    notifyUi?.(`agent-context: ${added} tool${added === 1 ? "" : "s"} ready (${tools.length} total)`, "info");
  }

  pi.on("session_start", async (_event, ctx) => {
    try {
      await registerTools(ctx.hasUI ? (m, l) => ctx.ui.notify(m, l) : undefined);
    } catch (e) {
      const msg = `agent-context bridge failed to start: ${(e as Error).message}`;
      if (ctx.hasUI) ctx.ui.notify(msg, "error");
    }
  });

  pi.on("session_shutdown", async () => {
    const proc = child;
    cleanupChild("session shutdown");
    proc?.kill();
  });

  // Diagnostics: /agent-context shows connection status and reconnects on demand.
  pi.registerCommand("agent-context", {
    description: "Show agent-context MCP bridge status (and reconnect)",
    handler: async (_args, ctx) => {
      try {
        await ensureStarted();
        const list = await rpc("tools/list", {});
        ctx.ui.notify(`agent-context connected: ${list?.tools?.length ?? 0} tools, ${registered.size} registered`, "info");
      } catch (e) {
        ctx.ui.notify(`agent-context not reachable: ${(e as Error).message}`, "error");
      }
    },
  });
}
