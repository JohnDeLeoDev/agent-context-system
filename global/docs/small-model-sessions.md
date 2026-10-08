# Small-model and compaction sessions

Load this when the session runs a small local model (qwen3.x, local 27B), or when
context is tight and compaction is near. Normal Claude sessions do not need it.

## Small-model sessions (qwen3.x, local 27B)

Fixed context budget (90,000 tokens), no compaction safety net. Estimate your
bootstrap cost from the context already loaded (system prompt, instructions,
memories). Working budget = 90,000 minus that estimate minus 9,000 margin. Plan
exactly one chunk that fits. Do not pull in a second. When the chunk is done and
verified, run `/handoff` and stop. If context reaches 75% of the window
before completion, hand off with partial state. Do not continue. Compaction is the
fallback when a chunk is not done but context is tight: run `/compact` with the
task state, then continue.

## Larger models (Claude, etc.)

Compaction carries the session. Do not `/clear`; compact instead. Run `/compact`
with a 1-3 sentence instruction telling the compactor what to keep: modified files,
test commands, task state, decisions made.

## When context is tight

Protect the task state and the compaction instruction first; that is what the next
segment of the session runs on.
