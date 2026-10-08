#!/usr/bin/env python3
'Tests for plain-language-check.\n\nThe must-PASS cases matter more than the must-deny ones. A guard that fires on honest\nwriting gets turned off, and two specific false positives would each make this rule\nactively harmful:\n  - flagging a descriptive identifier, which user asked for explicitly\n  - flagging a precise technical term, which is shorter than its explanation\n\nTwo real bugs were caught here and both were silent. The prose extractor used BRE\nalternation, which macOS sed does not support, and then used | as the sed delimiter in a\npattern full of | alternations. Each made comment scanning a no-op while markdown kept\nworking, so the hook looked fine.'
import os
import subprocess
import sys

HOOK = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/.agent-context/global/hooks/plain-language-check.py")
if not os.path.isfile(HOOK):
    print("no hook at %s" % HOOK)
    sys.exit(1)

pass_n = 0
fail_n = 0


def check(want, name, payload):
    'want: deny | warn | pass'
    global pass_n, fail_n
    proc = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True)
    out = proc.stdout or ""
    if not out.strip():
        got = "pass"
    elif '"permissionDecision": "deny"' in out:
        got = "deny"
    else:
        got = "warn"
    if got == want:
        pass_n += 1
        print("ok   %-38s (%s)" % (name, got))
    else:
        fail_n += 1
        print("FAIL %-38s want=%s got=%s" % (name, want, got))



check("deny", "markdown jargon",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.md","content":"A robust solution that will leverage the cache."}}')
check("deny", "markdown british",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.md","content":"Normalise the colour before you initialise."}}')
check("deny", "swift comment jargon",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.swift","content":"// This leverages a robust cache\\nfunc f() {}"}}')
check("deny", "kotlin comment british (Edit)",
      '{"tool_name":"Edit","tool_input":{"file_path":"/x.kt","new_string":"// normalise the colour first\\nval x = 1"}}')
check("deny", "python # comment",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.py","content":"# a comprehensive helper\\ndef f(): pass"}}')
check("deny", "trailing // comment",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.js","content":"const a = 1; // utilize the cache"}}')
check("deny", "commit message",
      '{"tool_name":"Bash","tool_input":{"command":"git commit -m \\"initialise the analyser\\""}}')
check("deny", "store memory body",
      '{"tool_name":"mcp__agent-context__upsert_memory","tool_input":{"slug":"x","body":"Prefer the colour token.","description":"d"}}')


check("pass", "descriptive identifier",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.swift","content":"func refreshRemoteInventoryCacheOnColourChange() { let robustValue = 1 }"}}')
check("pass", "technical vocabulary",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.md","content":"The write is idempotent. Normal form is computed once; semantics match the LSP spec."}}')
check("pass", "url is not a comment",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.js","content":"const u = \\"https://x.com/robust\\";"}}')
check("pass", "backticked code span",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.md","content":"Upstream spells it `initialise` and the flag is `--robust`."}}')
check("pass", "fenced block",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.md","content":"Example:\\n```\\nleverage robust colour\\n```\\nThat is the output."}}')
check("pass", "non-commit shell",
      '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}')
check("pass", "self-edit exemption",
      '{"tool_name":"mcp__agent-context__edit_body","tool_input":{"key":"plain-language-check","new_string":"robust colour leverage"}}')




check("pass", "edit_body script string literal",
      '{"tool_name":"mcp__agent-context__edit_body","tool_input":{"kind":"script","key":"x-cases","new_string":"_say(\\"Let me check the lockfile.\\")"}}')
check("deny", "edit_body script comment",
      '{"tool_name":"mcp__agent-context__edit_body","tool_input":{"kind":"script","key":"x-cases","new_string":"# let me check this\\nx = 1"}}')
check("deny", "edit_body doc prose",
      '{"tool_name":"mcp__agent-context__edit_body","tool_input":{"kind":"doc","key":"notes.md","new_string":"Let me check the lockfile."}}')
check("pass", "bulk_edit hook string literal",
      '{"tool_name":"mcp__agent-context__bulk_edit","tool_input":{"edits":[{"kind":"hook","key":"x","replacements":[["a","MSG = \\"It is very slow.\\""]]}]}}')
check("deny", "bulk_edit hook comment",
      '{"tool_name":"mcp__agent-context__bulk_edit","tool_input":{"edits":[{"kind":"hook","key":"x","replacements":[["a","# it is very slow\\nx = 1"]]}]}}')
check("deny", "bulk_edit doc prose",
      '{"tool_name":"mcp__agent-context__bulk_edit","tool_input":{"edits":[{"kind":"doc","key":"n.md","replacements":[["a","It is very slow."]]}]}}')


check("deny", "padding words",
      '{"tool_name":"Write","tool_input":{"file_path":"/x.md","content":"We basically just need to actually fix a number of things in order to ship."}}')

print()
print("passed %d, failed %d" % (pass_n, fail_n))
sys.exit(0 if fail_n == 0 else 1)
