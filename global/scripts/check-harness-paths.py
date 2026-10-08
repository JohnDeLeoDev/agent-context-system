#!/usr/bin/env python3
'List every place Python code builds a harness directory path by hand.\n\nUsage: check-harness-paths.py [--include-tests] PATH [PATH...]\n\nEach PATH is a .py file or a directory searched for .py files (worktrees, .git and\nnode_modules are skipped). Test files (test-*.py, wrapper-port-*.py, *-cases.py) build fake\nhomes on purpose and are skipped, even when named by hand, unless --include-tests is given. Every file is parsed with ast, so a path built in pieces is\nfound as well as one written out. The finding shapes:\n\n  literal    a string that names .claude, such as "~/.claude/hooks/x.py"\n  join       os.path.join(HOME, ".claude", "hooks"), Path(...), .joinpath(...)\n  join-root  the same call with ".claude" as its last piece\n  concat     HOME + "/.claude/scripts"\n  pathlib    Path.home() / ".claude" / "hooks"\n  fstring    f"{HOME}/.claude/state"\n\nA docstring, a comment, or a message string with whitespace names .claude as prose and is\nnot a finding. harness_paths.py is skipped: building the paths is its job. The remaining\nfindings are the worklist for moving a script onto harness_paths.\n\nOutput: one `file:line: shape` line per finding, then `N finding(s) in M file(s)`.\nExit 0 when clean, 3 when there are findings, 2 on a missing path or an unparseable file.\nRuns on Python 3.8, the system Python on the Synology nodes.'

import ast
import os
import re
import sys

SKIP_DIRS = {"worktrees", ".git", "node_modules", "__pycache__"}
OWNERS = ("harness_paths.py", "check-harness-paths.py")
JOIN_NAMES = {"join", "joinpath", "Path", "PurePath"}
NAMES_CLAUDE = re.compile(r"(?<![A-Za-z0-9_])\.claude(?![A-Za-z0-9_-])")
WHITESPACE = re.compile(r"\s")


def claude_text(node):
    'The string a node holds when it is a path-like string naming .claude, else None.\n\n    A string with whitespace is a message to a person, and reads ~/.claude as prose.'
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    text = node.value
    if WHITESPACE.search(text) or not NAMES_CLAUDE.search(text):
        return None
    return text


def is_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def line_of(node):
    return getattr(node, "lineno", 0)


def call_name(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and is_str(body[0].value):
                ids.add(id(body[0].value))
    return ids


def findings_for(tree):
    'Return (line, shape) pairs for one parsed file.'
    found = []
    seen = docstring_ids(tree)

    def mark(node):
        for child in ast.walk(node):
            if is_str(child):
                seen.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) in JOIN_NAMES:
            args = list(node.args)
            for i, arg in enumerate(args):
                text = claude_text(arg)
                if text is not None and id(arg) not in seen:
                    shape = "join-root" if text == ".claude" and i == len(args) - 1 else "join"
                    found.append((line_of(arg), shape))
                    seen.add(id(arg))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            for side in (node.left, node.right):
                if claude_text(side) is not None and id(side) not in seen:
                    shape = "pathlib" if isinstance(node.op, ast.Div) else "concat"
                    found.append((line_of(side), shape))
                    seen.add(id(side))
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if claude_text(part) is not None and id(part) not in seen:
                    found.append((line_of(node), "fstring"))
                    mark(node)
                    break
    for node in ast.walk(tree):
        if claude_text(node) is not None and id(node) not in seen:
            found.append((line_of(node), "literal"))
    return sorted(set(found))


def py_files(path):
    if os.path.isfile(path):
        yield path
        return
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def is_test_file(name):
    'Test and fixture files build fake homes on purpose, and several are locked.'
    return name.startswith(("test-", "wrapper-port-")) or name.endswith("-cases.py")


def main(argv):
    include_tests = "--include-tests" in argv
    argv = [a for a in argv if a != "--include-tests"]
    if not argv:
        sys.stderr.write("usage: check-harness-paths.py [--include-tests] PATH [PATH...]\n")
        return 2
    total = 0
    files_hit = 0
    errors = 0
    for path in argv:
        if not os.path.exists(path):
            sys.stderr.write("check-harness-paths: no such path: %s\n" % path)
            errors += 1
            continue
        for fname in py_files(path):
            base = os.path.basename(fname)
            if base in OWNERS or (is_test_file(base) and not include_tests):
                continue
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=fname)
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                sys.stderr.write("check-harness-paths: cannot parse %s: %s\n" % (fname, exc))
                errors += 1
                continue
            hits = findings_for(tree)
            for line, shape in hits:
                print("%s:%d: %s" % (fname, line, shape))
            total += len(hits)
            files_hit += 1 if hits else 0
    print("%d finding(s) in %d file(s)" % (total, files_hit))
    if errors:
        return 2
    return 3 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
