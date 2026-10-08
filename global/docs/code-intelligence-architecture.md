# Code intelligence architecture — the rebuild

---

## 1. What is actually wrong

Six distinct faults, not one. They were pursued one at a time for a day and a half because
every one of them reports as the same bare `CONNECTION_CLOSED` or 60s timeout.

### 1.1 Two code-intelligence clients per workspace (THE dominant fault)

7 language servers where there should be 3. ~3.7 GB of duplicate tsserver alone.

**Root cause found — a config inconsistency, not a mystery.** The Kotlin fix was applied and
never propagated:

The two workspaces with a `true` are exactly the two with duplicate servers. This is a
two-line fix and it is the single highest-value action in this document.

### 1.2 The built-in LSP tool cannot be made to share

Confirmed from the official plugin reference: *"Claude Code does not support absolute paths or
socket/pipe connections to already-running servers — it spawns the server process directly."*
It accepts `transport: socket` in `.lsp.json` and **ignores it**, always running stdio.
One server per session, never reused across sessions, never cleaned up on exit
(anthropics/claude-code [#76367](https://github.com/anthropics/claude-code/issues/76367),
[#87301](https://github.com/anthropics/claude-code/issues/87301) — one tsserver reached
**61.5 GB**). No `DISABLE_LSP` env var exists; [#15101](https://github.com/anthropics/claude-code/issues/15101)
asked for one and was auto-closed.

**Consequence: you cannot have both paths. Pick one per language.** Since the MCP bridge is
the one that reaches every harness (the whole premise of `mcp-servers.json`), the plugins go.

### 1.3 Probes measure a different process than the session uses

### 1.4 kotlin-lsp is architecturally single-instance, and cannot be configured out of it

`--system-path` looks like the fix. **It is not** — measured here, not assumed:

- Started `kotlin-lsp --socket … --multi-client --system-path <scratch>`
- `idea.config.path` / `idea.system.path` did move to the scratch dir
- That dir totalled **56 KB**. The real analyzer index stayed at
  `~/Library/Caches/JetBrains/analyzer/workspaces/<hash>/` — three workspace hashes, untouched.

This reproduces Kotlin/kotlin-lsp [#250](https://github.com/Kotlin/kotlin-lsp/issues/250)
exactly. The only reported workaround is an OS-level symlink, which relocates the *shared*
store rather than isolating per process.

Other confirmed kotlin-lsp constraints:
- [#249](https://github.com/Kotlin/kotlin-lsp/issues/249) `no stub serializer for kotlin.PACKAGE_DIRECTIVE` on warm start — **open**, exact match for our fatal crash.
- [#205](https://github.com/Kotlin/kotlin-lsp/issues/205) failed restarts orphan JVMs at 2.5–3.2 GB each — exact match for our PPID-1 orphans.
- [#148](https://github.com/Kotlin/kotlin-lsp/issues/148)/[#189](https://github.com/Kotlin/kotlin-lsp/issues/189) `initialize` blocks on the Gradle import; routinely exceeds client startup timeouts.
- [#178](https://github.com/Kotlin/kotlin-lsp/issues/178) **git worktrees of one repo corrupt the shared analyzer cache.** See §3.
- [#182](https://github.com/Kotlin/kotlin-lsp/issues/182) is JetBrains' own "Improve Kotlin LSP as an AI agent tool" thread; maintainer confirms pre-warming out-of-band is the only mitigation.

An experiment with `--multi-client` + `--client` on a socket connected but never returned an
`initialize` within 240s. **Undetermined** whether `--client` is a usable stdio↔socket proxy or
whether index contention caused it; not pursued further because §1.4 makes the shared-index
problem unavoidable regardless.

### 1.5 typescript-language-server's 60s timeout is its own, and unfixable there

The observed "references/definition time out at 60s while hover answers instantly" is not a
mystery and not a contention artifact alone:

- `typescript-language-server` has a **hardcoded, non-cancellable per-request timeout** —
  issues [#91](https://github.com/typescript-language-server/typescript-language-server/issues/91)
  and [#53](https://github.com/typescript-language-server/typescript-language-server/issues/53), both open.
- Hover needs one file's checker state; `references` needs the whole program, computed
  **synchronously on tsserver's single Node thread**. The asymmetry is architectural.
- v5.3.0, last published ~May 2026. The maintainers' own README says Microsoft's Go server
  "will hopefully supersede this project."
- `Debug Failure. False expression.` is a tsserver ScriptInfo/line-map desync bug
  (microsoft/TypeScript [#25844](https://github.com/Microsoft/TypeScript/issues/25844)), not
  fixable in the wrapper.

### 1.6 Everything fails open

A dead LSP falls back to grep, which still returns hits. A cold index answers "not found",
which reads exactly like absence — Claude Code's own built-in tool has this bug filed as
[#38011](https://github.com/anthropics/claude-code/issues/38011), closed as duplicate,
unresolved. There is **no LSP-standard ping**; editors infer liveness from process state, not
a protocol message.

---

## 2. Per-language verdicts

Researched independently per language, no assumptions carried over.

### The Swift win

sourcekit-lsp has two LSP extension requests that answer "cold or absent?" authoritatively:

- **`sourcekit/workspace/synchronize` with `{"index": true}`** — *blocks until background
  indexing has fully finished*. Upstream doc says it is "intended to be used in automated
  environments." This is exactly our case.
- `sourcekit/isIndexing` → `{indexing: bool}` — cheaper, but marked experimental, "do not rely on it."

**Rule: never trust a negative Swift symbol result until `synchronize` has returned.** This
eliminates the single worst failure mode — the confident false "not found" — for Swift
outright. Background indexing is on by default since Swift 6.1.

---

## 3. The worktree collision — a first-class finding

The worktree mandate and language-server indexing are in direct conflict, and nobody had
noticed. Every worktree is a **different absolute path**, so every worktree is a **different
workspace** to every language server:

### Decision: bind the language server to the MAIN checkout, never the worktree

`lsp-mcp.sh` currently takes `WS="$PWD"`. It should resolve `$PWD` through
`git rev-parse --path-format=absolute --git-common-dir` to find the **main** checkout and bind
there.

Rationale: symbol questions — *where is this defined, who calls this, what implements this* —
are questions about the codebase's structure, and the worktree's edits are a small delta
against it. Binding to the main checkout means **one warm index per project instead of one per
worktree**, and it survives worktree create/destroy entirely. The cost is that symbols defined
by *brand-new* code in the worktree are invisible until landed; that is a real limitation and
must be stated in the instruction, not hidden. Live `didOpen`/`didChange` from the worktree can
cover the file being edited.

---

## 4. Target architecture

```
                  ┌──────────────────────────────────────────┐
   session A ───► │  lspd  (one per (server, MAIN checkout))  │
   session B ───► │   • connect-or-spawn over a unix socket   │
   canary    ───► │   • refcounted; idle-timeout shutdown     │
   subagent  ───► │   • per-client: didOpen/didChange, req-id │
                  │   • shared:    the index                  │
                  │   • supervises the real server child      │
                  └────────────────┬─────────────────────────┘
                                   │ stdio
                       ┌───────────▼───────────┐
                       │  ONE language server  │
                       └───────────────────────┘
```

**Design reference is gopls `-remote=auto`** — 5+ years of production hardening on exactly this
pattern. Its state split is the one to copy: *per-client* = session/view, open buffers, unsaved
edits, request-ID namespace, cancellation; *shared* = the parsed/analyzed cache and index.
Get it wrong in one direction and one agent's uncommitted edits leak into another's view; wrong
in the other and you pay N× to rebuild the index.

**Build, don't adopt.** Nothing off the shelf fits:
- `lspmux`/`ra-multiplex` (Codeberg, EUPL-1.2) is the only maintained generic multiplexer, but
  is proven for rust-analyzer/clangd and **documents that it drops server-initiated requests**
  it cannot attribute — which would break `workspace/configuration` and `$/progress`, the two
  things we most need.
- Every MCP↔LSP bridge surveyed (isaacphi's incumbent, `mcpls`, Serena, the small forks)
  **spawns its own server**; none attach to an existing one. Swapping bridges changes nothing.
- gopls' forwarder is the right design but is Go-specific and not factored out as a library.

### Non-negotiable properties

1. **Connect-or-spawn**, lockfile-guarded against the spawn race.
2. **Refcount + idle timeout**, generous (tens of minutes) — the whole point is never paying
   Kotlin's cold Gradle import twice.
3. **Intercept `shutdown`/`exit` per client.** One session ending must never tear the shared
   server down. This inverts today's bug instead of patching it.
4. **Liveness = child-process exit / stdout EOF**, primary and free. An occasional cheap
   request only as a secondary "hung but alive" check. There is no protocol ping.
5. **Cold-vs-absent, per language, surfaced explicitly**: Swift → `sourcekit/workspace/synchronize`;
   others → track `$/progress` begin/end for indexing tokens. **A negative answer returned while
   indexing must be reported as `INDEXING`, never as "not found."**
6. **The probe uses the same socket the session uses.** A canary with its own process is
   measuring the wrong thing by construction (§1.3).
7. `lsp-guard.py`'s orphan reaping stays — but as a backstop, not the mechanism.

---

## 5. Sequenced plan

**Phase 2 — the daemon**
5. Write `lspd` (Python, in the store, projected like every other script): connect-or-spawn,
   refcount, per-client state, ID rewriting, `$/progress` tracking, idle shutdown.
6. `lsp-mcp.sh` becomes a thin client that attaches to it.
7. Rewrite `lsp-canary` to attach as an ordinary client — same socket, same path, real measurement.
8. Retire the parts of `lsp-guard.py` the daemon subsumes; keep orphan reaping as a backstop.

**Phase 4 — verification**
12. A test battery in the style of `test-require-worktree-edit-bash.py`: two concurrent clients
    on one workspace, a killed child, a cold index, a worktree switch. The current setup has
    **no test at all**, which is why every fix regressed.

---

## 6. What is still unknown

- Whether `kotlin-lsp --client` is a usable stdio↔socket proxy (my initialize timed out at 240s;
  cause undetermined between proxy semantics and index contention).
- Whether concurrent multi-process reads of one Swift indexstore-db/LMDB are officially safe —
  no upstream statement either way, no corruption reports found either.
- Whether `tsc --lsp` emits `$/progress` for project loading (TS7 LSP docs are thin at ~7 weeks
  post-GA). Must be tested empirically.
- Whether `permissions.deny: ["LSP"]` stops the *server process* or merely hides the tool.
  Probably only the latter — which is why Phase 0 disables the plugin instead.

---

## Swift is not fixed, and that is the correct outcome

`lspd.py --mcp` detects exactly this, refuses to "repair" it by rebinding (which cannot help),
and writes a health record carrying the one command that fixes it:

```
xcodebuild build -project Mobile-App-iOS.xcodeproj -scheme '<scheme>' \
  -destination 'generic/platform=iOS Simulator'
```

Deliberately not run unasked — it takes minutes and pins a CPU. **This is the one remaining
manual step.**

## Residual risks, stated plainly
