#!/usr/bin/env python3
"Battery for project-materialize.py's project-scope fetch (trigger B).\n\nOn a machine with no store checkout and a relay env, project-materialize.py asks the daemon (via\nstore_mcp's get_materialized MCP tool, policy/policy: one pathway) for the ONE project the cwd's\nrepo belongs to. It keeps only `projects/` and `workspaces/` keys in a per-run temp tree under\n$TMPDIR, overlays that project's commands, skills and scripts onto the repo's .agents/, and\ndeletes the tree. Nothing of the fetched scope is written under ~/.agent-context/ and no\nper-project record is kept. Files an older version wrote there (and their records in\n~/.cache/agent-context/project-scope/) are retired: a file goes only when its bytes still equal\nthe digest the record holds.\n\nTransport (HTTP, bearer header, JSON-RPC, redirects, the size cap, the deadline) is store_mcp's\nown contract and is covered by test-store-mcp.py. Every case here runs the WORKTREE's sibling\nproject-materialize.py as a subprocess (it still needs real git and filesystem behavior), with\nstore_mcp.call intercepted via a sitecustomize.py shim on PYTHONPATH: no case opens a socket or\ntouches the real home. The contract this battery pins (the implementation must use them):\nWARNING_TEXT, RECORD_DIR_PARTS (legacy records only), the record fields `repo_root`, `keys` and\n`digests`, and the get_materialized arguments `project` (a list) and `remote` (a list)."
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project-materialize.py")
TMP_BASE = os.path.expanduser("~/.cache/tmp")

TOKEN = "tok-9f3a1c7e-not-a-real-secret"
WARNING_TEXT = "project scope fetch failed"
RECORD_DIR_PARTS = (".cache", "agent-context", "project-scope")
TIMEOUT_CEILING_SECONDS = 30.0

DEMO_UUID = "11111111-1111-4111-8111-111111111111"
OTHER_UUID = "22222222-2222-4222-8222-222222222222"
OLD_UUID = "33333333-3333-4333-8333-333333333333"
DEMO_REMOTES = {"origin": "git@github.com:org/demo.git", "mirror": "https://example.com/org/demo.git"}

STUB = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"





SITECUSTOMIZE = '''
import ast, json, os, subprocess, sys, time
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import store_mcp

_spawn_log = os.environ.get("SPAWN_LOG")
if _spawn_log:
    _original = subprocess.Popen.__init__
    def _logged(self, args, *a, **k):
        argv = [args] if isinstance(args, (str, bytes)) else [str(x) for x in args]
        with open(_spawn_log, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "argv": argv}) + "\\n")
        _original(self, args, *a, **k)
    subprocess.Popen.__init__ = _logged

_call_log = os.environ.get("CALL_LOG")
_behavior_path = os.environ.get("CALL_BEHAVIOR")


def _fake_call(tool, arguments=None, env=None):
    if _call_log:
        with open(_call_log, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "tool": tool, "arguments": arguments or {}}) + "\\n")
    behavior = {"result": {}}
    if _behavior_path and os.path.exists(_behavior_path):
        with open(_behavior_path) as fh:
            behavior = ast.literal_eval(fh.read())
    if behavior.get("raise") == "ToolError":
        raise store_mcp.ToolError(behavior.get("message", "boom"))
    if behavior.get("raise") == "StoreUnreachable":
        raise store_mcp.StoreUnreachable(behavior.get("message", "unreachable"))
    return behavior["result"]


store_mcp.call = _fake_call
'''

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




def demo_bundle(name: str = "Demo", uuid: str = DEMO_UUID, workspace: str = "ws1",
                gone: bool = True, with_workspace: bool = True) -> dict[str, str]:
    bundle = {
        f"projects/{name}/project.toml": (f'uuid = "{uuid}"\ndisplay_name = "{name}"\n'
                                          f'canonical_remote = "github.com:org/{name.lower()}"\n'
                                          f'workspace = "{workspace}"\n'),
        f"projects/{name}/skills/x/SKILL.md": f"skill x of {name}\n",
        f"projects/{name}/commands/{name.lower()}-cmd.md": f"command of {name}\n",
        f"projects/{name}/scripts/run.py": "#!/usr/bin/env python3\nprint('run')\n",
        f"projects/{name}/scripts/run.py.meta.toml": "executable = true\n",
    }
    if gone:
        bundle[f"projects/{name}/skills/gone/SKILL.md"] = "goes away\n"
    if with_workspace:
        bundle[f"workspaces/{workspace}/workspace.toml"] = f'name = "{workspace}"\n'
        bundle[f"workspaces/{workspace}/skills/w/SKILL.md"] = "workspace skill\n"
    return bundle


def scoped_only(bundle: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in bundle.items() if k.startswith(("projects/", "workspaces/"))}


def git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


class Fixture:
    def __init__(self) -> None:
        os.makedirs(TMP_BASE, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="pmfetch.", dir=TMP_BASE)
        self.home = os.path.join(self.root, "home")
        self.store = os.path.join(self.home, ".agent-context")
        self.hook_dir = os.path.join(self.root, "pyhook")
        self.spawn_log = os.path.join(self.root, "spawns.jsonl")
        self.call_log = os.path.join(self.root, "calls.jsonl")
        self.behavior_path: str | None = None
        os.makedirs(os.path.join(self.home, "tmp"))
        write_text(os.path.join(self.hook_dir, "sitecustomize.py"), SITECUSTOMIZE)
        for stub in ("agents-materialize.py", "lsp-plugin-guard.py", "project-settings-sync.py"):
            write_text(os.path.join(self.store, "global", "scripts", stub), STUB)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def repo(self, name: str = "Demo", *, marker: str | None = DEMO_UUID,
             remotes: dict[str, str] | None = None) -> str:
        path = os.path.join(self.home, "Developer", name)
        os.makedirs(path)
        git("init", "-q", cwd=path)
        for remote_name, url in (DEMO_REMOTES if remotes is None else remotes).items():
            git("remote", "add", remote_name, url, cwd=path)
        if marker:
            write_text(os.path.join(path, ".agents", "project-id"),
                       f'id = "{marker}"\ndisplay_name = "{name}"\n')
        return path

    def seed(self, bundle: dict[str, str]) -> None:
        for key, content in bundle.items():
            write_text(os.path.join(self.store, key), content)

    def env_file(self, text: str) -> None:
        write_text(os.path.join(self.home, ".config", "agent-context", "env"), text)

    def path(self, *parts: str) -> str:
        return os.path.join(self.store, *parts)

    def record_path(self, uuid: str) -> str:
        return os.path.join(self.home, *RECORD_DIR_PARTS, uuid + ".json")

    def snapshot(self) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        for top in (os.path.join(self.store, "projects"), os.path.join(self.store, "workspaces"),
                    os.path.join(self.home, *RECORD_DIR_PARTS)):
            for dirpath, _dirs, files in os.walk(top):
                for name in files:
                    full = os.path.join(dirpath, name)
                    with open(full, "rb") as fh:
                        found[os.path.relpath(full, self.home)] = fh.read()
        return found

    def spawns(self) -> list[dict[str, Any]]:
        try:
            with open(self.spawn_log, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def calls(self) -> list[dict[str, Any]]:
        try:
            with open(self.call_log, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def set_behavior(self, behavior: dict) -> None:
        self.behavior_path = os.path.join(self.root, "behavior.py")
        write_text(self.behavior_path, repr(behavior))


@dataclass
class Result:
    rc: int
    out: str
    err: str
    elapsed: float

    @property
    def text(self) -> str:
        return self.out + self.err


def run_script(fx: Fixture, repo: str, *, relay: str | None) -> Result:
    'relay: "env" (variables), "file" (~/.config/agent-context/env), "no-token", or None.'
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": fx.home,
           "TMPDIR": os.path.join(fx.home, "tmp"), "LANG": "C.UTF-8", "PYTHONPATH": fx.hook_dir,
           "SPAWN_LOG": fx.spawn_log, "CALL_LOG": fx.call_log, "GIT_CONFIG_NOSYSTEM": "1",
           "SCRIPT_DIR": os.path.dirname(SCRIPT)}
    if fx.behavior_path:
        env["CALL_BEHAVIOR"] = fx.behavior_path
    if relay == "env":
        env.update(AGENT_CONTEXT_HOST="127.0.0.1", AGENT_CONTEXT_PORT="1", AGENT_CONTEXT_TOKEN=TOKEN)
    elif relay == "file":
        fx.env_file(f'OTHER_SECRET=nope\nAGENT_CONTEXT_HOST=127.0.0.1\nAGENT_CONTEXT_PORT=1\n'
                    f'AGENT_CONTEXT_TOKEN="{TOKEN}"\n')
    elif relay == "no-token":
        env.update(AGENT_CONTEXT_HOST="127.0.0.1", AGENT_CONTEXT_PORT="1")
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, SCRIPT], cwd=repo, env=env, capture_output=True,
                              text=True, timeout=TIMEOUT_CEILING_SECONDS + 15)
        return Result(proc.returncode, proc.stdout, proc.stderr, time.time() - started)
    except subprocess.TimeoutExpired:
        return Result(-1, "", "TIMEOUT", time.time() - started)


def written_ok(fx: Fixture, bundle: dict[str, str]) -> bool:
    return all(read_text(fx.path(k)) == v for k, v in scoped_only(bundle).items())


def any_written(fx: Fixture, bundle: dict[str, str]) -> list[str]:
    return [k for k in scoped_only(bundle) if os.path.exists(fx.path(k))]


def is_exec(path: str) -> bool:
    return bool(os.stat(path).st_mode & 0o111)


def scoped_files(fx: Fixture) -> list[str]:
    found = []
    for top in ("projects", "workspaces"):
        for dirpath, _dirs, files in os.walk(fx.path(top)):
            found.extend(os.path.join(dirpath, name) for name in files)
    return found


def tmp_leftovers(fx: Fixture) -> list[str]:
    'Anything a run left in its $TMPDIR (the fetched scope tree and the overlay stage).'
    tmp = os.path.join(fx.home, "tmp")
    return sorted(os.listdir(tmp)) if os.path.isdir(tmp) else []


def store_scope_dirs(fx: Fixture) -> list[str]:
    return [d for d in ("projects", "workspaces") if os.path.exists(fx.path(d))]


OVERLAY_FILES = {
    "claude/commands/demo-cmd.md": "command of Demo\n",
    "claude/skills/x/SKILL.md": "skill x of Demo\n",
}
WORKSPACE_OVERLAY = {"claude/skills/w/SKILL.md": "workspace skill\n"}


def overlaid(repo: str, *, workspace: bool = True) -> bool:
    "The demo bundle's commands and skills reached the repo's .agents/, and its script did\n    too with an exec bit."
    want = {**OVERLAY_FILES, **(WORKSPACE_OVERLAY if workspace else {})}
    script = os.path.join(repo, ".agents", "scripts", "run.py")
    return (all(read_text(os.path.join(repo, ".agents", rel)) == text for rel, text in want.items())
            and os.path.isfile(script) and is_exec(script))


def seed_legacy(fx: Fixture, repo_root: str, uuid: str, bundle: dict[str, str]) -> None:
    'What an older project-materialize left: the files under the store and their record.'
    fx.seed(bundle)
    digests = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in bundle.items()}
    write_text(fx.record_path(uuid), json.dumps(
        {"repo_root": repo_root, "keys": sorted(bundle), "digests": digests}, indent=1) + "\n")


def token_leaks(fx: Fixture, results: list[Result]) -> list[str]:
    leaks = [f"output {i}" for i, r in enumerate(results) if TOKEN in r.text]
    for dirpath, _dirs, files in os.walk(fx.home):
        for name in files:
            full = os.path.join(dirpath, name)
            if full == os.path.join(fx.home, ".config", "agent-context", "env"):
                continue
            try:
                with open(full, "rb") as fh:
                    if TOKEN.encode() in fh.read():
                        leaks.append(os.path.relpath(full, fx.home))
            except OSError:
                pass
    if any(TOKEN in json.dumps(s) for s in fx.spawns()):
        leaks.append("subprocess argv")
    if any(TOKEN in json.dumps(c) for c in fx.calls()):
        leaks.append("get_materialized call arguments")
    return leaks


def warning_lines(result: Result) -> list[str]:
    return [ln for ln in result.text.splitlines() if WARNING_TEXT in ln]




def case_fetch_request_and_writes() -> None:
    print("C1 request and writes (relay env from variables)")
    fx = Fixture()
    bundle = {**demo_bundle(), "hooks/evil.sh": "#!/bin/sh\necho evil\n", "docs/x.md": "doc\n",
              "scripts/evil.py": "print('evil')\n", "manifests/mcp-servers.json": "{}\n"}
    fx.set_behavior({"result": scoped_only(bundle)})
    try:
        repo = fx.repo()
        result = run_script(fx, repo, relay="env")
        calls = fx.calls()
        check("exactly one call", len(calls) == 1, f"{len(calls)} calls")
        call = calls[0] if calls else None
        check("tool is get_materialized", call is not None and call["tool"] == "get_materialized",
              str(call))
        args = call["arguments"] if call else {}
        check("project arg is the marker id as a list",
              args.get("project") == [DEMO_UUID], str(args))
        check("remote arg is every git remote URL",
              sorted(args.get("remote", [])) == sorted(DEMO_REMOTES.values()), str(args))
        check("the project's commands, skills, workspace skills and script reach the repo's .agents/",
              overlaid(repo), str({rel: read_text(os.path.join(repo, ".agents", rel))
                                   for rel in (*OVERLAY_FILES, *WORKSPACE_OVERLAY)}))
        check("nothing of the fetched scope is under the store root",
              store_scope_dirs(fx) == [] and not scoped_files(fx), str(store_scope_dirs(fx)))
        check("non-scoped keys are not written",
              not any(os.path.exists(p) for p in (fx.path("hooks", "evil.sh"), fx.path("docs", "x.md"),
                                                  fx.path("shared-docs", "x.md"),
                                                  fx.path("scripts", "evil.py"),
                                                  fx.path("manifests", "mcp-servers.json"),
                                                  os.path.join(fx.home, ".claude", "hooks", "evil.sh"))))
        check("no per-project record is kept",
              not os.path.exists(os.path.join(fx.home, *RECORD_DIR_PARTS)))
        check("the per-run temp tree is deleted", tmp_leftovers(fx) == [], str(tmp_leftovers(fx)))
        check("script continues (rc 0)", result.rc == 0, f"rc={result.rc} {result.text[-200:]}")
        check("token absent from output, files and argv", not token_leaks(fx, [result]),
              str(token_leaks(fx, [result])))
    finally:
        fx.cleanup()


def case_relay_env_file() -> None:
    print("C1 relay env from ~/.config/agent-context/env")
    fx = Fixture()
    fx.set_behavior({"result": scoped_only(demo_bundle())})
    try:
        repo = fx.repo()
        result = run_script(fx, repo, relay="file")
        check("request made", len(fx.calls()) == 1, f"{len(fx.calls())} calls")
        check("project overlaid onto .agents/, nothing under the store root",
              overlaid(repo) and store_scope_dirs(fx) == [], str(store_scope_dirs(fx)))
        check("token absent from output and files", not token_leaks(fx, [result]))
    finally:
        fx.cleanup()


def case_remote_only() -> None:
    print("C1 no marker, remote present")
    fx = Fixture()
    fx.set_behavior({"result": scoped_only(demo_bundle())})
    try:
        run_script(fx, fx.repo(marker=None), relay="env")
        calls = fx.calls()
        call = calls[0] if calls else None
        args = call["arguments"] if call else {}
        check("request has remote args and an empty project list",
              call is not None and args.get("project") == []
              and sorted(args.get("remote", [])) == sorted(DEMO_REMOTES.values()), str(args))
    finally:
        fx.cleanup()


def case_no_fetch(label: str, *, relay: str | None, checkout: str | None = None,
                  marker: str | None = DEMO_UUID, remotes: dict[str, str] | None = None) -> None:
    print("C1 no fetch: " + label)
    fx = Fixture()
    bundle = demo_bundle()
    fx.set_behavior({"result": scoped_only(bundle)})
    try:
        if checkout == "git":
            os.makedirs(fx.path(".git"))
        elif checkout == "server":
            os.makedirs(fx.path("server"))
        if checkout:
            os.makedirs(fx.path("projects"))
        repo = fx.repo(marker=marker, remotes=remotes)
        result = run_script(fx, repo, relay=relay)
        check(label + ": no call", fx.calls() == [], f"{len(fx.calls())} calls")
        check(label + ": nothing written from the bundle", not any_written(fx, bundle),
              str(any_written(fx, bundle)[:3]))
        check(label + ": rc 0", result.rc == 0, f"rc={result.rc}")
        check(label + ": no fetch warning", not warning_lines(result))
    finally:
        fx.cleanup()


def case_no_new_execution_path() -> None:
    print("C4 no subprocess in the fetch step but git")
    fx = Fixture()
    fx.set_behavior({"result": scoped_only(demo_bundle())})
    try:
        run_script(fx, fx.repo(), relay="env")
        calls = fx.calls()
        if not calls:
            check("call observed before judging spawns", False, "no call")
            return
        first_call = calls[0]["t"]
        before = [s for s in fx.spawns() if s["t"] <= first_call]
        odd = [s["argv"] for s in before
               if s["argv"][0] != "git"
               and not (s["argv"][:2] == ["bash", "-c"] and "pwd" in s["argv"][2])]
        check("before the call only git (and the logical-pwd bash) was spawned", not odd, str(odd))
        check("git was used for the remotes lookup",
              any(s["argv"][0] == "git" and ("get-url" in s["argv"] or "remote" in s["argv"])
                  for s in before), str([s["argv"] for s in before]))
    finally:
        fx.cleanup()


FAILURE_CASES: list[tuple[str, dict]] = [
    ("ToolError (e.g. authentication required)",
     {"raise": "ToolError", "message": "authentication required"}),
    ("StoreUnreachable (connection refused)",
     {"raise": "StoreUnreachable", "message": "the daemon is unreachable (ConnectionRefusedError)"}),
    
    
    
    ("result is not a string map", {"result": ["a", "b"]}),
    ("result has an unsafe key with ..",
     {"result": {**scoped_only(demo_bundle()), "projects/Demo/../../../pmfetch-escape.txt": "escaped\n"}}),
    ("result has an unsafe key with an empty segment",
     {"result": {**scoped_only(demo_bundle()), "projects//pmfetch-escape.txt": "escaped\n"}}),
    ("result has an unsafe key with an empty segment at the end",
     {"result": {**scoped_only(demo_bundle()), "projects/Demo/": "escaped\n"}}),
]


def preseed() -> dict[str, str]:
    return {**demo_bundle(gone=False, with_workspace=False),
            "projects/Demo/skills/old/SKILL.md": "old content\n"}


def case_failure(label: str, behavior: dict) -> None:
    print("C3/C5 failure: " + label)
    fx = Fixture()
    fx.set_behavior(behavior)
    try:
        fx.seed(preseed())
        before = fx.snapshot()
        repo = fx.repo()
        kept = os.path.join(repo, ".agents", "claude", "commands", "kept.md")
        write_text(kept, "keep this installed projection\n")
        target_before = read_text(kept)
        result = run_script(fx, repo, relay="env")
        check(label + ": one warning line", len(warning_lines(result)) == 1,
              f"{len(warning_lines(result))} lines: {result.text[-300:]}")
        check(label + ": warning names the script", bool(warning_lines(result)) and all("project-materialize" in ln for ln in warning_lines(result)),
              str(warning_lines(result)))
        check(label + ": existing files untouched", fx.snapshot() == before)
        check(label + ": no key of the response written",
              not os.path.exists(fx.path("projects", "Demo", "skills", "gone", "SKILL.md"))
              and not os.path.isdir(fx.path("workspaces", "ws1"))
              and not os.path.exists(os.path.join(fx.home, "pmfetch-escape.txt"))
              and not os.path.exists(os.path.join(fx.home, "tmp", "pmfetch-escape.txt"))
              and not os.path.exists(os.path.join(fx.root, "pmfetch-escape.txt"))
              and not os.path.exists(fx.path("projects", "pmfetch-escape.txt")))
        check(label + ": nothing overlaid onto .agents/ (the seeded store copy is not read)",
              not os.path.exists(os.path.join(repo, ".agents", "claude", "commands", "demo-cmd.md")))
        check(label + ": installed projection is unchanged", read_text(kept) == target_before)
        check(label + ": no temp tree left", tmp_leftovers(fx) == [], str(tmp_leftovers(fx)))
        check(label + ": fetch failure exits nonzero", result.rc == 1, f"rc={result.rc}")
        check(label + ": token absent from output, files and argv", not token_leaks(fx, [result]),
              str(token_leaks(fx, [result])))
    finally:
        fx.cleanup()


def case_no_store_writes_and_rerun() -> None:
    print("C2 two runs: no record, no store writes, overlay follows the response")
    fx = Fixture()
    try:
        repo = fx.repo()
        fx.set_behavior({"result": scoped_only(demo_bundle())})
        run_script(fx, repo, relay="env")
        check("first run overlaid the skill that will go away",
              os.path.isfile(os.path.join(repo, ".agents", "claude", "skills", "gone", "SKILL.md")))
        fx.set_behavior({"result": scoped_only(demo_bundle(gone=False, with_workspace=False))})
        run_script(fx, repo, relay="env")
        check("second run: store root still has no projects/ or workspaces/",
              store_scope_dirs(fx) == [], str(store_scope_dirs(fx)))
        check("second run: no record dir", not os.path.exists(os.path.join(fx.home, *RECORD_DIR_PARTS)))
        check("second run: no temp tree left", tmp_leftovers(fx) == [], str(tmp_leftovers(fx)))
        check("second run: still-carried files overlaid", overlaid(repo, workspace=False))
    finally:
        fx.cleanup()


def case_checkout_overlay() -> None:
    print("C6 configured relay overrides a local checkout")
    fx = Fixture()
    bundle = demo_bundle()
    fx.set_behavior({"result": scoped_only(bundle)})
    try:
        os.makedirs(fx.path(".git"))
        fx.seed(bundle)
        repo = fx.repo()
        result = run_script(fx, repo, relay="env")
        check("one canonical request", len(fx.calls()) == 1, f"{len(fx.calls())} calls")
        check("overlay built from the relay bundle", overlaid(repo),
              result.text[-300:])
        check("the store's own tree is intact", written_ok(fx, bundle))
        check("no temp tree left", tmp_leftovers(fx) == [], str(tmp_leftovers(fx)))
    finally:
        fx.cleanup()


def case_legacy_retire(offline: bool) -> None:
    print("C7 legacy retirement" + (" (server down)" if offline else ""))
    fx = Fixture()
    demo_only = demo_bundle(gone=False, with_workspace=False)
    fx.set_behavior({"raise": "StoreUnreachable", "message": "unreachable"} if offline
                    else {"result": scoped_only(demo_only)})
    try:
        repo = fx.repo()
        old_repo = fx.repo("Old", marker=OLD_UUID, remotes={"origin": "git@github.com:org/old.git"})
        legacy = demo_bundle()
        seed_legacy(fx, repo, DEMO_UUID, legacy)
        old_bundle = demo_bundle("Old", OLD_UUID, "ws7")
        seed_legacy(fx, old_repo, OLD_UUID, old_bundle)
        shutil.rmtree(old_repo)
        edited = fx.path("projects", "Demo", "skills", "gone", "SKILL.md")
        write_text(edited, "edited by hand\n")
        loose = {"projects/Loose/skills/l/SKILL.md": "not from any record\n"}
        fx.seed(loose)
        before = fx.snapshot()
        run_script(fx, repo, relay="env")
        if offline:
            check("failed fetch preserves legacy scope", fx.snapshot() == before)
            return
        left = sorted(os.path.relpath(p, fx.store) for p in scoped_files(fx))
        expected = sorted(["projects/Demo/skills/gone/SKILL.md", "projects/Loose/skills/l/SKILL.md"])
        check("files whose bytes match the record digest are deleted; the hand-edited file and the "
              "file with no record stay", left == expected, str(left))
        check("hand-edited file keeps its bytes", read_text(edited) == "edited by hand\n")
        check("the other project's files are retired even though its repo is gone",
              not any_written(fx, old_bundle), str(any_written(fx, old_bundle)[:3]))
        check("emptied workspaces/ is removed", not os.path.exists(fx.path("workspaces")))
        check("both records are removed",
              not os.path.exists(fx.record_path(DEMO_UUID)) and not os.path.exists(fx.record_path(OLD_UUID)))
    finally:
        fx.cleanup()
    fx = Fixture()
    try:
        repo = fx.repo()
        seed_legacy(fx, repo, DEMO_UUID, demo_bundle())
        fx.set_behavior({"raise": "StoreUnreachable", "message": "unreachable"})
        run_script(fx, repo, relay="env")
        check("failed fetch preserves recorded projects/ and workspaces/", store_scope_dirs(fx) != [],
              str(store_scope_dirs(fx)))
    finally:
        fx.cleanup()
    print("C7 no relay env: the legacy copy the machine still reads is kept")
    fx = Fixture()
    try:
        repo = fx.repo()
        bundle = demo_bundle()
        seed_legacy(fx, repo, DEMO_UUID, bundle)
        run_script(fx, repo, relay=None)
        check("legacy files stay", written_ok(fx, bundle), str(any_written(fx, bundle)[:3]))
        check("record stays", os.path.exists(fx.record_path(DEMO_UUID)))
    finally:
        fx.cleanup()


def case_ds_store_sweep() -> None:
    print("C8 .DS_Store leftovers")
    ds = ".DS_Store"
    fx = Fixture()
    try:
        repo = fx.repo()
        seed_legacy(fx, repo, DEMO_UUID, demo_bundle())
        for rel in ("", "projects", "projects/Demo", "projects/Demo/skills", "workspaces", "workspaces/ws1"):
            write_text(fx.path(rel, ds), "finder\n")
        fx.set_behavior({"raise": "StoreUnreachable", "message": "unreachable"})
        run_script(fx, repo, relay="env")
        check("D1: failed fetch leaves legacy dirs and .DS_Store alone", store_scope_dirs(fx) != [],
              str(store_scope_dirs(fx)))
        check("D1: the store root and its own .DS_Store stay",
              os.path.isdir(fx.store) and os.path.isfile(fx.path(ds)))
    finally:
        fx.cleanup()
    fx = Fixture()
    try:
        repo = fx.repo()
        for rel in ("projects", "projects/Demo", "projects/Demo/skills", "workspaces"):
            write_text(fx.path(rel, ds), "finder\n")
        fx.set_behavior({"raise": "StoreUnreachable", "message": "unreachable"})
        run_script(fx, repo, relay="env")
        check("D1b: failed fetch leaves unrecorded leftovers", store_scope_dirs(fx) != [], str(store_scope_dirs(fx)))
    finally:
        fx.cleanup()
    fx = Fixture()
    try:
        repo = fx.repo()
        seed_legacy(fx, repo, DEMO_UUID, demo_bundle())
        write_text(fx.path("projects", "Loose", "skills", "l", "SKILL.md"), "not from any record\n")
        for rel in ("projects", "projects/Loose", "projects/Loose/skills", "workspaces", "workspaces/ws1"):
            write_text(fx.path(rel, ds), "finder\n")
        fx.set_behavior({"raise": "StoreUnreachable", "message": "unreachable"})
        run_script(fx, repo, relay="env")
        check("D2: the dir with a real file keeps its file and every .DS_Store above it",
              os.path.isfile(fx.path("projects", "Loose", "skills", "l", "SKILL.md"))
              and all(os.path.isfile(fx.path(rel, ds)) for rel in ("projects", "projects/Loose", "projects/Loose/skills")))
        check("D2: failed fetch leaves the .DS_Store-only tree", os.path.exists(fx.path("workspaces")),
              str(os.path.exists(fx.path("workspaces"))))
    finally:
        fx.cleanup()
    fx = Fixture()
    try:
        repo = fx.repo()
        outside = os.path.join(fx.root, "outside")
        write_text(os.path.join(outside, ds), "outside finder\n")
        write_text(os.path.join(outside, "keep.txt"), "outside file\n")
        os.makedirs(fx.path("projects", "Demo"))
        os.symlink(os.path.join(outside, ds), fx.path("projects", "Demo", ds))
        os.symlink(outside, fx.path("projects", "link"))
        fx.set_behavior({"raise": "StoreUnreachable", "message": "unreachable"})
        run_script(fx, repo, relay="env")
        check("D3: nothing outside the store is deleted",
              os.path.isfile(os.path.join(outside, ds)) and os.path.isfile(os.path.join(outside, "keep.txt")))
        check("D3: the symlinked .DS_Store and symlinked dir stay where they are",
              os.path.islink(fx.path("projects", "Demo", ds)) and os.path.islink(fx.path("projects", "link")))
    finally:
        fx.cleanup()
    fx = Fixture()
    try:
        repo = fx.repo()
        write_text(fx.path("projects", ds), "finder\n")
        run_script(fx, repo, relay=None)
        check("D4: no relay env leaves a .DS_Store-only root alone", os.path.isfile(fx.path("projects", ds)))
    finally:
        fx.cleanup()


def main() -> int:
    case_fetch_request_and_writes()
    case_relay_env_file()
    case_remote_only()
    case_no_fetch("no relay env at all", relay=None)
    case_no_fetch("host and port but no token", relay="no-token")
    case_no_fetch("no marker and no remote", relay="env", marker=None, remotes={})
    case_no_new_execution_path()
    for label, behavior in FAILURE_CASES:
        case_failure(label, behavior)
    case_no_store_writes_and_rerun()
    case_checkout_overlay()
    case_legacy_retire(offline=False)
    case_legacy_retire(offline=True)
    case_ds_store_sweep()
    total = passed + len(failures)
    print(f"\n{passed}/{total} passed")
    for label in failures:
        print("FAILED: " + label)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
