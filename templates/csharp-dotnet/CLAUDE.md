# CLAUDE.md — {{PROJECT}}

Stack-specific rules for this project. Universal workflow rules
(filesystem, notes, code discovery, delegation, workflow, git safety, etc.)
live in `~/.claude/CLAUDE.md`.

## Project Overview

{{PROJECT_DESCRIPTION}}

- **Stack:** C# / .NET
- **Solution:** `{{SOLUTION}}.sln`
- **Target framework:** {{TARGET_FRAMEWORK}}

## Architecture

<project-specific architecture notes go here — typical headings:
projects/csprojs, layers, key services, data access, anything else
load-bearing>

## Conventions

<project-specific conventions: DI container, ORM, logging,
serialization, async patterns, etc.>

## Patterns

<project-specific patterns: helpers, base classes, key abstractions, etc.>

## Build & Test

```bash
dotnet build                                # build the solution
dotnet test                                 # run all tests
dotnet format {{SOLUTION}}.sln              # apply formatting + Roslyn fixes
```

The `post-edit-format.py` PostToolUse hook auto-runs `dotnet format` on
edited `.cs` files (severity: info), so most formatting drift is handled
automatically.

Run a test command **once**, capture everything, then analyze before fixing.

## Feature catalog

`.agents/docs/features/` (if you maintain one) — per-feature docs.
