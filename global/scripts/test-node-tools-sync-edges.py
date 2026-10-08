#!/usr/bin/env python3
'Edge cases for node-tools-sync.py, from the Consolidation Phase 6a adversarial review.\n\nThe review found that a bin link made by an earlier sync for a DIFFERENT root was refused\nwith "node-tools-sync did not link it", which is false. The refusal itself is right (the\nlink belongs to another install and must not be silently repointed); the message has to\nsay what the file really is so the operator can act on it.\n\nEach case refuses before pnpm resolves anything, so no install runs. pnpm must be on\nPATH, because the script checks for it first. Writes only inside a temp dir.'

import os
import shutil
import subprocess
import sys
import tempfile

STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.expanduser("~/.agent-context")
SYNC = os.path.join(STORE, "global", "scripts", "node-tools-sync.py")
SOURCE = os.path.join(STORE, "global", "node-tools")

tmp = tempfile.mkdtemp(prefix="node-tools-sync-edges-")
passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def sync(*args):
    print("    running node-tools-sync.py %s" % " ".join(args), flush=True)
    r = subprocess.run([sys.executable, SYNC] + list(args), capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    return r.returncode, r.stdout, r.stderr


root = os.path.join(tmp, "share", "node-tools")
other_root = os.path.join(tmp, "elsewhere", "node-tools")


print("[1] a link this script made for another root")
bin1 = os.path.join(tmp, "bin1")
os.makedirs(bin1)
link = os.path.join(bin1, "copilot-api")
other_target = os.path.join(other_root, "node_modules", ".bin", "copilot-api")
os.symlink(other_target, link)
rc, out, err = sync("--source", SOURCE, "--root", root, "--bin-dir", bin1)
check("it is refused", os.path.isfile(SYNC) and rc not in (0, 127), "rc=%s err=%r" % (rc, err[-300:]))
check("the refusal names the link and the root it points into", link in err and other_target in err, repr(err[-300:]))
check("the refusal does not claim node-tools-sync never linked it", "did not link" not in err, repr(err[-300:]))
check("the link is left pointing where it did", os.path.islink(link) and os.readlink(link) == other_target)


print("[2] a plain file")
bin2 = os.path.join(tmp, "bin2")
os.makedirs(bin2)
plain = os.path.join(bin2, "tsgo")
with open(plain, "w", encoding="utf-8") as fh:
    fh.write("#!/bin/sh\necho mine\n")
rc, out, err = sync("--source", SOURCE, "--root", root, "--bin-dir", bin2)
check("a plain file is refused, named, and called out as not a link",
      os.path.isfile(SYNC) and rc not in (0, 127) and plain in err and "not a symlink" in err, repr(err[-300:]))

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
