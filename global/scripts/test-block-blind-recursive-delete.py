#!/usr/bin/env python3
'test-block-blind-recursive-delete — deny/allow cases for block-blind-recursive-delete.\n\nThe shared cases live in hook-test-cases.py and run through hook-test-run.py, like every\nother hook. This battery adds the two cases that runner cannot express, because both turn\non FILESYSTEM STATE the payload alone cannot carry:\n\n  1. the SAME path, allowed while empty and refused once it has contents -- the empty-dir\n     exemption is the one that keeps this guard quiet enough to survive, so it has to be\n     tested against a real directory rather than asserted;\n  2. a scratch delete that is allowed but must PRINT its inventory. That itemization is\n     the half of context inventory O9 that makes a false "the directory was empty" report\n     impossible, and a test that only checked the exit status would pass while it was\n     silently dropped.'
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.expanduser("~/.agent-context/global/hooks/block-blind-recursive-delete.py")
fails = 0

proc = subprocess.run(
    [sys.executable, os.path.join(HERE, "hook-test-run.py"),
     "--hook", "block-blind-recursive-delete.py"] + sys.argv[1:])
if proc.returncode != 0:
    fails = 1

if not os.path.isfile(HOOK):
    print("  FAIL  hook not deployed at %s" % HOOK)
    sys.exit(1)

TMP = tempfile.mkdtemp()


BASE = os.path.expanduser("~/.cache/test-blind-delete-%d" % os.getpid())


def cleanup():
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(BASE, ignore_errors=True)


try:
    os.makedirs(os.path.join(BASE, "subject"), exist_ok=True)

    def ask(cwd, command):
        payload = json.dumps({"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}})
        proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True)
        return proc.stdout or ""

    def check(label, expect, output):
        global fails
        if expect == "deny":
            if '"deny"' in output:
                print("  ok   %s" % label)
            else:
                print("  FAIL %s -- expected deny, got: %s" % (label, output or "<silence>"))
                fails = 1
        elif expect == "allow":
            if output == "" or "systemMessage" in output:
                print("  ok   %s" % label)
            else:
                print("  FAIL %s -- expected allow, got: %s" % (label, output))
                fails = 1
        elif expect == "itemize":
            if "systemMessage" in output:
                print("  ok   %s" % label)
            else:
                print("  FAIL %s -- expected an inventory, got: %s" % (label, output or "<silence>"))
                fails = 1

    print("  stateful cases:")

    check("an EMPTY directory is deleted without comment", "allow",
          ask(BASE, "rm -rf %s/subject" % BASE))

    with open(os.path.join(BASE, "subject", "ledger.md"), "w") as fh:
        fh.write("content\n")
    out = ask(BASE, "rm -rf %s/subject" % BASE)
    check("the SAME directory is refused once it has contents", "deny", out)

    if "ledger.md" in out:
        print("  ok   the refusal names what would have been lost")
    else:
        print("  FAIL the refusal did not list the contents")
        fails = 1

    
    
    ledger_dir = os.path.join(TMP, "ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    open(os.path.join(ledger_dir, "row-0001.json"), "w").close()
    open(os.path.join(ledger_dir, "row-0002.json"), "w").close()
    out = ask(TMP, "rm -rf %s" % ledger_dir)
    check("a scratch delete is allowed but itemized", "itemize", out)
    if "row-0001.json" in out:
        print("  ok   the inventory names the entries, so 'it was empty' cannot stand")
    else:
        print("  FAIL the inventory did not name the entries")
        fails = 1
finally:
    cleanup()

if fails == 0:
    print("ALL PASS")
else:
    print("FAILURES")
sys.exit(fails)
