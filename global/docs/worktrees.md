# Worktree landing workflow (generic)

Universal mechanics for the worktree mandate. **Project-specific overrides
win:** when `<project>/.agents/docs/worktrees.md` exists, follow it for
base-ref, build-gate, finish script, integration branch, and push target. This
file is the fallback for projects without their own runbook.

## When it applies

Every non-exempt source change. **Exempt:** doc-only edits and `.claude/`-only
edits — make those directly in the main checkout. Everything else
(app / library / test source) goes through a worktree.

## Lifecycle

1. **Start** — create a worktree before editing. Use whichever mechanism your
   agent provides (built-in worktree tools like `EnterWorktree`, or raw
   `git worktree add`). The worktree lives under `.agents/worktrees/<name>/`
   (gitignored). `worktree.baseRef` is managed fleet-wide as `"head"` (by
   `home-settings-sync.py`), so `EnterWorktree` branches from the main
   checkout's **current HEAD** — the commit the previous landing just produced —
   not from a possibly-stale `origin/<default>`. Ensure the main checkout is on
   the integration branch first. If a worktree still comes up without a change
   you just landed, `git merge --ff-only main` inside it.
2. **Work** — edit directly (LSP + Edit/Write), build, and commit on the
   worktree's own branch as normal. When delegating, the parent creates the
   worktree first; subagents work inside it and never create their own.
   **Every edit must target the worktree path, not the main checkout.**
   Edit/Write/Read take *absolute* paths, and an absolute path into the main
   checkout (`/…/<project>/src/…`) writes there silently — outside the worktree,
   defeating isolation, and your in-worktree lint/build then validates the
   *unchanged* tree (a green build that proves nothing). Capture the worktree
   root from the creation result and prefix every file target with it;
   re-root any path an earlier search or main-checkout step reported.
   **Invariant: every write target contains `/.agents/worktrees/` (or `/.claude/worktrees/` for an existing worktree)** — if it
   doesn't, you're corrupting the main checkout; stop and re-root.
   **Multi-line edits: use Edit/Write, not a Bash heredoc.** In a
   worktree-isolated session the harness's own isolation guard refuses
   `python3 - <<PY` style scripts as "too complex to verify that it stays inside
   the worktree" (the store's hooks are not involved and cannot lift it) —
   switching tools first saves two failed calls.
3. **Gate** — run the checks selected by Global Agent Instructions, Verification and applicable project or branch rules. On the shared landing core, pass repeatable `--check=<gate-name>` options from the project's check list. Selection includes declared prerequisites and required controls. `--check=all` selects every gate; `--check=none` requires a non-build relevance proof. Omitted selection keeps the project's declared defaults. Reuse matching existing receipts; a running check remains pending. Never land with a required check failing.
4. **Land** — `merge --no-ff` the worktree branch into the integration branch
   (the branch the worktree was based on) in the main checkout, then push to the
   project's remote if one exists. Prefer the project's finish script when
   present — it enforces clean-tree + gate + merge + push atomically.
5. **Clean up** — remove the worktree once landed (via your agent's removal
   mechanism or `git worktree remove`).

## The agent-context store itself

`require-worktree-edit` (and its Bash door) enforce the worktree rule for
`~/.agent-context/server/` as for any project; a refusal names the fix.
Store entities are a different rule (MCP tools, never files).

Two more rules:
- **The store is edited from the store.** A session whose working directory is a
  project may not write any file under `~/.agent-context/` by hand, worktree or not.
  From a project, the MCP tools are the only route into the store.
- **No manual git writes in the store's main checkout.** `guard-git-write` enforces this.
  Commit inside the worktree, then land with `store-wt-finish.py` from outside it,
  then confirm with `store-landed.py`. A fast-forward by hand skips the gate.
- Edits left uncommitted under `server/` in the main checkout are parked by the daemon
  onto a `parked/<host>-<stamp>` branch after ~20 idle minutes, and main is restored.

Address the worktree path yourself. As a net, once a session has a worktree for a repo
the hook moves an edit aimed at the main checkout into it, or refuses and names the
worktree path to read first.

**Land from outside the worktree.** The harness's worktree-isolation guard refuses
`git -C <main-checkout>`, `cd`-prefixed compounds and `HOME=` assignments from a
session inside a worktree. ExitWorktree(keep) first, then run the finish script from
the main checkout, then remove the worktree.

## Gotchas

**Do commit as you go.** The failure this protects against is the current Opus-class
scope-inflation mode: a narrow request that turns into an unrequested rewrite. The
converged defense practitioners report for it is frequent commits — rollback points the
model cannot reach past — not model trust and not review discipline. Without them a
worktree is all-or-nothing: the only restore point is the worktree boundary, so undoing
twenty minutes of drift costs the whole worktree including the parts that were right.

Practical shape:

- Commit at each point where the work is coherent, even mid-task. `wt-finish.sh` rebases
  the branch onto `main` and fast-forwards it, so intermediate commits cost nothing at
  landing time — they do not produce merge commits and they do not need squashing first.
- A commit inside a worktree is not a publish. Nothing is reachable from `origin` until
  `wt-finish.sh` runs, so the "pushes only when explicitly requested" rule is untouched.
- `git reset --hard` is denied fleet-wide, so recovery is `git revert`, `git checkout
  <sha> -- <path>` on a specific file, or landing a smaller subset. Plan the commits so
  that is possible.

Claude Code's `/rewind` checkpointing is off on this fleet
(`fileCheckpointingEnabled: false`) and tracks only edits made through the file tools,
so worktree commits are the only rollback.
