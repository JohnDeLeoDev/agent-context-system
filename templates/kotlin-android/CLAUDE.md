# CLAUDE.md — {{PROJECT}}

Stack-specific rules for this project. Universal workflow rules
(filesystem, notes, code discovery, delegation, workflow, git safety, etc.)
live in `~/.claude/CLAUDE.md`.

## Project Overview

{{PROJECT_DESCRIPTION}}

- **Stack:** Kotlin, Jetpack Compose, Room (KSP)
- **Build:** Gradle, AGP, compileSdk {{COMPILE_SDK}}, minSdk {{MIN_SDK}}
- **Package:** `{{PACKAGE}}`

Do not modify `gradle.properties`, `settings.gradle.kts`, or
`gradle/libs.versions.toml` without explicit instruction: these define the
build's modules, repositories and dependency versions.

## Architecture

<project-specific architecture notes go here — typical headings:
AppModel / managers, APIs, Database, Navigation, anything else load-bearing>

## Conventions

<project-specific conventions: DI framework choice, networking lib,
JSON lib, Compose state pattern, etc.>

## Patterns

<project-specific patterns: helpers, design tokens, adaptive layout, etc.>

## Build & Test

```bash
./gradlew assembleDebug                         # Debug APK
./gradlew assembleRelease                       # Release APK
./gradlew test                                  # Unit tests — target may be a no-op if app/src/test/ is empty
./gradlew testDebugUnitTest --tests "<class>"   # once tests exist
./gradlew lint                                  # Lint check
```

Run a test command **once**, capture everything, then analyze before fixing.

## Feature catalog

`.agents/docs/features/` (if you maintain one) — per-feature docs.
