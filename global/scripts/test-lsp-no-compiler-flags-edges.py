#!/usr/bin/env python3
'THE FAULT (measured by the reviewer). buildServer.json fields were read with a line\nmatcher that JOINED every match with a newline. A file with two "build_root" lines gave\n"/root1\\n/root2", os.path.isdir() failed on that, and swift_unbuilt() returned None: the\nexact silent "not found" the gate exists to prevent. The launch check reads fields the\nsame way, so the same file also looked like a vanished root and forced a rebind.\n\nEvery case uses a throwaway HOME and stubs; nothing real is touched.\n\nRun: python3 test-lsp-no-compiler-flags-edges.py'
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LSPD = os.path.join(HERE, "lspd.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -- " + detail) if detail and not cond else ""), flush=True)


spec = importlib.util.spec_from_file_location("lspd_under_test", LSPD)
if spec is None or spec.loader is None:
    sys.exit("cannot load %s" % LSPD)
lspd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lspd)


def root(home, name, logs):
    d = os.path.join(home, name)
    os.makedirs(d)
    if logs:
        os.makedirs(os.path.join(d, "Logs", "Build"))
        open(os.path.join(d, "Logs", "Build", "x.xcactivitylog"), "w").close()
    return d


def workspace(home, name, bsj_text):
    ws = os.path.join(home, name)
    os.makedirs(os.path.join(ws, "Foo.xcodeproj"))
    with open(os.path.join(ws, "buildServer.json"), "w") as fh:
        fh.write(bsj_text)
    return os.path.realpath(ws)


def duplicated(r1, r2):
    
    return ('{\n  "name": "xcode build server",\n  "build_root": %s,\n  "scheme": "App",\n'
            '  "build_root": %s\n}\n' % (json.dumps(r1), json.dumps(r2)))


def main():
    home = os.path.realpath(tempfile.mkdtemp(prefix="nfe-"))
    try:
        print("unbuilt-Swift gate: buildServer.json edges\n")
        a1, a2 = root(home, "a1", False), root(home, "a2", False)
        ws = workspace(home, "dup-unbuilt", duplicated(a1, a2))
        got = lspd.swift_unbuilt(ws)
        check("a duplicated build_root, both roots unbuilt: the gate still fires",
              isinstance(got, str) and got.startswith(
                  "no compiler flags: build Foo once (xcodebuild build -project "
                  "Foo.xcodeproj -scheme 'App' "), "got %r" % got)

        b1, b2 = root(home, "b1", True), root(home, "b2", True)
        ws = workspace(home, "dup-built", duplicated(b1, b2))
        check("a duplicated build_root, both roots built: no gate",
              lspd.swift_unbuilt(ws) is None, "got %r" % lspd.swift_unbuilt(ws))

        bindir = os.path.join(home, "bin")
        os.makedirs(bindir)
        os.makedirs(os.path.join(home, "brew"))
        xlog = os.path.join(home, "xbs.log")
        for name, body in (("xcode-build-server", 'echo "$*" >> "%s"' % xlog),
                           ("xcode-select", "echo /Applications/Xcode.app/Contents/Developer"),
                           ("sourcekit-lsp", "exit 0")):
            p = os.path.join(bindir, name)
            with open(p, "w") as fh:
                fh.write("#!/bin/sh\n" + body + "\n")
            os.chmod(p, 0o755)
        env = dict(os.environ, HOME=home, PATH=bindir + ":/usr/bin:/bin",
                   LSPD_HOMEBREW_BIN=os.path.join(home, "brew"))
        for k in ("AGENT_LSP_WORKSPACE", "DEVELOPER_DIR", "LSPD_XCODE_APPS"):
            env.pop(k, None)
        p = subprocess.run([sys.executable, LSPD, "--mcp", "--key", "sourcekit-lsp",
                            "--workspace", ws, "--preflight"], cwd=ws, env=env,
                           capture_output=True, text=True, timeout=60)
        check("... and the launch check does not rebind it as a vanished root",
              p.returncode == 0 and not os.path.exists(xlog) and "rebinding" not in p.stderr,
              "rc=%s rebind_calls=%s stderr=%r" % (p.returncode, os.path.exists(xlog),
                                                  p.stderr[:200]))

        c1 = root(home, "c1", False)
        ws = workspace(home, "not-json", '# hand edited\n"build_root": "%s",\n"scheme": "App"\n'
                       % c1)
        got = lspd.swift_unbuilt(ws)
        check("a buildServer.json that is not JSON keeps the first-matching-line reading",
              isinstance(got, str) and got.startswith("no compiler flags: build Foo once ("),
              "got %r" % got)

        d1 = root(home, "d1", False)
        ws = workspace(home, "plain", json.dumps({"build_root": d1, "scheme": "App"}))
        got = lspd.swift_unbuilt(ws)
        check("must not change: a single well-formed build_root still gates",
              isinstance(got, str) and got.startswith("no compiler flags: build Foo once ("),
              "got %r" % got)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
