---
name: verify
description: Run verification loop after changes — build, test, and diff review.
---

Run these verification steps in order:

1. **Build** — `dotnet build` (must pass with zero errors)
2. **Test** — `dotnet test` (note any regressions)
3. **Diff review** — `git diff` to verify only intended changes were made
4. **No regressions** — Confirm no files were accidentally modified

Report results concisely:

- Build: PASS/FAIL (error count if failed)
- Test: PASS/FAIL (failed test count if any)
- Diff: List of changed files with line counts
- Verdict: READY / NEEDS FIX
