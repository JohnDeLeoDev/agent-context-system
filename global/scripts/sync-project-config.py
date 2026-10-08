#!/usr/bin/env python3
"sync-project-config: audit a project's canonical .agents/ store against\nits stack template. Reports drift; --apply re-copies templated files, then\nre-materializes the .claude/ harness projection.\n\nUsage:\n  sync-project-config.py <area> <project> [<stack>] [--apply]\n\nCanonical content lives in .agents/ (gitignored). .claude/ is a generated\nprojection: never audited or edited directly; it is rebuilt from .agents/ by\nagents-materialize.py after --apply."
import glob
import os
import subprocess
import sys


def _same(src, dst):
    'True if src and dst have identical bytes (what `diff -q` checks).'
    try:
        with open(src, "rb") as f1, open(dst, "rb") as f2:
            return f1.read() == f2.read()
    except OSError:
        return False


def _copy(src, dst):
    "Reproduce plain `cp src dst`. If dst exists, truncate-write in place\n    (its mode is untouched). Otherwise create with src's mode, which the\n    kernel filters by umask exactly as a real open(2) call would."
    with open(src, "rb") as fh:
        data = fh.read()
    if os.path.exists(dst):
        with open(dst, "r+b") as fh:
            fh.seek(0)
            fh.write(data)
            fh.truncate()
    else:
        mode = os.stat(src).st_mode & 0o777
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def _chmod_plus_x(path):
    'Reproduce `chmod +x path`: add execute bits not masked by umask.'
    cur_umask = os.umask(0)
    os.umask(cur_umask)
    st = os.stat(path)
    os.chmod(path, st.st_mode | (0o111 & ~cur_umask))


def _find_files(root):
    'Reproduce `find root -type f`: same traversal order, same paths.'
    out = subprocess.run(["find", root, "-type", "f"], capture_output=True, text=True)
    return [line for line in out.stdout.split("\n") if line]


def audit_file(src, dst, dev_path, apply_):
    'Compare src (template) to dst (project file); report and, if apply_,\n    fix. Returns 1 if drift or a missing file was found, else 0.'
    rel = dst[len(dev_path) + 1:] if dst.startswith(dev_path + "/") else dst
    if not os.path.exists(dst):
        print("  missing: %s" % rel)
        if apply_:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _copy(src, dst)
            if src.endswith(".sh"):
                _chmod_plus_x(dst)
            print("    -> copied from template")
        return 1
    if not _same(src, dst):
        print("  drift: %s" % rel)
        if apply_:
            _copy(src, dst)
            if src.endswith(".sh"):
                _chmod_plus_x(dst)
            print("    -> re-copied from template")
        return 1
    return 0


def main():
    args = []
    apply_ = False
    for a in sys.argv[1:]:
        if a == "--apply":
            apply_ = True
        else:
            args.append(a)

    if len(args) < 2:
        print("usage: %s <area> <project> [<stack>] [--apply]"
              % os.path.basename(sys.argv[0]), file=sys.stderr)
        return 64

    area = args[0]
    project = args[1]
    stack = args[2] if len(args) > 2 else ""

    home_dir = os.environ["HOME"]
    store = os.path.join(home_dir, ".agent-context")
    templates = os.path.join(store, "templates")
    dev_path = os.path.join(home_dir, "Developer", area, project)

    if not os.path.isdir(dev_path):
        print("error: %s does not exist" % dev_path, file=sys.stderr)
        return 1

    if not stack:
        if glob.glob(os.path.join(dev_path, "build.gradle*")):
            stack = "kotlin-android"
        elif glob.glob(os.path.join(dev_path, "*.sln")):
            stack = "csharp-dotnet"
        else:
            print("error: could not auto-detect stack for %s" % dev_path, file=sys.stderr)
            return 1

    template_dir = os.path.join(templates, stack)
    if not os.path.isdir(template_dir):
        print("error: unknown stack '%s'" % stack, file=sys.stderr)
        return 1

    print("Auditing %s/%s against %s template (apply=%d)" % (area, project, stack, int(apply_)))

    drift_count = 0

    
    

    
    
    
    dot_agents = os.path.join(template_dir, "dot-agents")
    if os.path.isdir(dot_agents):
        prefix = dot_agents + "/"
        for src in _find_files(dot_agents):
            rel = src[len(prefix):] if src.startswith(prefix) else src
            if rel.startswith("claude/skills/"):
                continue
            drift_count += audit_file(src, os.path.join(dev_path, ".agents", rel), dev_path, apply_)

    
    skills_dir = os.path.join(dot_agents, "claude", "skills")
    if os.path.isdir(skills_dir):
        prefix = dot_agents + "/"
        for src in _find_files(skills_dir):
            rel = src[len(prefix):] if src.startswith(prefix) else src
            dst = os.path.join(dev_path, ".agents", rel)
            if not os.path.exists(dst):
                print("  missing (skill starter): %s" % rel)
                drift_count += 1
                if apply_:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    _copy(src, dst)
                    print("    -> copied starter from template")

    
    shared = os.path.join(templates, "_shared")
    if os.path.isdir(shared):
        prefix = shared + "/"
        for src in _find_files(shared):
            rel = src[len(prefix):] if src.startswith(prefix) else src
            drift_count += audit_file(
                src, os.path.join(dev_path, ".agents", "claude", rel), dev_path, apply_)

    if apply_:
        
        subprocess.run([sys.executable, os.path.join(store, "global", "scripts", "agents-materialize.py"),
                        dev_path])

    if drift_count == 0:
        print("✓ In sync with template.")
    else:
        if apply_:
            print("Applied: %d fix(es)." % drift_count)
        else:
            print("%d drift(s). Run with --apply to fix." % drift_count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
