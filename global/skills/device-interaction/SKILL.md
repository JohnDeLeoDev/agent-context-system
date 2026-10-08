---
uuid: "2f5ea468-8456-527d-b657-e4827687c05c"
type: "skill"
name: "device-interaction"
description: "Verify iOS app behavior on an Apple device or Simulator through Xcode tools. For Android, use adb or project device skills."
---
# Device Interaction

Exported from Xcode 27 (`xcrun mcpbridge run-agent skills export`) and stored here so
subagents can load it. Re-export after an Xcode upgrade to pick up new commands. The
store copy has its punctuation adapted to the fleet's plain-language rules.

TRIGGER when: user asks to verify/test/check if the app works on device, after implementing a UI-affecting feature that needs device verification, user says "does it work", "test this", "check on device", user reports UI doesn't work as expected, need to debug touch/interaction issues.
Do not trigger when: user asks about unit tests only, build-only requests without device testing, code review without device testing, simulator configuration questions, changes that don't affect UI (e.g. comments, refactors, non-UI logic).

---

# For the Main Agent

**This is a SUBAGENT skill.** Invoke it via the Agent tool when device verification is needed. If there is an open session for that work, provide that session identifier to a subagent for exclusive use by that subagent.

```
Agent tool:
- subagent_type: "worker-device"  (never a harness catch-all like `general-purpose`
  or `claude`, which are not registered agent types outside Claude Code)
- model: <one tier below the orchestrator, e.g. "sonnet" when the session is on opus>
- description: "Verify login feature works"
- prompt: "Using the device-interaction skill, verify that the login feature works correctly on session <session-identifier>. Launch the app, capture screenshot and UI hierarchy, check that the login button is visible and tappable, and report if the implementation is working correctly."
```

**Pass the `model` explicitly.** This is a worker, and the global *Model tiering*
rule says workers never inherit or escalate to the orchestrator's tier. Use `sonnet` (the
floor: never `haiku`), or `opus` when the orchestrator runs above it. Address it by alias,
never a versioned id. Do not use a `fork` subagent for this: forks
always inherit the orchestrator's model and cannot be downgraded.

**After implementing a UI-affecting feature, invoke this skill to verify the implementation works on a device.**

## Session Lifecycle

```
DeviceInteractionStartWorkspaceSession (workspace-backed; do this early, runs in the background)
  → DeviceInteractionInstallAndRun (after each code change; includes building)
    → DeviceEventSynthesize (interact + observe, repeatable)
  → DeviceInteractionEndSession (when done: keeping sessions open is resource-heavy)
```

There are two ways to start a session:
- **DeviceInteractionStartWorkspaceSession**: bound to a workspace. Required for DeviceInteractionInstallAndRun. Use this when verifying the app you are building.
- **DeviceInteractionStartSession**: not bound to any workspace. Use this only when interacting with an already-installed app.

## DeviceInteraction(Workspace)StartSession tools

### Device Discovery

When opening a new device interaction session, pass a device identifier to select a device, or omit it to use the current destination. Pass an empty string to get a list of available targets.

## DeviceInteractionInstallAndRun tool

### Optional Parameters

- `commandLineArguments`: arguments passed to the app at launch. Use `$(inherited)` as a token to preserve the scheme's existing arguments (e.g. `["$(inherited)", "--reset-state"]` to add an extra argument at the end).
- `environmentVariables`: key/value pairs set in the app's environment at launch. Use `"$(inherited)"` as a key to preserve the scheme's existing environment variables (e.g. `{"$(inherited)": "", "DEBUG_MODE": "1"}`).

Omit both parameters to leave the scheme's arguments and environment unchanged.

**Prefer these parameters over editing the scheme directly.** They are applied only for that one run and have no lasting effect on the user's configuration.

---

# For the Subagent

**Always** report UI issues that might be caused by code: overlapping or unreadable text, unexpectedly cropped image/text, wrong colors etc.

## DeviceEventSynthesize tool

This tool allows performing an interaction and observing the state of a device.

## Reading Hierarchy Files

The hierarchy files include calculated hitPoint positions for each element:

```
UIView {{100, 200}, {60, 30}}, hitPoint: {130.0, 215.0}
  UIButton "Login" {{110, 205}, {30, 20}}, hitPoint: {125.0, 215.0}
  UIButton "Login2" {{140, 205}, {20, 20}}, hitPoint: {150.0, 215.0}, activationBundleId: com.your.app
```

- `{100, 200}` - origin position
- `{50, 30}` - width and height
- `hitPoint: {125.0, 215.0}` - calculated hitPoint point (best for tapping)

**Always prefer the hitPoint coordinates for touch events.** Only after tapping a hitPoint and confirming (via recapture) it had no effect may you fall back to a raw screenshot-estimated position.

Warning: Elements marked `isRemoteLeafPlaceholder` do not report child elements; interacting with them requires falling back to screenshot-estimated coordinates.

### Multiple Applications (activationBundleId annotations)

When windows from more than one application overlap on screen, each element line is annotated with a `activationBundleId: <bundle-identifier>` suffix.

If an element line carries a `activationBundleId`, **any interaction with it requires activating that application first** by passing `activationBundleId` parameter to the DeviceEventSynthesize tool.

Tip: you can pass `activationBundleId` with no `interactionCommand`.
**Application activation is expensive so use that only if necessary.**

## Interaction Command Syntax

The `interactionCommand` parameter accepts a command syntax:

| Command | Description |
|---|---|
| `t <x> <y> [duration]` | Tap at coordinates with optional hold duration |
| `d <x> <y>` | Double tap |
| `t <x1> <y1> f <x2> <y2> [duration]` | Swipe from (x1,y1) to (x2,y2) |
| `drag <x1> <y1> <x2> <y2> [holdDuration] [moveDuration]` | Drag-and-drop: press-and-hold at (x1,y1) then slowly move to (x2,y2). Use for reordering lists or drag-and-drop targets |
| `mt [x1 y1, ...] dur [x1 y1, ...] dur ...` | Multi-touch sequence. Each `[...]` keyframe lists touch positions (`x y`). The duration after each block is the travel time to the next keyframe; for the last block it is the hold time before lifting. A finger ends when its slot is an empty comma entry (e.g. `[, x y]`) **or** when the frame has fewer entries than the finger's index; both are equivalent. A finger that reappears in a later keyframe after lifting starts a new tap. |
| `b h/p/u/d [duration]` | Hardware button: h=Home, p=Power, u=VolUp, d=VolDown |
| `b c/s/a [duration]` | **watchOS only.** c=Digital Crown press, s=side button, a=Action button (some devices only) |
| `sender keyboard kbd <text>` | Type text; **must be the last command in the chain**: all content after `kbd ` is taken verbatim (multiple spaces preserved). For special characters use `\u{XXXX}` Unicode escapes: `\u{000A}` (return/newline), `\u{0009}` (tab) |
| `w duration` | Wait for a duration without any work |
| `orientation faceDown/faceUp/landscapeLeft/landscapeRight/portrait/portraitUpsideDown` | Set device orientation (iOS only) |
| `c <rotations>` | **watchOS only.** Rotate the Digital Crown; sign sets direction, 1.0 = one full revolution |

**Examples:**
- `"t 100 200"` - Tap at (100, 200)
- `"d 200 300"` - Double tap at (200, 300)
- `"t 200 600 f 200 200 0.3"` - Swipe up (scroll to the content below)
- `"t 200 200 f 200 600 0.3"` - Swipe down (scroll to the content above)
- `"drag 100 300 100 100"` - Drag-and-drop from (100,300) to (100,100) with default durations
- `"drag 100 300 100 100 0.5 1.5"` - Drag-and-drop with 0.5s hold, 1.5s move
- `"mt [100 200] 0.5 [100 200] 1.0 [300 400] 0.2"` - Drag: hold at (100,200) for 0.5s, move to (300,400) over 1.0s, hold 0.2s then lift
- `"mt [100 300, 300 300] 0.5 [175 300, 225 300] 0.5"` - Two-finger pinch: both fingers start 200px apart and move toward each other
- `"b h"` - Press home button
- `"b h b h"` - Press home button twice to go to the app switcher
- `"b h w 0 b h"` - Wake and unlock a device (non-passcode devices only)
- `"b c"` - watchOS: press the Digital Crown
- `"b s"` - watchOS: press the side button
- `"b a"` - watchOS: press the Action button (some devices only)
- `"sender keyboard kbd hello world"` - Type text with spaces
- `"sender keyboard kbd hello   world"` - Type text preserving multiple spaces
- `"sender keyboard kbd submit\u{000A}"` - Type text then press Return/submit
- `"w 0.3"` - Wait for 0.3s
- `"orientation landscapeLeft"` - Rotate device to landscape
- `"c 1.0"` - watchOS: rotate the Digital Crown one full turn (e.g. scroll a list)
- `"c -0.5"` - watchOS: rotate the crown half a turn the other way

## Standard Subagent Workflow

Before any interaction, always capture and read the hierarchy (and screenshot). After any interaction, capture again and verify the result. For complex components (like toggles or switches), look at nested elements (like `Switch` or `Slider`): nearby elements might correspond to the control itself. When done, report findings to the main agent.

- To capture without interacting, use DeviceEventSynthesize with an empty interactionCommand.
- Never guess positions from screenshots alone: use hierarchy hitPoint coordinates; screenshot estimation is only a fallback after a hitPoint is tried and fails.
- If not confident or thumbnail resolution is insufficient, analyze the full-size screenshot.

## Timing and Retries

## Judging Success vs Failure

When verifying, distinguish between these categories:

- **Functional bug** (always report): element doesn't respond to tap, navigation goes to wrong screen, crash, data not displayed, missing expected UI element.
- **Visual/layout bug** (always report): overlapping text, truncated labels, elements rendered off-screen, wrong colors, broken alignment.
- **Transient state** (do not report as bug): loading spinners, brief animations, keyboard appearing/dismissing. Capture again after the transition completes.
- **Unexpected exits** (always report): crashes, application exits. To identify, track process id and capture process's standard output.
- **Expected behavior** (do not report as bug): empty states with placeholder text, disabled buttons when form is incomplete, permission dialogs.

## Error Handling

- If application is not visible, retry once, as this might be caused by a slow device.
- If tap target unclear, re-read hierarchy data for correct hitPoint coordinates.
- You can inspect runtime logs to troubleshoot. If you suspect timing bugs, suggest to the main agent that temporarily adding `print` statements in the relevant code may help diagnose the issue.
- Report issues back to the main agent with details and suggestions.

## Local note (this fleet)

The tool this harness exposes is named `mcp__xcode__DeviceInteractionSynthesize`, not
`DeviceEventSynthesize`; the command syntax above is unchanged. Capture with an empty
`interactionCommand`.
