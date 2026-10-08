#!/usr/bin/env python3
"agents-materialize.py -- Rebuild the harness projection(s) from canonical .agents/.\n\nThe canonical, agent-neutral store on disk is <root>/.agents/ (gitignored,\nreconstituted from the ~/.agent-context git store). Coding-agent harnesses that\ncannot read that layout directly (Claude Code, etc.) get a thin *generated*\nprojection built here. Nothing under an agent-named dir (.claude/, .copilot/...)\nis source -- it is all rebuilt from .agents/.\n\nLayout contract:\n  .agents/claude/settings.json        -> hardlinked to .claude/settings.json\n  .agents/claude/settings.local.json  -> hardlinked to .claude/settings.local.json\n  .agents/claude/skills|commands|agents  -> copied to .claude/<same>\n  .agents/scripts|hooks                -> used IN PLACE (worktree flow and settings hooks name\n                                          them under .agents/); not projected\n  .agents/docs|tmp                     -> used IN PLACE (referenced by path); not projected\n  .agents/claude/commands              -> also projected to .opencode/commands (opencode\n                                          has no Claude-compat path for commands)\n  .claude/worktrees                    -> harness-owned, ephemeral; never touched\n  .claude/ralph-loop.local.md          -> harness-owned; the ralph-loop plugin's own\n                                          stop-hook hardcodes this path, so it cannot\n                                          be moved under .agents/ with the other state\n\nThe dividing line, and the reason for the orphan report at the bottom: .claude/ holds\nONLY what this script can rebuild, plus the two harness-owned entries above. Durable\nstate -- scratch dirs, generated docs, logs, credentials -- belongs under .agents/,\nwhich is excluded per-clone and swept. Keep that true and `rm -rf .claude` followed by\na re-materialize is a safe repair; let it rot and the directory quietly becomes the\nonly copy of things nobody is backing up.\n\nWhy hardlink settings but copy the dirs:\n  - Symlinked .claude/settings.json breaks permission recognition (upstream bug),\n    and a symlinked skills/ dir is not discovered. So neither may be a symlink.\n  - A hardlink is a real file sharing one inode, so in-place /config edits stay in\n    sync with the canonical copy automatically (and dodge the symlink bugs).\n  - Dir hardlinks are not permitted, so skills/commands/agents are copied; edit\n    them in .agents/claude/ and re-run this script.\n\nDevice-agnostic: no absolute paths; hardlinks require .agents and .claude on the\nsame filesystem (always true -- both live in the repo). Idempotent. Safe to re-run,\nincluding as a SessionStart hook."

import hashlib
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def regular_files(root):
    'Relative paths of regular files under root, depth-first, sorted (like `find -type f`).'
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            import stat as _stat
            if _stat.S_ISREG(st.st_mode):
                out.append(os.path.relpath(path, root))
    return out


def link_file(src, dst):
    'Single-file harness config: reconcile then hardlink (same inode = no drift).'
    if not os.path.isfile(src):
        return 0
    
    if os.path.exists(dst) and os.path.samefile(src, dst):
        return 0
    
    
    if os.path.isfile(dst) and not os.path.islink(dst) and os.path.getmtime(dst) > os.path.getmtime(src):
        with open(dst, "rb") as fh:
            data = fh.read()
        with open(src, "wb") as fh:
            fh.write(data)
    try:
        os.remove(dst)
    except FileNotFoundError:
        pass
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy(src, dst)
    return 1


class Materializer:
    'Dir-scanned / harness-reached content: update the projection IN PLACE from\n    canonical. rsync -a preserves mode (the exec bit on scripts/hooks) and\n    timestamps just like `cp -Rp`, and --delete prunes files dropped from .agents.\n    rsync writes each changed file to a temp under dst and renames it over, so dst\n    is never momentarily absent.\n\n    Authorship is decided by a recorded manifest of what WE last wrote, not by mtimes:\n    .agents/ is itself re-projected from the store at every SessionStart, so its files\n    routinely carry a newer mtime than a genuine hand edit in .claude/. A hash that\n    still matches the manifest is provably our own untouched output; a hash matching\n    neither the manifest nor the incoming source is something a person or agent wrote.'

    def __init__(self, root):
        self.root = root
        self.a = os.path.join(root, ".agents")
        self.c = hp.project_claude_dir(root)
        self.n_link = 0
        self.n_copy = 0
        self.manifest_path = os.path.join(self.a, "tmp", ".claude-projection-manifest")
        self.rescue_dir = os.path.join(
            self.a, "tmp", "claude-projection-rescue", time.strftime("%Y%m%d-%H%M%S"))
        self.rescued = 0
        self.rescue_names = []
        self.manifest = self._load_manifest()

    def _load_manifest(self):
        recorded = {}
        try:
            with open(self.manifest_path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return recorded
        for line in lines:
            fields = line.split()
            if len(fields) >= 2 and fields[1] not in recorded:
                recorded[fields[1]] = fields[0]
        return recorded

    def copy_dir(self, src, dst):
        if not os.path.isdir(src):
            return
        rel = os.path.relpath(dst, self.c)
        os.makedirs(dst, exist_ok=True)
        
        for r in regular_files(dst):
            f = os.path.join(dst, r)
            src_f = os.path.join(src, r)
            if not os.path.isfile(src_f):
                continue  
            if _files_equal(f, src_f):
                continue  
            key = "%s/%s" % (rel, r)
            recorded = self.manifest.get(key)
            if recorded is None:
                continue  
            if sha256_of(f) == recorded:
                continue  
            dest = os.path.join(self.rescue_dir, rel, r)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.copy2(f, dest)
            except OSError:
                pass
            self.rescued += 1
            self.rescue_names.append("  ##  overwritten: .claude/%s" % key)
        proc = subprocess.run(["rsync", "-a", "--delete", src + "/", dst + "/"])
        if proc.returncode != 0:
            sys.exit(proc.returncode)
        self.n_copy += 1

    def link_file(self, src, dst):
        self.n_link += link_file(src, dst)

    def write_manifest(self):
        os.makedirs(os.path.join(self.a, "tmp"), exist_ok=True)
        try:
            lines = []
            for d in ("skills", "commands", "agents"):
                dpath = os.path.join(self.c, d)
                if not os.path.isdir(dpath):
                    continue
                for r in regular_files(dpath):
                    rel = "%s/%s" % (d, r)
                    lines.append("%s  %s" % (sha256_of(os.path.join(dpath, r)), rel))
            tmp = self.manifest_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, self.manifest_path)
        except OSError:
            pass

    def prune_rescue_batches(self):
        
        
        rescue_root = os.path.join(self.a, "tmp", "claude-projection-rescue")
        try:
            batches = sorted(
                (d for d in os.listdir(rescue_root)
                 if os.path.isdir(os.path.join(rescue_root, d))),
                reverse=True)
        except OSError:
            return
        for old in batches[20:]:
            shutil.rmtree(os.path.join(rescue_root, old), ignore_errors=True)


def _files_equal(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def main(argv):
    
    
    
    args = argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print("agents-materialize: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    root = args[0] if args and args[0] else (
        os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    a = os.path.join(root, ".agents")
    c = hp.project_claude_dir(root)

    if not os.path.isdir(a):
        print("agents-materialize: no .agents/ at %s — nothing to do" % root, file=sys.stderr)
        return 0
    os.makedirs(c, exist_ok=True)

    m = Materializer(root)

    m.link_file(os.path.join(a, "claude", "settings.json"), os.path.join(c, "settings.json"))
    m.link_file(os.path.join(a, "claude", "settings.local.json"),
                os.path.join(c, "settings.local.json"))

    
    
    
    
    local_json = os.path.join(c, "settings.local.json")
    canon_local_json = os.path.join(a, "claude", "settings.local.json")
    if os.path.isfile(local_json) and not os.path.isfile(canon_local_json):
        os.makedirs(os.path.join(a, "claude"), exist_ok=True)
        shutil.copy2(local_json, canon_local_json)
        print("agents-materialize: captured .claude/settings.local.json into .agents/ "
              "— it had no canonical copy", file=sys.stderr)
        m.link_file(canon_local_json, local_json)

    m.copy_dir(os.path.join(a, "claude", "skills"), os.path.join(c, "skills"))
    m.copy_dir(os.path.join(a, "claude", "commands"), os.path.join(c, "commands"))
    m.copy_dir(os.path.join(a, "claude", "agents"), os.path.join(c, "agents"))
    
    
    

    
    
    
    
    
    
    home = os.environ.get("HOME", "")
    pin = os.path.join(home, ".agent-context", "global", "scripts", "agents-pin.py")
    agents_dir = os.path.join(c, "agents")
    if (os.path.isfile(pin) and os.path.isdir(os.path.join(home, ".pi", "agent"))
            and os.path.isdir(agents_dir)):
        
        
        
        sys.stdout.flush()
        proc = subprocess.run([sys.executable, pin, "pi", agents_dir,
                                os.path.join(root, ".pi", "agents")])
        if proc.returncode != 0:
            print("agents-materialize: pi-agents-pin failed (pi workers stay on tier aliases)",
                  file=sys.stderr)

    
    
    
    
    m.write_manifest()

    print("agents-materialize: .claude projection rebuilt from .agents (%d linked, %d dir(s) copied)"
          % (m.n_link, m.n_copy))

    
    
    
    
    
    
    
    projected_names = {"skills", "commands", "agents", "scripts", "hooks",
                        "settings.json", "settings.local.json"}
    harness_names = {"worktrees", "ralph-loop.local.md", ".DS_Store"}
    try:
        entries = os.listdir(c)
    except OSError:
        entries = []
    visible = sorted(n for n in entries if not n.startswith("."))
    hidden = sorted(n for n in entries if n.startswith(".") and n not in (".", ".."))
    orphans = []
    for n in visible + hidden:
        if n in projected_names or n in harness_names:
            continue
        orphans.append("    ⚠ .claude/%s" % n)
    if orphans:
        print("agents-materialize: content in .claude/ that nothing projects "
              "— not rebuilt, not swept, not backed up:", file=sys.stderr)
        print("\n".join(orphans), file=sys.stderr)
        print("    Move it under .agents/ (already excluded per-clone, and swept) or delete it.",
              file=sys.stderr)
        print("    .claude/ must stay disposable: 'rm -rf .claude && materialize' is the repair.",
              file=sys.stderr)

    
    
    
    
    
    
    
    
    
    
    opencode_config = os.path.join(home, ".config", "opencode")
    commands_dir = os.path.join(a, "claude", "commands")
    if os.path.isdir(opencode_config) and os.path.isdir(commands_dir):
        hm = os.path.join(home, ".agent-context", "global", "scripts", "harness-materialize.py")
        if os.path.isfile(hm):
            sys.stdout.flush()  
            subprocess.run([sys.executable, hm, "--commands", commands_dir,
                             os.path.join(root, ".opencode", "commands")])
        git_info = os.path.join(root, ".git", "info")
        exclude = os.path.join(git_info, "exclude")
        has_entry = False
        if os.path.isfile(exclude):
            with open(exclude, encoding="utf-8", errors="replace") as fh:
                has_entry = any(line == ".opencode/" for line in fh.read().splitlines())
        if os.path.isdir(git_info) and not has_entry:
            
            
            needs_newline = False
            if os.path.isfile(exclude) and os.path.getsize(exclude) > 0:
                with open(exclude, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    needs_newline = fh.read(1) != b"\n"
            with open(exclude, "ab") as fh:
                if needs_newline:
                    fh.write(b"\n")
                fh.write(b".opencode/\n")

    
    
    codex_home = os.path.join(home, ".codex")
    if os.path.isdir(codex_home):
        hm = os.path.join(home, ".agent-context", "global", "scripts", "harness-materialize.py")
        if os.path.isfile(hm):
            sys.stdout.flush()
            proc = subprocess.run([sys.executable, hm, "--codex-project", root])
            if proc.returncode != 0:
                print("agents-materialize: Codex projection failed", file=sys.stderr)
        git_info = os.path.join(root, ".git", "info")
        exclude = os.path.join(git_info, "exclude")
        if os.path.isdir(git_info):
            old = ""
            if os.path.isfile(exclude):
                with open(exclude, encoding="utf-8", errors="replace") as fh:
                    old = fh.read()
            if ".codex/" not in old.splitlines():
                with open(exclude, "a", encoding="utf-8") as fh:
                    if old and not old.endswith("\n"):
                        fh.write("\n")
                    fh.write(".codex/\n")

    m.prune_rescue_batches()

    
    
    
    
    if m.rescued > 0:
        rel_rescue_dir = os.path.relpath(m.rescue_dir, root)
        print("", file=sys.stderr)
        print("  ################################################################", file=sys.stderr)
        print("  ##  LOCAL EDITS IN .claude/ WERE OVERWRITTEN FROM .agents/    ##", file=sys.stderr)
        print("  ################################################################", file=sys.stderr)
        print("\n".join(m.rescue_names), file=sys.stderr)
        print("  ##", file=sys.stderr)
        print("  ##  YOUR VERSIONS ARE SAVED HERE (nothing was lost):", file=sys.stderr)
        print("  ##    %s" % rel_rescue_dir, file=sys.stderr)
        print("  ##", file=sys.stderr)
        print("  ##  .claude/ is a GENERATED projection of .agents/, which is itself", file=sys.stderr)
        print("  ##  projected from the agent-context store. To keep this work, promote", file=sys.stderr)
        print("  ##  the rescued file into the STORE (upsert_script / upsert_hook /", file=sys.stderr)
        print("  ##  edit_body). Editing .claude/ alone cannot survive a SessionStart.", file=sys.stderr)
        print("  ##  To discard it instead, delete the rescue directory above.", file=sys.stderr)
        print("  ################################################################", file=sys.stderr)
        print("", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
