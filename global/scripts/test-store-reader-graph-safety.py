#!/usr/bin/env python3
'Battery for the keep-list-only safety round on store-reader-graph.py (items A1 to A6).\n\nAdditive to the three locked batteries: this file checks only the new behavior.\nA1 removal_safe and notice. A2 known_gaps. A3 scan_blind_spots extras. A4 text-mode header\nand reach lines. A5 --out refusal rules and mode. A6 new fields in --out JSON.\nFixtures live under ~/.cache/tmp and are removed at the end. stdlib only.'

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "store-reader-graph.py")
CLASSES = ("hooks", "scripts", "docs", "skills", "commands", "agents", "manifests")
LAPTOP_ROOT = "laptop launchers and units (not inspected)"
ALL_CHECKED = ["--checked", "m4", "--checked", "rp", "--checked", "pc",
               "--checked", "mirror-a", "--checked", "mirror-b"]
NOTICE_PHRASES = ("keep-list evidence only", "must not be used to remove")
GAP_KEYWORDS = ("test-", "doc-typed", "~~~", "inline", "python fence", "frontmatter",
                "home .md", "drop-in", "symlink", "listdir", "suffix")
NEW_BLIND_SPOTS = [
    "systemd .service.d drop-in directories",
    "~/.claude.json",
    "symlink launchers in ~/.local/bin",
    "doc-typed areas (templates/, machines/, projects/*/.claude configs)",
    "home markdown files",
    "inline !`cmd` and ~~~ / non-shell fences",
]
OLD_BLIND_SPOTS = [
    "chezmoi source (~/.local/share/chezmoi)",
    "crontab",
    "shell rc files (.zshenv, .zshrc, .bash_profile, .profile)",
    "~/.claude/hooks, commands and scripts directories",
    "symlinked directories (not followed)",
]

scratch_root = os.path.join(os.path.expanduser("~"), ".cache", "tmp")
os.makedirs(scratch_root, exist_ok=True)
tmp = tempfile.mkdtemp(prefix="store-reader-graph-safety-test-", dir=scratch_root)

passed = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


def write(root: str, rel: str, content: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def build(name: str, files: dict[str, str]) -> str:
    root = os.path.join(tmp, name)
    for rel, content in files.items():
        write(root, rel, content)
    for sub in ("store", "home", "outdir"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def run_tool(args: list[str], env_overrides: dict[str, str] | None = None) -> tuple[int, str, str]:
    env = dict(os.environ)
    env.pop("AGENT_CONTEXT_STORE", None)
    env.update(env_overrides or {})
    proc = subprocess.run([sys.executable, TOOL] + args, capture_output=True,
                          text=True, env=env, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def base_args(root: str) -> list[str]:
    return ["--store", os.path.join(root, "store"), "--home", os.path.join(root, "home")]


def run_json(root: str, extra: list[str] | None = None) -> tuple[int, Any, str]:
    rc, out, err = run_tool(base_args(root) + ["--json"] + (extra or []), {"HOME": root})
    try:
        return rc, json.loads(out), err
    except ValueError:
        return rc, None, err + out[:300]


def run_out(root: str, out_path: str) -> tuple[int, str, str]:
    return run_tool(base_args(root) + ["--json", "--out", out_path], {"HOME": root})


def key_entry(report: Any, cls: str, key: str) -> dict[str, Any]:
    section = report.get("classes", {}).get(cls, {}) if isinstance(report, dict) else {}
    found = section.get("keys", {}).get(key) if isinstance(section, dict) else None
    return found if isinstance(found, dict) else {}


def path_state(path: str) -> tuple[bool, bool, str | None, bytes | None]:
    'Existence, symlink-ness, link target and followed content, without raising.'
    exists = os.path.lexists(path)
    is_link = os.path.islink(path)
    target = os.readlink(path) if is_link else None
    content: bytes | None = None
    if os.path.isfile(path):
        with open(path, "rb") as handle:
            content = handle.read()
    return exists, is_link, target, content


def one_line(err: str) -> bool:
    return len(err.strip().splitlines()) == 1 and "Traceback" not in err


def runs(*names: str) -> str:
    return "import subprocess\n" + "".join(
        'subprocess.run(["python3", "%s"])\n' % name for name in names)


def settings_json(hook_name: str) -> str:
    command = "python3 $HOME/.agent-context/global/hooks/" + hook_name
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": command}]}]}}, indent=2) + "\n"


G = "store/global/"
FIXTURE_FILES = {
    G + "hooks/hook-live.py": "print('live')\n",
    "home/.claude/settings.json": settings_json("hook-live.py"),
    G + "scripts/test-t1.py": runs("script-t1.py"),
    G + "scripts/script-t1.py": "print('t1')\n",
    G + "scripts/script-orphan.py": "print('orphan')\n",
}
TEST_ONLY_KEY = "global/scripts/script-t1.py"
UNREACHABLE_KEY = "global/scripts/script-orphan.py"
LIVE_KEY = "global/hooks/hook-live.py"

out_counter = 0


def fresh_out(root: str) -> str:
    global out_counter
    out_counter += 1
    return os.path.join(root, "outdir", "fresh-%d.json" % out_counter)


try:
    root = build("main", FIXTURE_FILES)

    print("fixture sanity")
    rc, base, err = run_json(root)
    check("default --json run exits 0 and prints a JSON object",
          rc == 0 and isinstance(base, dict), "rc=%s %s" % (rc, err[:200]))
    check("fixture: hook-live.py is root-reachable",
          key_entry(base, "hooks", LIVE_KEY).get("reach") == "root-reachable",
          str(key_entry(base, "hooks", LIVE_KEY).get("reach")))
    check("fixture: script-t1.py is test-only",
          key_entry(base, "scripts", TEST_ONLY_KEY).get("reach") == "test-only",
          str(key_entry(base, "scripts", TEST_ONLY_KEY).get("reach")))
    check("fixture: script-orphan.py is unreachable",
          key_entry(base, "scripts", UNREACHABLE_KEY).get("reach") == "unreachable",
          str(key_entry(base, "scripts", UNREACHABLE_KEY).get("reach")))
    check("fixture: old five scan_blind_spots are still present",
          isinstance(base, dict) and all(s in base.get("scan_blind_spots", [])
                                         for s in OLD_BLIND_SPOTS))

    variants: list[tuple[str, list[str]]] = [("default", []), ("all checked", ALL_CHECKED)]
    variants += [("--class " + cls, ["--class", cls]) for cls in CLASSES]
    variants += [("--class hooks, all checked", ["--class", "hooks"] + ALL_CHECKED)]
    reports: dict[str, Any] = {}
    for label, extra in variants:
        rc, report, err = run_json(root, extra)
        reports[label] = report
        check("%s: --json run exits 0 and prints a JSON object" % label,
              rc == 0 and isinstance(report, dict), "rc=%s %s" % (rc, err[:200]))

    print("A1: removal_safe and notice")
    for label, report in reports.items():
        got = report.get("removal_safe", "missing") if isinstance(report, dict) else "no report"
        check("%s: removal_safe is the boolean false" % label, got is False, "got %r" % (got,))
        notice = report.get("notice") if isinstance(report, dict) else None
        check("%s: notice is a string with both required phrases" % label,
              isinstance(notice, str) and all(p in notice for p in NOTICE_PHRASES),
              "got %r" % (notice,))

    print("A2: known_gaps")
    for label, report in reports.items():
        gaps = report.get("known_gaps") if isinstance(report, dict) else None
        shape_ok = (isinstance(gaps, list) and len(gaps) == 11
                    and all(isinstance(g, dict) and set(g) == {"id", "summary"} for g in gaps))
        check("%s: known_gaps is 11 objects with exactly id and summary" % label,
              shape_ok, "got %s" % str(gaps)[:200])
        ids = [g.get("id") for g in gaps] if shape_ok and isinstance(gaps, list) else []
        check("%s: known_gaps ids are G1..G11 in order" % label,
              ids == ["G%d" % n for n in range(1, 12)], "got %s" % ids)
        summaries_ok = shape_ok and isinstance(gaps, list) and all(
            isinstance(g["summary"], str) and g["summary"].strip() for g in gaps)
        check("%s: every summary is a non-empty string" % label, summaries_ok)
    gaps_default = base.get("known_gaps") if isinstance(base, dict) else None
    for n, keyword in enumerate(GAP_KEYWORDS, 1):
        summary = ""
        if isinstance(gaps_default, list) and len(gaps_default) >= n \
                and isinstance(gaps_default[n - 1], dict):
            summary = str(gaps_default[n - 1].get("summary", ""))
        check("G%d summary contains %r" % (n, keyword), keyword.lower() in summary.lower(),
              "summary %r" % summary[:120])

    print("A3: scan_blind_spots extras")
    for label, report in reports.items():
        spots = report.get("scan_blind_spots") if isinstance(report, dict) else None
        missing = [s for s in NEW_BLIND_SPOTS if not isinstance(spots, list) or s not in spots]
        check("%s: scan_blind_spots contains the six new entries" % label,
              not missing, "missing %s" % missing)

    print("A4: text mode")
    rc_text, text, err_text = run_tool(base_args(root), {"HOME": root})
    check("text run exits 0", rc_text == 0, "rc=%s %s" % (rc_text, err_text[:200]))
    lines = text.splitlines()
    class_line = re.compile(r"^(%s): \d+ keys" % "|".join(CLASSES))
    first_class = next((i for i, ln in enumerate(lines) if class_line.match(ln)), len(lines))
    check("text has per-class lines", first_class < len(lines))
    header = "\n".join(lines[:first_class])
    notice_text = base.get("notice") if isinstance(base, dict) else None
    check("header contains the notice sentence",
          isinstance(notice_text, str) and bool(notice_text) and notice_text in header,
          "header %r" % header[:200])
    check("header has the line 'removal_safe: false'",
          any(ln.strip() == "removal_safe: false" for ln in lines[:first_class]),
          "header %r" % header[:200])
    for marker, field in (("unchecked roots:", "unchecked_roots"),
                          ("scan blind spots:", "scan_blind_spots")):
        marker_at = header.find(marker)
        check("header has a line '%s'" % marker,
              any(ln.strip().startswith(marker) for ln in lines[:first_class]))
        entries = base.get(field) if isinstance(base, dict) else None
        entries = entries if isinstance(entries, list) else []
        after = header[marker_at:] if marker_at >= 0 else ""
        missing = [e for e in entries if e not in after]
        check("header lists every %s entry after '%s'" % (field, marker),
              bool(entries) and not missing, "missing %s" % missing[:3])
    check("header lists the laptop root under unchecked roots",
          LAPTOP_ROOT in header[max(header.find("unchecked roots:"), 0):]
          and "unchecked roots:" in header)
    gaps_line = next((ln for ln in lines if ln.startswith("known gaps: 11")), "")
    check("a 'known gaps: 11' line lists G1..G11",
          bool(gaps_line) and all(re.search(r"\bG%d\b" % n, gaps_line) for n in range(1, 12)),
          "line %r" % gaps_line[:200])
    for cls, key, reach in (("scripts", TEST_ONLY_KEY, "test-only"),
                            ("scripts", UNREACHABLE_KEY, "unreachable")):
        info = key_entry(base, cls, key)
        caveat = str(info.get("reach_caveat", ""))
        key_re = re.compile(r"^\s+\S+\s+" + re.escape(key) + r"(\s|$)")
        at = next((i for i, ln in enumerate(lines) if key_re.match(ln)), -1)
        window = "\n".join(lines[at:at + 2]) if at >= 0 else ""
        check("%s line is found in text mode" % os.path.basename(key), at >= 0)
        check("%s line or next carries reach %s" % (os.path.basename(key), reach),
              at >= 0 and reach in window, "window %r" % window[:200])
        check("%s line or next carries its reach_caveat" % os.path.basename(key),
              at >= 0 and bool(caveat) and caveat in window, "window %r" % window[:300])
    live_re = re.compile(r"^\s+\S+\s+" + re.escape(LIVE_KEY) + r"(\s|$)")
    live_at = next((i for i, ln in enumerate(lines) if live_re.match(ln)), -1)
    check("root-reachable key line is not followed by a reach_caveat",
          live_at >= 0 and "not checked on" not in "\n".join(lines[live_at:live_at + 2]))

    print("A5: --out refusal of existing files")
    a5 = build("a5", FIXTURE_FILES)
    outdir = os.path.join(a5, "outdir")
    existing = write(a5, "outdir/existing.json", "PRECIOUS\n")
    real_target = write(a5, "outdir/real-target.json", "TARGET\n")
    os.symlink(real_target, os.path.join(outdir, "sym.json"))
    dangling = os.path.join(outdir, "dangling.json")
    os.symlink(os.path.join(outdir, "nowhere.json"), dangling)
    nowhere = os.path.join(outdir, "nowhere.json")
    existing_cases = (
        ("existing regular file", existing, [existing]),
        ("symlink to an existing file", os.path.join(outdir, "sym.json"),
         [os.path.join(outdir, "sym.json"), real_target]),
        ("dangling symlink", dangling, [dangling, nowhere]),
    )
    for label, out_path, watched in existing_cases:
        before = [path_state(p) for p in watched]
        rc, _o, err = run_out(a5, out_path)
        check("--out %s: refused with nonzero exit" % label, rc != 0, "rc=%s" % rc)
        check("--out %s: one stderr message, no traceback" % label, one_line(err),
              err[:200])
        check("--out %s: target untouched" % label,
              [path_state(p) for p in watched] == before)
    twice = os.path.join(outdir, "twice.json")
    rc_first, out_first, _e = run_out(a5, twice)
    first_state = path_state(twice)
    rc_second, _o, err_second = run_out(a5, twice)
    check("--out same path twice: first run exits 0 and creates the file",
          rc_first == 0 and first_state[0] and first_state[3] is not None, "rc=%s" % rc_first)
    check("--out same path twice: second run refused with nonzero exit", rc_second != 0,
          "rc=%s" % rc_second)
    check("--out same path twice: second run one stderr message, no traceback",
          one_line(err_second), err_second[:200])
    check("--out same path twice: first file unchanged", path_state(twice) == first_state)

    print("A5: --out refusal of protected locations")
    denied_dirs = (".claude", ".codex", ".copilot", ".config", ".ssh",
                   os.path.join(".local", "bin"), "Library")
    for rel in denied_dirs:
        directory = os.path.join(a5, rel)
        os.makedirs(os.path.join(directory, "sub"), exist_ok=True)
        for label, target in (("directly in", os.path.join(directory, "report.json")),
                              ("nested under", os.path.join(directory, "sub", "report.json"))):
            rc, _o, err = run_out(a5, target)
            check("--out %s $HOME/%s: refused with nonzero exit" % (label, rel), rc != 0,
                  "rc=%s" % rc)
            check("--out %s $HOME/%s: one stderr message, no traceback" % (label, rel),
                  one_line(err), err[:200])
            check("--out %s $HOME/%s: no file created" % (label, rel),
                  not os.path.lexists(target))
    link_dir = os.path.join(outdir, "linkclaude")
    os.symlink(os.path.join(a5, ".claude"), link_dir)
    linked_target = os.path.join(link_dir, "via-link.json")
    rc, _o, err = run_out(a5, linked_target)
    check("--out through a symlinked directory into $HOME/.claude: refused", rc != 0,
          "rc=%s" % rc)
    check("--out through a symlinked directory into $HOME/.claude: no file, one message",
          one_line(err) and not os.path.lexists(os.path.join(a5, ".claude", "via-link.json")),
          err[:200])
    for dotfile in (".zshrc", ".reader-graph-report.json"):
        target = os.path.join(a5, dotfile)
        rc, _o, err = run_out(a5, target)
        check("--out $HOME/%s (dotfile directly in $HOME): refused" % dotfile, rc != 0,
              "rc=%s" % rc)
        check("--out $HOME/%s: one stderr message, no file created" % dotfile,
              one_line(err) and not os.path.lexists(target), err[:200])

    print("A5: --out accepted paths and mode")
    os.makedirs(os.path.join(a5, ".cache", "reports"), exist_ok=True)
    accepted = (("fresh path under $HOME/outdir", os.path.join(outdir, "report.json")),
                ("fresh path under $HOME/.cache", os.path.join(a5, ".cache", "reports",
                                                              "report.json")))
    for label, target in accepted:
        rc, _o, err = run_out(a5, target)
        check("--out %s: accepted with exit 0" % label,
              rc == 0 and os.path.isfile(target), "rc=%s %s" % (rc, err[:200]))
        mode = stat.S_IMODE(os.stat(target).st_mode) if os.path.isfile(target) else None
        check("--out %s: file mode is 0600" % label, mode == 0o600,
              "mode %s" % (oct(mode) if mode is not None else None))

    print("A6: new fields in the --out JSON")
    for label, extra in (("default", []), ("all checked", ALL_CHECKED),
                         ("--class docs", ["--class", "docs"])):
        target = fresh_out(a5)
        rc, stdout, _err = run_tool(base_args(a5) + ["--json", "--out", target] + extra,
                                    {"HOME": a5})
        written: Any = None
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as handle:
                try:
                    written = json.load(handle)
                except ValueError:
                    written = None
        try:
            printed: Any = json.loads(stdout)
        except ValueError:
            printed = None
        check("%s: --out exits 0 and writes a JSON object" % label,
              rc == 0 and isinstance(written, dict), "rc=%s" % rc)
        check("%s: --out JSON equals stdout JSON" % label,
              isinstance(written, dict) and written == printed)
        check("%s: --out JSON has removal_safe false and the notice" % label,
              isinstance(written, dict) and written.get("removal_safe") is False
              and isinstance(written.get("notice"), str)
              and all(p in written["notice"] for p in NOTICE_PHRASES))
        check("%s: --out JSON has 11 known_gaps and the new blind spots" % label,
              isinstance(written, dict) and isinstance(written.get("known_gaps"), list)
              and len(written["known_gaps"]) == 11
              and all(s in written.get("scan_blind_spots", []) for s in NEW_BLIND_SPOTS))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

total = passed + len(failures)
print("test-store-reader-graph-safety: %d/%d passed" % (passed, total))
sys.exit(1 if failures else 0)
