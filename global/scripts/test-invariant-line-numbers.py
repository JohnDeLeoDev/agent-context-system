#!/usr/bin/env python3
"Reported line numbers in invariant-check.py are the file's own line numbers.\n\nEach fixture puts a three-line docstring and a comment line above the offending line, so\na check that counts lines in stripped text reports line 5 or earlier instead of line 6.\nThe last case pins the one behavior the fix could break: an fsync whose guard sits above\ntwo comment lines still passes.\n\nOffending tokens are built by concatenation so invariant-check does not report this file.\n\nUsage: test-invariant-line-numbers.py"

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "invariant-check.py")

PY3 = "pyth" + "on3"
FSYNC = "os.fs" + "ync("
REPLACE = "os.rep" + "lace("
OPEN_W = "op" + 'en(target, "w")'
HEAD = '"""Fixture.\n\nThree lines.\n"""\n# a comment line\n'

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


def load():
    spec = importlib.util.spec_from_file_location("invariant_check_lines", CHECK)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load()
    cases = (
        ("store-write-not-atomic", mod._store_write_not_atomic,
         HEAD + "fh = " + OPEN_W + "\n", "line 6"),
        ("fsync-not-behind-the-switch", mod._fsync_not_behind_the_switch,
         HEAD + FSYNC + "fd)\n", "line 6"),
        ("hand-rolled-atomic-write", mod._hand_rolled_atomic_write,
         HEAD + REPLACE + "a, b)\n", "line 6"),
        ("interpreter-is-rendered", mod._bare_python_command,
         HEAD + 'cmd = "' + PY3 + ' tool.py"\n', "line 6"),
        ("stages-foreign-machine-rows", mod._stages_foreign_machine_rows,
         HEAD + 'run(["git", "add", "-A", "--", ".", ":!server"])\n', "line(s) 6"),
    )
    print("[1] a finding cites the line in the file, after a docstring and a comment")
    for label, fn, src, want in cases:
        try:
            got = fn(os.path.join(HERE, "fixture.py"), src)
        except Exception as exc:
            check(label, False, "raised %s: %s" % (type(exc).__name__, exc))
            continue
        ok = bool(got) and (want + " " in got + " " or want + "," in got
                            or got.endswith(want))
        check("%s reports %s" % (label, want), ok, "got %r" % (got,))

    print("[2] a guard above comment lines still covers the fsync")
    src = (HEAD + "if _fsync_enabled():\n    # why the flush\n    # is conditional\n    "
           + FSYNC + "fd)\n")
    try:
        got = mod._fsync_not_behind_the_switch(os.path.join(HERE, "fixture.py"), src)
        check("a guarded fsync with two comment lines between passes", got is None,
              "got %r" % (got,))
    except Exception as exc:
        check("a guarded fsync with two comment lines between passes", False,
              "raised %s: %s" % (type(exc).__name__, exc))

    print("\n%d passed, %d failed" % (passed, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
