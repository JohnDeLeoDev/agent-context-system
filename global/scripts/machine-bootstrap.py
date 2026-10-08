#!/usr/bin/env python3
"machine-bootstrap.py - idempotent machine bootstrap for the agent-context store.\n\nsetup.sh used to do all of this itself. Everything except the steps below\nis now owned elsewhere and this script does not repeat it:\n  - instruction/skills symlinks for the other harnesses, Claude Code/Desktop/\n    Copilot CLI MCP config, opencode skills symlink and Copilot skillDirectories:\n    home-materialize.py (which this script calls last) and the\n    harness-materialize.py it runs.\n  - Claude Code permissions.allow += mcp__agent-context: superseded by\n    home-settings-sync.py setting defaultMode=bypassPermissions.\n  - opencode's MCP block and permission.mcp block: opencode.json is\n    chezmoi-owned now: a second writer caused drift.\n  - the pre-commit deploy gate: server.py's ensure_precommit_gate.\n\nWhat is left, and still not done anywhere else:\n  1. the `uv` binary (~/.local/bin/uv or on PATH) - every harness's MCP config\n     spawns the agent-context server via `uv run`; missing uv means ENOENT.\n  2. pytest in server/.venv - the daemon's self-deploy gate (daemon.py\n     run_gate = py_compile + pytest) fail-safes and never self-deploys new\n     server code without it.\n  3. pi's `skills` key in ~/.pi/agent/settings.json - pi's own docs\n     (docs/settings.md) list ~/.pi/agent/settings.json as the only GLOBAL\n     settings file; there is no bare ~/.pi/settings.json in pi's schema, so\n     that second path setup.sh used to write is dropped here.\n  4. Copilot CLI's tool_approvals block in ~/.copilot/permissions-config.json\n     - auto-approves the agent-context MCP tools per home location.\n  5. ~/.claude/CLAUDE.md as a symlink to the store's AGENTS.md. Claude Code reads\n     that file before the store loads, and it carries the filesystem and git\n     guardrails; harness-materialize links the other harnesses' instruction files\n     but not Claude's. A different existing file is moved aside, never deleted.\n\nThen this runs home-materialize.py, which handles every projection above and\nin turn calls harness-materialize.py for the other harnesses.\n\nA bare run installs software and writes home config files. Usage:\n  machine-bootstrap.py [--dry-run]\n  -h, --help   print this docstring and exit 0; does nothing else.\n  --dry-run    report what each step would do; write nothing, run nothing.\n\nFresh machine: git clone <remote> ~/.agent-context &&\n  sh ~/.agent-context/setup.sh"
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.environ.get("HOME") or os.path.expanduser("~")
STORE = os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(HOME, ".agent-context")
SERVER_DIR = os.path.join(STORE, "server")
LOCAL_BIN_UV = os.path.join(HOME, ".local", "bin", "uv")


def _uv_binary():
    found = shutil.which("uv")
    if found:
        return found
    if os.path.isfile(LOCAL_BIN_UV) and os.access(LOCAL_BIN_UV, os.X_OK):
        return LOCAL_BIN_UV
    return None


def step_install_uv(dry_run):
    uv = _uv_binary()
    if uv:
        return "uv: already installed (%s)" % uv
    if dry_run:
        return "uv: would install via astral.sh/uv/install.sh"
    
    fetch = subprocess.run(["curl", "-LsSf", "https://astral.sh/uv/install.sh"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if fetch.returncode != 0:
        raise RuntimeError(
            "download failed (curl rc=%d): %s - agent-context MCP will not start"
            % (fetch.returncode, fetch.stderr.decode("utf-8", "replace").strip()[-300:]))
    proc = subprocess.run(["sh"], input=fetch.stdout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "install failed (sh rc=%d): %s - agent-context MCP will not start"
            % (proc.returncode, proc.stderr.decode("utf-8", "replace").strip()[-300:]))
    if not _uv_binary():
        raise RuntimeError("install reported success but no uv binary found")
    return "uv: installed"


def step_install_pytest(dry_run):
    pytest_bin = os.path.join(SERVER_DIR, ".venv", "bin", "pytest")
    if os.path.isfile(pytest_bin) and os.access(pytest_bin, os.X_OK):
        return "pytest: already in server/.venv"
    uv = _uv_binary()
    if not uv:
        raise RuntimeError(
            "no uv binary - install pytest into server/.venv manually "
            "(see onboarding/machine-setup.md)")
    if dry_run:
        return "pytest: would run `uv sync` + `uv pip install pytest` in %s" % SERVER_DIR
    sync = subprocess.run([uv, "sync"], cwd=SERVER_DIR,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if sync.returncode != 0:
        raise RuntimeError("`uv sync` failed in %s (rc=%d): %s"
                            % (SERVER_DIR, sync.returncode, (sync.stderr or "").strip()[-300:]))
    install = subprocess.run([uv, "pip", "install", "pytest"], cwd=SERVER_DIR,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if install.returncode != 0:
        raise RuntimeError("`uv pip install pytest` failed in %s (rc=%d): %s"
                            % (SERVER_DIR, install.returncode, (install.stderr or "").strip()[-300:]))
    return "pytest: installed into server/.venv"


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def _write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def step_pi_skills(dry_run):
    path = os.path.join(HOME, ".pi", "agent", "settings.json")
    if not os.path.isfile(path):
        return "pi skills: %s not found, skipping" % path
    data = _load_json(path)
    dirs = data.get("skills", [])
    if "~/.agent-context/shared-skills" in dirs:
        return "pi skills: already configured"
    if dry_run:
        return "pi skills: would set skills=['~/.agent-context/shared-skills'] in %s" % path
    data["skills"] = ["~/.agent-context/shared-skills"]
    _write_json(path, data)
    return "pi skills: configured"


def step_copilot_permissions(dry_run):
    path = os.path.join(HOME, ".copilot", "permissions-config.json")
    copilot_dir = os.path.join(HOME, ".copilot")
    approval = {"kind": "mcp", "serverName": "agent-context", "toolNames": ["*"]}

    if os.path.isfile(path):
        data = _load_json(path)
        locations = data.get("locations", {})
        has_all = bool(locations) and all(
            any(a.get("kind") == "mcp" and a.get("serverName") == "agent-context"
                for a in loc.get("tool_approvals", []))
            for loc in locations.values())
        if has_all:
            return "copilot permissions: already configured"
        if dry_run:
            return "copilot permissions: would add agent-context tool_approvals to %s" % path
        for loc in data.setdefault("locations", {}):
            approvals = data["locations"][loc].setdefault("tool_approvals", [])
            if not any(a.get("kind") == "mcp" and a.get("serverName") == "agent-context"
                       for a in approvals):
                approvals.append(dict(approval))
        _write_json(path, data)
        return "copilot permissions: configured"

    if os.path.isdir(copilot_dir):
        if dry_run:
            return "copilot permissions: would create %s" % path
        data = {"locations": {HOME: {"tool_approvals": [dict(approval)]}}}
        _write_json(path, data)
        return "copilot permissions: created"

    return "copilot permissions: %s not found, skipping" % copilot_dir


def step_claude_instructions(dry_run):
    target = os.path.join(STORE, "AGENTS.md")
    link = os.path.join(hp.claude_home(HOME), "CLAUDE.md")
    if not os.path.isfile(target):
        raise RuntimeError("%s not found; the store checkout is incomplete" % target)
    if os.path.islink(link) and os.readlink(link) == target:
        return "claude instructions: already linked"
    if dry_run:
        return "claude instructions: would link %s -> %s" % (link, target)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    moved = ""
    if os.path.lexists(link):
        moved = "%s.bak.%d" % (link, int(time.time()))
        os.replace(link, moved)
    os.symlink(target, link)
    return "claude instructions: linked" + (" (previous file kept at %s)" % moved if moved else "")


def step_home_materialize(dry_run):
    path = os.path.join(STORE, "global", "scripts", "home-materialize.py")
    if not os.path.isfile(path):
        raise RuntimeError("home-materialize.py not found at %s" % path)
    if dry_run:
        return "home-materialize: would run %s" % path
    proc = subprocess.run([sys.executable, path])
    if proc.returncode != 0:
        raise RuntimeError("home-materialize.py failed (rc=%d)" % proc.returncode)
    return "home-materialize: done"


STEPS = (
    ("uv", step_install_uv),
    ("pytest", step_install_pytest),
    ("pi skills", step_pi_skills),
    ("copilot permissions", step_copilot_permissions),
    ("claude instructions", step_claude_instructions),
    ("home-materialize", step_home_materialize),
)

KNOWN_FLAGS = ("--dry-run",)


def main(argv):
    
    
    
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    unknown = [a for a in argv if a not in KNOWN_FLAGS]
    if unknown:
        print("machine-bootstrap: unknown flag(s): %s" % " ".join(unknown), file=sys.stderr)
        print("machine-bootstrap: a bare run installs uv/pytest and writes home config. "
              "Refusing rather than guessing.", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    dry_run = "--dry-run" in argv

    for name, fn in STEPS:
        try:
            print(fn(dry_run))
        except Exception as exc:
            print("machine-bootstrap: step '%s' failed: %s" % (name, exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
