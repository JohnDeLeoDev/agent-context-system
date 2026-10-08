#!/usr/bin/env python3
'test-ralph-intervals.'
import importlib.util
from pathlib import Path
import subprocess
import tempfile

STORE = Path.home() / ".agent-context"
HOOK = Path(__file__).resolve().parent.parent / "hooks" / "ralph-patch-guard.py"

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

new = load("ralph_patch_current", HOOK)
old_source = subprocess.run(
    ["git", "-C", str(STORE), "show",
     "88b028f9:global/hooks/ralph-patch-guard.py"],
    capture_output=True, text=True, check=True).stdout
scratch = Path.home() / ".cache" / "tmp"
scratch.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="ralph-interval-", dir=scratch) as tmp:
    old_path = Path(tmp) / "old_guard.py"
    old_path.write_text(old_source)
    old = load("ralph_patch_old", old_path)
    anchor = chr(10).join([
        '# Output JSON to block the stop and feed prompt back',
        '# The "reason" field contains the prompt that will be sent back to Claude',
        'jq -n ' + chr(92),
        '  --arg prompt "$PROMPT_TEXT" ' + chr(92),
    ])
    older, _, _ = old.apply_patches(anchor)
    current, _, _ = new.apply_patches(older)
    assert new.MARKER_B in current, "new B marker absent"
    assert "FULL_EVERY=$((10#$FULL_EVERY))" in current, "decimal conversion absent"
    assert "LOCAL PATCH B (agent-context, re-feed policy)" not in current

for raw in ("00", "08", "09", "0008"):
    proc = subprocess.run(
        ["bash", "-c",
         'FULL_EVERY="$1"; FULL_EVERY=$((10#$FULL_EVERY)); printf "%s" "$FULL_EVERY"',
         "interval", raw], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout == str(int(raw)), (raw, proc.stderr)
print("5 passed, 0 failed")
