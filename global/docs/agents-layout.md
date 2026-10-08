# Agent layout

Read [[agent-context.md]] for the system's rules, structure and decisions. Its Rules and Structure sections own storage and projection policy.

Read and edit store entities through MCP. Files under a project's `.agents/` and `.claude/`, and the installed home harness directories, are generated inputs for harness loaders. They are not an authoring location. A mismatch is a materialization defect; correct the store source or the materializer.

Keep durable agent context and ledgers in store docs. Scratch and harness-owned runtime files follow [[agent-context.md]]. For `.agents/worktrees/` and existing `.claude/worktrees/` entries, follow [[worktrees.md]]. Do not treat generated directories as durable storage.

For a registered project, `python3 ~/.agent-context/global/scripts/project-materialize.py --check <repo>` reports projection drift without repairing it. Exit 3 means drift. The store checkout is not a registered project, so this check does not verify its home harness projections.
