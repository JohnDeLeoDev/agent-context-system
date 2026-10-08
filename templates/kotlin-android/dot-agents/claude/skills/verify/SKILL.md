---
name: verify
description: Run verification loop after changes — build, lint, and diff review.
---

Run these verification steps in order:

1. **Build** — `./gradlew assembleDebug` (must pass with zero errors)
2. **Lint** — `./gradlew lint` (check for new warnings)
3. **Diff review** — `git diff` to verify only intended changes were made
4. **No regressions** — Confirm no files were accidentally modified

Report results concisely:

- Build: PASS/FAIL (error count if failed)
- Lint: PASS/WARN (new warning count)
- Diff: List of changed files with line counts
- Verdict: READY / NEEDS FIX
