# Core system health

Rationale and mechanics behind the `Core system health` global instruction.

## The problem

Every core system in this fleet fails **open**, and in each case the degraded mode is
indistinguishable from the working one:

| System | Silent failure | Why nothing notices |
|---|---|---|
| `agent-context` MCP | server fails to launch → session has no instructions, no memory, **no guardrails** | `CLAUDE.md` says "call `get_session_context`", but nothing verified it happened. The session answers normally. |
| LSP — dead | `lspd.py --mcp` exits 127 with a good stderr sentence | stderr from a failed MCP launch reaches no session. The agent demotes to grep, which still returns hits. |
| LSP — alive but wrong | resolves nothing; answers "not found" | Worse than dead: "not found" reads as *the symbol does not exist*, so the agent draws a confident wrong conclusion. |
| materialize chain | `\|\| echo "(non-fatal)" >&2`, exit 0 | "Non-fatal" means nobody hears. |

## Mechanism

    global/scripts/core-health-probe.py       -> state/health/mcp.json
    global/scripts/lsp-canary.py              -> state/health/lsp/<cwd>-<server>.json
    global/scripts/hook-registration-probe.py -> state/health/hooks.json
    global/scripts/health-record.py           -> state/health/<component>.json
    global/hooks/preflight-core-health.py     -- aggregates + injects (SessionStart, last)
    global/hooks/require-store-bootstrap.py   -- the bootstrap gate (UserPromptSubmit)
    global/lsp-canaries.json                  -- per-project canary symbols

Both slow probes run **detached**: `claude mcp list` dials every server including
OAuth ones, and the canary deliberately waits out a cold index. Each writes a verdict
the *next* session reads.

One exception: a cached `mcp.json` that names a degraded or missing
server is re-probed **in the foreground** (`reprobe_now`, `REPROBE_TIMEOUT` 20 s) before the block repeats it. A clean verdict is still trusted for `MCP_MAX_AGE` (6 h)
and refreshed in the background. Why: a fault fixed inside that window would still be announced. `HEAL_TIMEOUT` is
95 s so both fit under the 120 s hook timeout. Tests: `test-preflight-mcp-reprobe.py`.

**An OAuth server reported as `needs-authentication` is very often transient and clears
itself** — the token refreshes on the next successful dial, and the degraded core systems block then
stops naming it. The probe holds a first sighting in `unconfirmed`, which the
block does not show; the server is reported only when a probe at least 5 minutes later
(`CONFIRM_GAP`, within a 6 h `SIGHTING_WINDOW`) still sees it, and the preflight runs that probe
when the verdict's `recheck_after` passes. A `⊘ Disabled for this project` server is healthy: user turned it off.
Tests: `test-core-health-probe.py`. Do not open a re-authentication flow, and do not report it to user as a
broken system, on a single sighting: check whether the same server is still named in the
next session's block first.

### Staleness is itself a finding

A probe verdict older than `MCP_STALE` (36h) is reported as loudly as a failure. A
monitor that quietly stopped running is precisely the failure class this exists to
kill.

## The LSP canary

sourcekit-lsp indexes asynchronously and **answers negatively while it does**:
immediately after MCP `initialize`, `definition("AgentEntry")` returns
`AgentEntry not found`, and seconds later the same call returns the right answer. So the first symbol question
of a session — the one a fresh agent is most likely to ask — is the one most likely
to be answered wrong, with no error anywhere.

Two consequences, both implemented:

1. The canary retries across a 90s budget before reporting anything, which is what
   separates a cold index (transient) from a rotten BSP binding (permanent). A canary
   that reported the t=0 answer would cry wolf every session.
2. It runs **every** session rather than on a staleness timer, because it is also the
   **warm-up**. By the time an agent asks a real question the index is up.

Why `Kit/Models` is the right canary subtree: `buildServer.json`
is what makes `Kit/Models` resolve at all (and costs `Kit/CLI`). A symbol from `iOS/`
would be a *weaker* canary — `iOS/` resolves off the DerivedData index store either
way and would keep passing while `Kit/Models` was dead.

## The bootstrap gate

`require-store-bootstrap` (UserPromptSubmit) scans the transcript for a
`get_session_context` tool_use, then stamps `state/health/bootstrap/<session_id>` so
later prompts cost one `stat()` instead of a rescan.

Two failures, two responses:

* **Tool exists, went uncalled** → `decision: "block"`. Fixable in one call, so make
  it impossible to skip rather than merely rude to skip.
* **Server absent or unhealthy** (per `mcp.json`) → never block; inject an
  `UNGUARDED SESSION` banner. Blocking every prompt over a fault the agent cannot fix
  would brick the session.

It **fails open** on an unreadable transcript, unlike `block-deploy.py`. That hook
gates one irreversible action, so a false block costs a retry; this one stands in
front of every prompt, where failing closed would lock user out of his own session.

## A degradation this probe set does not cover: a stale disk copy

Everything here asks whether a system is live. It never asks whether what it delivers still
matches the store. A projected file that has drifted — or a harness directory outside the
repo root, which is in no store scope at all — serves old rules while every probe reads
green, which is the same fail-open shape this doc exists for. `project-materialize.py
--check` is the instrument (it reports shadows and stray harness dirs, and exits 3);
`get_doc("agents-layout.md")` is the map.

## Hook registration

A hook can be in the store, in the store's `global/hooks`, committed, synced to seven
machines — and never run, because nothing added it to `MANAGED` in
`home-settings-sync.py`, which then reports "already in sync" (true, and useless: in
sync with a table that never learned about the hook).

`hook-registration-probe` asserts against **`settings.json`**, not against `MANAGED`.
Comparing to `MANAGED` would only catch hooks nobody wired; comparing to the file
Claude Code actually reads also catches one that was wired and later dropped,
clobbered or lost to a merge. It checks both directions — a store hook with no
registration, and a registration pointing at a file that no longer exists (the
harness skips those silently).

Deliberate non-registrations live in that script's `EXEMPT` map and **each needs a
reason**, so "unregistered on purpose" and "unregistered by accident" stay
distinguishable.

## Adding a core system

Add its probe at the same time. A system with no probe is one whose failure nobody
will ever see — which is the entire finding this work started from.

## Self-repair

Probes make failure visible; `self-heal.py` closes the
other half wherever a fault has one obvious remedy.

Three rules:

1. **Recipes are hardcoded in the script.** A repair command is never read from a
   verdict file. Verdicts are written by background probes into a state directory;
   executing their contents would turn every probe into an arbitrary-code-execution
   path. A verdict *selects* a recipe by key; it never supplies one.
2. **Never heal silently.** A repair that quietly fixes things recreates the very
   problem this system exists to end. Successes are reported as loudly as failures,
   in a `SELF-REPAIRED` block, and the instruction requires the agent to mention
   them. "It broke and fixed itself twice a day for a month" should be findable.
3. **Cheap and idempotent only.** Anything slow, CPU-hungry or side-effecting stays
   manual and is surfaced as an exact command.

Auto-repaired: the store server venv (`uv sync`), hook registration and a failed
settings sync (`home-settings-sync.py`), harness projection
(`harness-materialize.py`), git remote/auth/signing convergence
(`git-mirror-converge --quick` — config only, no commits, no pushes). Throttled to
one attempt per fault key per 6h, so a permanently broken thing does not become a
background CPU leak. A successful repair **clears the verdict that triggered it**;
without that the fault is fixed and still reported, which trains the reader to
ignore the block.

Left manual on purpose: repopulating an empty DerivedData needs a real build
(minutes, pins a core) — the exact `xcodebuild` line, with the right scheme, is put
in the finding instead.

## In-session repair: replacing a language server

A dead LSP stops code work, so the question a stopped session actually has is *what may I do
about it from here*. Three failures, three answers, and one thing that is never an
answer.

**Never**: kill the MCP bridge, `pkill -f lspd`, or restart the MCP server from inside
the session. Claude Code does not respawn a failed MCP server: it marks it dead for the
whole session, so every one of those turns a recoverable server fault into
an unrecoverable session fault.

**1. The bridge is connected and the server is answering wrongly.** The half-dead case:
`hover` resolves correctly while every `definition`/`references` by symbol name answers
"not found" (Kotlin/kotlin-lsp#249, open upstream — the warm-cache restore loses its stub
serializers). Nothing crashed, so nothing automatic fires, and the answer is
byte-identical to a true negative. `lsp-failure-tripwire` catches it by referring a miss
to the workspace canary; the repair is a fresh process:

    python3 ~/.agent-context/global/scripts/lspd.py --restart --key <server> --workspace <main checkout>

It replaces only the server child. The daemon replays the cached `initialize`, re-opens
the documents that were open, and never closes a client socket, so the session's LSP
tools keep working across it. It prints `pid <old> -> <new>  handshaked=yes warm=yes`,
and reports the handshake rather than the spawn because a pid with no `initialize`
behind it answers protocol errors. `--key` is required and it never sweeps: a restart
costs a cold index, and every other session on that daemon pays it too. Ask the
workspace's canary symbol again before trusting the next answer.

**1b. `--status` prints `code=STALE`.** That is not a broken server — it is a daemon
running an older build of `lspd.py` than the one on disk, because a daemon is pinned to the
build it was spawned with. `--restart` does not fix it: that replaces the server and leaves the
daemon. Retire it instead, when nothing is attached:

    python3 ~/.agent-context/global/scripts/lspd.py --upgrade --key <server>

It refuses while a client is attached (`--force` overrides, at the cost of that session's
LSP), and a stale daemon also adopts the current build on its own at its next natural
restart. So a STALE line is a "do this when the coast is clear", not an outage, and
`--status` still exits 0 for it.

**3. The MCP server never connected this session** (`CONNECT_TIMEOUT`, tools absent from
the roster). Nothing in-session repairs this: a tool-roster change needs the client to
reconnect. Say so, fall back honestly, and fix it for the next session — warm the daemon
out of band with `python3 ~/.agent-context/global/scripts/lsp-canary.py <cwd>`, which both warms and
writes a verdict, then start a fresh session. A cold daemon is itself a cause here:
kotlin-lsp's `initialize` blocks on the Gradle import (#148/#189) and routinely exceeds
the harness's 30s MCP connect budget, so the first session after a daemon dies is the one
that pays.

## LSP provisioning (chezmoi)

Declaring a language server in `mcp-servers.json` does not install one.
`run_onchange_after_install-lsp-toolchain.sh.tmpl` provisions the whole
toolchain on every Mac, and `dot_local/bin/executable_csharp-ls` makes the shim a
managed dotfile (the stray `~/.dotnet/tools` copy is in `.chezmoiremove`).

### The dotnet PATH entry

Microsoft's dotnet installer writes `/etc/paths.d/dotnet-cli-tools` containing the
**literal string** `~/.dotnet/tools`. A tilde never expands inside `PATH`, so that
entry resolves to nothing on every Mac: `command -v csharp-ls` fails, the LSP bridge
exits 127, and the only report is a stderr line no session reads.

Compounding it, a **login** zsh sources `/etc/zprofile`, which runs `path_helper` and
rebuilds `PATH` from `/etc/paths.d` — dropping `.zshenv`'s prepends. Measured: the
three directories are present under `zsh -c` and absent under `zsh -lc`.

Fixed in two places on purpose, because neither covers every launch:

* `$ZDOTDIR/.zshenv` prepends `$HOME/.dotnet/tools`, which covers
  terminal-launched harnesses.
* `lspd.py --mcp` guarantees `.local/bin`, `.dotnet/tools` and
  `/opt/homebrew/bin` itself — covers GUI/launchd-launched harnesses, which read no
  shell config at all. `/opt/homebrew/bin` is in that list because its absence made
  the script report `xcode-build-server` as "not installed" on a machine where it
  plainly was, sending the reader off to reinstall something they already had.

### buildServer.json: existing is not the same as usable

### C# and `solution/open`

`csharp-ls` on this fleet is a POSIX sh shim over `roslyn-language-server`, which needs
`--stdio` (else it talks over a named pipe) and `--autoLoadProjects` (else nothing
loads and every symbol is "not found"). Roslyn also expects a `solution/open` notification; without it every `tools/call`
returns `broken pipe`. `lspd` sends it once per server start, and `lspd.py --mcp` is
the MCP front end.
