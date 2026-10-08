---
uuid: "69883577-b24c-5cad-a4b4-8c4514e44731"
type: "skill"
name: "test-first-delivery"
description: "Use when user or a project rule selects test-first delivery: agreed criteria, failing-test proof, locked tests, independent review."
---
# Test-first delivery

This optional high-assurance workflow ends when agreed criteria and selected checks pass and an independent review has resolved its findings.

A test covers only what someone thought of. This procedure does not promise zero
bugs. It makes every claim of "done" checkable, and every remaining gap visible.

## When to use

Use when user requests test-first delivery, an applicable project/branch requirement calls for it, or the lead explicitly selects high-assurance test-first work for the task. Ordinary work follows Global Agent Instructions, Verification. Docs and config do not activate this procedure.

## 0. Check the premise

Confirm every function, file, flag, API and service the request names exists. If one
does not, say so and stop. Do not create it to make the request true.

## 1. Write the acceptance criteria

One numbered list. Each criterion is an observable behavior a test can pass or fail
on. Cover, in order:

1. The normal path.
2. Error paths: bad input, missing data, a dependency that fails or times out.
3. Edge inputs: empty, one, many, boundary values, and concurrency where it applies.
4. What must not change: behavior next to the change, public interfaces, output formats.
5. Every sibling site the change applies to. Find them with LSP. A fix applied at one
   site of several is not done.
6. Out of scope, named, so nobody tests or builds it.
7. Not verifiable here, named, with the check that would verify it: a device run, a
   deploy, user's review.

Give each criterion its verification:

| Kind | Verified by |
|---|---|
| Logic | a unit or integration test in the project's own framework |
| UI | the project's UI or screenshot test, or the `run` / `device-interaction` skill with a screenshot |
| CLI or script | running it, exit code and output quoted |
| Service | a request against a local instance, response quoted |
| Cannot run here | the "not verifiable here" list |

Name each assumption in the criteria that you did not verify.

## 2. Get approval (non-trivial tasks)

Show user the criteria and verification table. Ask through `AskUserQuestion`:
approve, or change. Write no code before he approves.

## 3. Write the tests and watch them fail

1. Write the tests in the worktree, in the project's existing framework and layout.
2. Run them. Each new test must fail on an assertion about the missing behavior. An
   import error, a typo or a missing fixture is not a failure yet: fix the test and
   run again.
3. A new test that passes before any implementation cannot detect the change. Rewrite it.
4. A "must not change" test passes now. Prove it can fail: break that behavior in the
   worktree, run the test, watch it fail, then Edit the code back.
5. Keep the failing output. The report quotes it.

## 4. Lock the tests

    python3 ~/.agent-context/global/scripts/test-lock.py lock <test file>...

A .py file is typechecked with basedpyright first; an error refuses that file.

Commit the tests in the worktree. From here on:

- `block-locked-test-edit` refuses edits to a locked file.
- `locked-test-drift-gate` refuses to end a turn while a locked file differs from its lock.
- Only user's explicit approval authorizes an unlock. If a locked test is wrong,
  stop, name the assertion and why. In Claude Code, ask with AskUserQuestion in
  the approval shape (the refusal prints it); `approval-question` removes the
  lock when he picks Approve. In Codex, use `request_user_input_async` with
  one exact approval question per call and options `Approve`, then `Deny`.
  In opencode 2.x, use the `question` tool: one call holding only that question,
  header `Approval`, options `Approve`, then `Deny`; `opencode-approval` removes
  the lock and adds the outcome to the tool result.
  Check `test-lock.py status` before
  editing; if the lock remains, report that the grant failed. Never run a consent
  script yourself or hand user a command to type.

`python3 ~/.agent-context/global/scripts/test-lock.py status <checkout>` lists the locks. Locks are
per checkout: lock, implement and verify in the same worktree. The one exception is the
agent-context store: a lock on a store test is a record committed in the store
(`global/test-locks/`), so it binds on every host and an approved unlock anywhere lifts it.

## 5. Implement

Change the code until the locked tests pass.

- Never special-case a test input. Never mock the code under test. A mock stands in
  for a dependency outside the unit.
- The same failure after three fix attempts: stop. Report the failing output, the three
  attempts, and the assumption that might be wrong. Ask user.

## 6. Run selected checks

Run the checks assigned in the approved brief. Select full suites, build, lint and type checks under Global Agent Instructions, Verification. Reuse matching receipts. A failure returns to step 5 and invalidates affected evidence.

## 7. Try to break it

Use a fresh reviewer on the diff under the current harness model and effort hierarchy. The brief carries:

- the approved criteria,
- the selected check commands, owners and matching receipts,
- "Find inputs or states that break this change. Reuse matching checks and run targeted reproductions for concrete gaps. Report each break as
  the input and the observed output. Refute this brief before building on it. Name
  each claim you did not verify."

Reproduce every finding yourself. A real one becomes a new test: write it, watch it
fail, lock it, return to step 5. Report a finding you cannot reproduce as unreproduced.

## 8. Report

One row per criterion:

| # | Criterion | Verified by | Result |
|---|---|---|---|

- Result quotes the command and its key output line, for example "`pytest -q`: 42 passed".
- Then list: tests seen failing before implementation, each review finding and its
  outcome, and the "not verifiable here" items.
- No row reads as passing without output behind it.

The lead lands through the project's finish script within the authorized scope. Checkout-local locks end with worktree removal. Store test locks remain until an approved unlock.
