#!/usr/bin/env python3
'Usage:\n  init-project.py <area> <project> [<stack>]\n\nIdempotent: existing files are NOT overwritten. Canonical content lives in\n.agents/ (gitignored); .claude/ is a generated projection rebuilt by\nagents-materialize.py and must never be edited by hand.'

import glob
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp


def find_files(root):
    'Files under root, deepest-first-stable, sorted at each level.'
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            found.append(os.path.join(dirpath, fn))
    return found


def strip_prefix(path, prefix):
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


def main():
    prog = os.path.basename(sys.argv[0])
    args = sys.argv[1:]

    if len(args) < 2:
        print(f"usage: {prog} <area> <project> [<stack>]", file=sys.stderr)
        return 64

    area = args[0]
    project = args[1]
    stack = args[2] if len(args) > 2 else ""

    home_dir = os.environ.get("HOME")
    if home_dir is None:
        print(f"{prog}: HOME: unbound variable", file=sys.stderr)
        return 1

    store = os.path.join(home_dir, ".agent-context")
    templates = os.path.join(store, "templates")
    dev_path = os.path.join(home_dir, "Developer", area, project)

    if not os.path.isdir(dev_path):
        print(f"error: {dev_path} does not exist", file=sys.stderr)
        return 1

    
    if not stack:
        if glob.glob(os.path.join(dev_path, "build.gradle*")):
            stack = "kotlin-android"
        elif glob.glob(os.path.join(dev_path, "*.sln")):
            stack = "csharp-dotnet"
        else:
            print(f"error: could not auto-detect stack for {dev_path}", file=sys.stderr)
            print("       pass stack explicitly: kotlin-android | csharp-dotnet", file=sys.stderr)
            return 1

    template_dir = os.path.join(templates, stack)
    if not os.path.isdir(template_dir):
        print(f"error: unknown stack '{stack}'", file=sys.stderr)
        return 1

    print(f"Initializing {area}/{project} as {stack}")
    print(f"  target: {dev_path}")

    

    def copy_if_absent(src, dst):
        if os.path.exists(dst):
            print(f"  skip (exists): {strip_prefix(dst, dev_path + '/')}")
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        result = subprocess.run(["cp", src, dst])
        if result.returncode != 0:
            sys.exit(result.returncode)
        print(f"  copied: {strip_prefix(dst, dev_path + '/')}")

    
    copy_if_absent(os.path.join(template_dir, "CLAUDE.md"), os.path.join(dev_path, "CLAUDE.md"))

    
    dot_agents = os.path.join(template_dir, "dot-agents")
    if os.path.isdir(dot_agents):
        for src in find_files(dot_agents):
            rel = strip_prefix(src, dot_agents + "/")
            dst = os.path.join(dev_path, ".agents", rel)
            copy_if_absent(src, dst)
            
            if src.endswith(".sh"):
                try:
                    st = os.stat(dst)
                    os.chmod(dst, st.st_mode | 0o111)
                except OSError:
                    pass

    
    
    
    shared = os.path.join(templates, "_shared")
    if os.path.isdir(shared):
        for src in find_files(shared):
            rel = strip_prefix(src, shared + "/")
            copy_if_absent(src, os.path.join(dev_path, ".agents", "claude", rel))

    
    subprocess.run([sys.executable, os.path.join(store, "global", "scripts", "agents-materialize.py"), dev_path])

    
    gi = os.path.join(dev_path, ".gitignore")
    for pat in ("/" + hp.AGENTS_DIRNAME + "/", "/" + hp.CLAUDE_DIRNAME + "/"):
        present = False
        if os.path.isfile(gi):
            with open(gi) as f:
                present = pat in f.read().splitlines()
        if not present:
            with open(gi, "a") as f:
                f.write(pat + "\n")
            print(f"  gitignore += {pat}")

    

    print()
    print("There is no project task tracker. Raise open items with user in the")
    print("conversation; do not file them to Reminders, Notes, or a repo doc.")

    print()
    print(f"Done. Stack: {stack}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
