#!/usr/bin/env python3
"test-lock: lock acceptance tests so the code has to meet them.\n\nThe test-first-delivery skill writes a task's tests, runs them to watch them fail,\nthen locks them here. After that, block-locked-test-edit refuses edits to a locked\nfile, and locked-test-drift-gate refuses to end a turn while a locked file differs\nfrom its recorded hash. A lock comes off only when user approves an unlock question\n(approval-question or codex-test-unlock runs test-lock-consent.py).\n\nUsage:\n  test-lock.py lock <file>...    record the sha256 of each file. A .py file is\n                                 typechecked with basedpyright first; errors\n                                 refuse that file (others in the call still lock)\n  test-lock.py status [<path>]   list the locks for the checkout holding <path>\n                                 (default: the current directory). Exit 1 when a\n                                 locked file changed or is missing.\n  test-lock.py publish           turn this host's store locks from before records\n                                 existed into store records (one-time migration)\n  test-lock.py check <path>      exit 2 and the lock's locked_at stamp when <path>\n                                 is locked, else 0 (block-locked-test-edit's own\n                                 live-store check, through store_task.run)\n  test-lock.py -h | --help\n\nOne manifest per checkout, in $XDG_STATE_HOME/agent-context/test-locks/. A lock\nwhose checkout no longer exists is ignored, so removing a worktree clears its locks."

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_task  

STATE_DIR = os.environ.get("TEST_LOCK_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME")
    or os.path.join(os.path.expanduser("~"), ".local", "state"),
    "agent-context", "test-locks")


def file_sha256(path):
    'Hex sha256 of a file, or None when it cannot be read.'
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def checkout_root(path):
    'Realpath of the git checkout holding `path`, or None.'
    d = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
    try:
        out = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return os.path.realpath(out.stdout.strip())


def manifest_path(root):
    key = hashlib.sha1(root.encode("utf-8")).hexdigest()[:16]
    return os.path.join(STATE_DIR, key + ".json")


def store_root():
    'Realpath of the agent-context store, whose locks are records inside it, or None.'
    path = (os.environ.get("TEST_LOCK_STORE_ROOT") or os.environ.get("AGENT_CONTEXT_STORE")
            or os.path.join(os.path.expanduser("~"), ".agent-context"))
    path = os.path.realpath(path)
    return path if os.path.isdir(path) else None


def records_dir(root):
    return os.path.join(root, "global", "test-locks")


def record_name(rel):
    'One file name per path, reversibly: `_` is escaped first, so test__lock.py and\n    test/lock.py can never flatten to the same record.'
    return rel.replace("_", "_u").replace(os.sep, "__") + ".json"


def valid_rel(rel):
    'A record path must stay inside the store: relative, no `..`, no NUL.'
    if not isinstance(rel, str) or not rel or "\x00" in rel or os.path.isabs(rel):
        return False
    norm = os.path.normpath(rel)
    return norm == rel and norm != ".." and not norm.startswith(".." + os.sep)





_RECORDS = {}


def read_records(root):
    '([(file name, record)] well-formed, [(file name, problem)] not).'
    try:
        stamp = os.stat(records_dir(root)).st_mtime_ns
    except OSError:
        stamp = None
    hit = _RECORDS.get(root)
    if stamp is not None and hit is not None and hit[0] == stamp:
        return hit[1]
    result = _read_records(root)
    if stamp is not None:
        _RECORDS[root] = (stamp, result)
    return result


def _read_records(root):
    good, bad = [], []
    try:
        names = sorted(os.listdir(records_dir(root)))
    except OSError:
        return good, bad
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(records_dir(root), name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            bad.append((name, "corrupt"))
            continue
        if not (isinstance(rec, dict) and isinstance(rec.get("sha256"), str)
                and isinstance(rec.get("path"), str)):
            bad.append((name, "corrupt"))
        elif not valid_rel(rec["path"]):
            bad.append((name, "invalid"))
        else:
            good.append((name, rec))
    return good, bad


def load_records(root):
    "{relative path: entry} from the store's well-formed lock records. A corrupt or\n    escaping record locks nothing; `status` reports it."
    return {rec["path"]: {"sha256": rec["sha256"], "locked_at": rec.get("locked_at") or ""}
            for _name, rec in read_records(root)[0]}


def save_records(root, files):
    "Make the store's records match `files`: write what changed, delete a well-formed\n    record whose lock is gone or whose name is stale. A corrupt or escaping record is left\n    for `status` to report, never deleted as a side effect."
    d = records_dir(root)
    os.makedirs(d, exist_ok=True)
    wanted = {record_name(rel): rel for rel in files if valid_rel(rel)}
    for name, rel in sorted(wanted.items()):
        entry = files[rel] if isinstance(files[rel], dict) else {}
        body = {"path": rel, "sha256": entry.get("sha256"),
                "locked_at": entry.get("locked_at") or ""}
        target = os.path.join(d, name)
        try:
            with open(target, encoding="utf-8") as fh:
                if json.load(fh) == body:
                    continue
        except (OSError, ValueError):
            pass
        tmp = "%s.tmp.%d" % (target, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    for name, _rec in read_records(root)[0]:
        if name not in wanted:
            os.remove(os.path.join(d, name))


def load_host(root):
    "This host's manifest for one checkout. A missing or corrupt one reads as empty."
    try:
        with open(manifest_path(root), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {"root": root, "files": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), dict):
        return {"root": root, "files": {}}
    doc["root"] = root
    return doc


def save_host(doc):
    "Write this host's manifest atomically."
    os.makedirs(STATE_DIR, exist_ok=True)
    target = manifest_path(doc["root"])
    tmp = "%s.tmp.%d" % (target, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, target)


def forget_legacy(root, rels):
    'Drop host-manifest store locks that now have records. An empty manifest is deleted.'
    doc = load_host(root)
    gone = [rel for rel in rels if rel in doc["files"]]
    if not gone:
        return
    for rel in gone:
        del doc["files"][rel]
    if doc["files"]:
        save_host(doc)
    else:
        try:
            os.remove(manifest_path(root))
        except OSError:
            pass


def store_git_dir():
    store = store_root()
    return os.path.realpath(os.path.join(store, ".git")) if store else None


def store_worktrees():
    'store worktrees.'
    gdir = store_git_dir()
    store = store_root()
    legacy = os.path.join(store, ".claude", "worktrees") + os.sep if store else None
    found = []
    try:
        names = sorted(os.listdir(os.path.join(gdir or "", "worktrees")))
    except OSError:
        return found
    for name in names:
        try:
            with open(os.path.join(gdir or "", "worktrees", name, "gitdir"),
                      encoding="utf-8") as fh:
                pointer = fh.read().strip()
        except OSError:
            continue
        root = os.path.realpath(os.path.dirname(pointer))
        if not pointer or not os.path.isdir(root) or root in found:
            continue
        if legacy and (root + os.sep).startswith(legacy):
            continue
        found.append(root)
    return found


def is_store_worktree(root):
    return root in store_worktrees()


def load(root):
    "The lock state for one checkout: records for the store and for each of its\n    worktrees, this host's manifest for any other repo."
    if root == store_root() or is_store_worktree(root):
        return {"root": root, "files": load_records(root), "shared": True}
    return load_host(root)


def save(doc):
    "Persist lock state: records for a store checkout, this host's manifest otherwise.\n    Records go to `records_root` when the doc sets one (a worktree path bound by main's\n    records), else to the doc's own root."
    if doc.get("shared"):
        save_records(doc.get("records_root") or doc["root"], doc["files"])
        return
    save_host(doc)


def worktree_records():
    "One lock set per store worktree that holds records of its own. Not part of\n    manifests(): the drift gate judges only the store's own records."
    store = store_root()
    found = []
    for root in store_worktrees():
        files = load_records(root)
        if files and root != store:
            found.append({"root": root, "files": files, "shared": True})
    return found


def containing_worktree(p):
    'containing worktree.'
    store = store_root()
    if not store:
        return None
    for root in store_worktrees():
        if p.startswith(root + os.sep):
            return root
    base = os.path.join(store, ".agents", "worktrees") + os.sep
    if p.startswith(base):
        name = p[len(base):].partition(os.sep)[0]
        if name:
            return base + name
    return None


def local_manifests():
    'local manifests.'
    store = store_root()
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        names = []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(STATE_DIR, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if (isinstance(doc, dict) and isinstance(doc.get("files"), dict)
                and doc["files"] and os.path.isdir(doc.get("root") or "")
                and os.path.realpath(doc.get("root") or "") != store):
            found.append(doc)
    return found


def manifests():
    "Every lock set that holds a lock and whose checkout still exists.\n\n    The store's records count on every host. A host manifest for the store itself is a\n    lock from before records existed; it is migrated by `publish`, never enforced."
    found = local_manifests()
    store = store_root()
    if store:
        files = load_records(store)
        if files:
            found.append({"root": store, "files": files, "shared": True})
    return found


STORE_DIR = ".agent-context"


def real(path, cwd=None):
    "Realpath of a path that may not exist yet. Another host's spelling of a store file\n    reads as this host's (see local_spelling)."
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    p = local_spelling(os.path.normpath(p))
    parent, base = os.path.split(p)
    return os.path.join(os.path.realpath(parent), base)


def local_spelling(p):
    "`p` on this host when it names a file in ANOTHER host's agent-context store, else `p`."
    
    if p.endswith(os.sep + STORE_DIR) and not os.path.lexists(p):
        return store_root() or p
    marker = os.sep + STORE_DIR + os.sep
    if marker not in p:
        return p
    head, key = p.split(marker, 1)
    if os.path.exists(head + os.sep + STORE_DIR) or not valid_rel(key):
        return p
    local = store_root()
    return os.path.join(local, key) if local else p


def in_live_store(path):
    'in live store.'
    live = store_task.live_store()
    p = real(path)
    if p != live and not p.startswith(live + os.sep):
        return False
    root = checkout_root(p)
    return root is None or root == live


def store_is_remote():
    'True on a relay machine: the live store here is a projection with no lock records,\n    so every lock question about it goes to the daemon.'
    return not os.path.exists(os.path.join(store_task.live_store(), ".git"))


def asks_daemon(path):
    "Whether a check of `path` must be answered by the daemon, not this host's files."
    return (not store_task.in_server()
            and store_task.targets_live_store(os.environ.get("TEST_LOCK_STORE_ROOT"))
            and store_is_remote() and in_live_store(path))


def is_locked(path):
    "True when `path` is locked, False when it is not. Raises store_task's\n    StoreUnreachable or ToolError when the daemon must answer and cannot: a caller deciding\n    whether an unlock may happen refuses then, never reading silence as unlocked."
    if not asks_daemon(path):
        return locked_entry(path) is not None
    result = store_task.forward("test-lock", ["check", path])
    code = int(result.get("exit") or 0)
    if code not in (0, 2):
        raise store_task.store_mcp.ToolError("test-lock check exited %d: %s" % (
            code, (result.get("stderr") or "").strip()))
    return code == 2


def resolve_root(path, cwd=None):
    "(root, store-relative key) for the store root or one of its worktrees that contains\n    `path` (L7); (None, None) when it names neither. `real()` resolves symlinks in every\n    parent directory (not a final one) before this compares against the already realpath'd\n    store and worktree roots, so a `..` segment or a symlinked parent cannot resolve to a key\n    it should not: `real(path)` no longer starts with any of those roots."
    p = real(path, cwd)
    store = store_root()
    
    
    
    
    for root in store_worktrees() + ([store] if store else []):
        if p.startswith(root + os.sep):
            return root, os.path.relpath(p, root)
    return None, None


def canonical_key(path, cwd=None):
    'The store-relative key `path` names; see resolve_root.'
    return resolve_root(path, cwd)[1]


def bound_docs(path, cwd=None):
    '(key, [(doc, rel)]) for every record bound to the key `path` names (L2): the store\'s\n    own record if it has one, and -- scoped to what `path` itself could mean -- either the ONE\n    worktree `path` is inside (a worktree path never reaches a sibling worktree\'s unrelated\n    lock on a file at the same relative name), or, when `path` is a plain store path with no\n    single worktree to prefer, every worktree\'s own record for that key (an unlock from main\n    must be able to reach whichever worktree independently holds the same lock, since main\n    alone cannot say which one). ("None", []) when `path` names no key (L7) or nothing binds\n    the key it names.'
    root, key = resolve_root(path, cwd)
    if key is None:
        return None, []
    out = []
    store = store_root()
    if store:
        main_files = load_records(store)
        if key in main_files:
            out.append(({"root": store, "files": main_files, "shared": True}, key))
    for doc in worktree_records():
        if key in doc["files"] and (root == store or doc["root"] == root):
            out.append((doc, key))
    return key, out


def locked_entry(path, cwd=None):
    "(manifest, relative path) when `path` is locked, else None. For a store or worktree\n    path this is the union bound_docs() computes, preferring the record whose own root is\n    the one `path` resolves into (so the drift gate keeps judging the store itself even\n    when a worktree's copy is what an edit hook was asked about). A worktree's root is a\n    path under the store's own tree, so a startswith check against `path` would find the\n    store root too, and always match it first; comparing against resolve_root's own answer\n    avoids that. For any other path (an ordinary, non-store checkout) this is that\n    checkout's own host manifest, exactly as before. Runs no git beyond what\n    store_root/store_worktrees already do."
    p = real(path, cwd)
    hit_root, _key = resolve_root(path, cwd)
    _key2, hits = bound_docs(path, cwd)
    if hits:
        same_root = next((h for h in hits if h[0]["root"] == hit_root), None)
        return same_root or hits[0]
    for doc in manifests():
        if doc.get("shared"):
            continue
        root = doc["root"]
        if p.startswith(root + os.sep):
            rel = os.path.relpath(p, root)
            if rel in doc["files"]:
                return doc, rel
    
    
    if os.path.exists(p):
        for doc in manifests():
            if doc.get("shared"):
                continue
            for rel in doc["files"]:
                q = os.path.join(doc["root"], rel)
                try:
                    if os.path.exists(q) and os.path.samefile(p, q):
                        return doc, rel
                except OSError:
                    continue
    return None


def drift(doc):
    '[(relative path, "changed" | "missing")] for one manifest.'
    out = []
    for rel, entry in sorted(doc["files"].items()):
        got = file_sha256(os.path.join(doc["root"], rel))
        if got is None:
            out.append((rel, "missing"))
        elif not isinstance(entry, dict) or got != entry.get("sha256"):
            out.append((rel, "changed"))
    return out


def resolve_basedpyright():
    'Path to basedpyright: PATH first, then ~/.local/bin/basedpyright. None if absent.'
    found = shutil.which("basedpyright")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/basedpyright")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def _format_diag(d):
    start = (d.get("range") or {}).get("start") or {}
    line = start.get("line", 0) + 1
    col = start.get("character", 0) + 1
    rule = d.get("rule")
    suffix = " (%s)" % rule if rule else ""
    return "  %s:%d:%d - %s: %s%s" % (
        d.get("file", ""), line, col, d.get("severity", "error"), d.get("message", ""), suffix)


def _usable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def server_python(root):
    "The interpreter basedpyright should use for a store checkout that has no server/.venv.\n\n    pyrightconfig.json points at server/.venv, which only the main checkout has, so a test\n    typechecked from a worktree could not resolve pytest or agent_context and was refused. The\n    workaround was an untracked server/.venv symlink, which then blocked store-wt-finish. Now\n    the worktree borrows, in order: the main checkout's server/.venv, then the machine venv\n    (AGENT_CONTEXT_VENVS, default ~/.local/share/agent-context/venvs, subdirectory `server`).\n    None when `root` has its own venv (the config finds it), is not the store's server tree,\n    or no venv exists."
    if not os.path.isfile(os.path.join(root, "server", "pyproject.toml")):
        return None
    if _usable(os.path.join(root, "server", ".venv", "bin", "python")):
        return None
    candidates = []
    try:
        common = subprocess.run(["git", "-C", root, "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if common:
        main = os.path.dirname(os.path.realpath(os.path.join(root, common)))
        candidates.append(os.path.join(main, "server", ".venv", "bin", "python"))
    venvs = os.environ.get("AGENT_CONTEXT_VENVS") or os.path.expanduser(
        "~/.local/share/agent-context/venvs")
    candidates.append(os.path.join(venvs, "server", "bin", "python"))
    return next((c for c in candidates if _usable(c)), None)


def run_basedpyright(tool, path, root, python=None):
    "Typecheck one file with basedpyright, run from `root` so its\n    pyrightconfig.json applies. `python` names the interpreter to resolve imports\n    against (server_python). A dict with:\n      status: 'ok' | 'warnings' | 'errors' | 'crash' | 'timeout'\n      lines:  formatted diagnostic lines, for 'warnings' and 'errors'\n      reason: a one-line cause, for 'crash' and 'timeout'"
    argv = [tool, "--outputjson"] + (["--pythonpath", python] if python else []) + [path]
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": "basedpyright timed out after 60s"}
    except OSError as exc:
        return {"status": "crash", "reason": "basedpyright failed to run: %s" % exc}
    try:
        doc = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        return {"status": "crash",
                "reason": "basedpyright crashed (exit %d): %s" % (proc.returncode, detail)}
    diags = doc.get("generalDiagnostics") or []
    errors = [d for d in diags if d.get("severity") == "error"]
    warnings = [d for d in diags if d.get("severity") == "warning"]
    if errors:
        return {"status": "errors", "lines": [_format_diag(d) for d in errors]}
    if warnings:
        return {"status": "warnings", "lines": [_format_diag(d) for d in warnings]}
    return {"status": "ok"}


def cmd_lock(files):
    if not files:
        sys.stderr.write("test-lock: lock needs at least one file\n")
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    docs, refused, candidates = {}, [], []
    for f in files:
        p = real(f)
        if not os.path.isfile(p):
            sys.stderr.write("test-lock: not a file: %s\n" % f)
            return 2
        root = checkout_root(p)
        if not root:
            sys.stderr.write("test-lock: not inside a git checkout: %s\n" % f)
            return 2
        doc = docs.setdefault(root, load(root))
        rel = os.path.relpath(p, root)
        sha = file_sha256(p)
        entry = doc["files"].get(rel)
        legacy = None
        if entry is None and doc.get("shared"):
            
            
            legacy = load_host(root)["files"].get(rel)
            entry = legacy
        if isinstance(entry, dict) and entry.get("sha256") != sha:
            refused.append(rel)
        else:
            candidates.append((f, p, root, rel, sha, rel in doc["files"], legacy))
    if refused:
        sys.stderr.write(
            "test-lock: refused, nothing was written. These files changed after they "
            "were locked:\n%s\n"
            "Re-locking would accept the change the lock exists to stop. Edit each file "
            "back to its locked content, or ask user to approve an unlock with one "
            "structured question (Claude: AskUserQuestion; Codex: request_user_input_async):\n"
            "    Unlock the locked test <absolute path>? [approval:test-unlock:<absolute path>:0]\n"
            % "\n".join("  " + r for r in refused))
        return 3

    tool, tool_checked = None, False
    added, kept, type_refused = [], [], []
    for f, p, root, rel, sha, already, legacy in candidates:
        
        
        if p.endswith(".py") and not already and legacy is None:
            if not tool_checked:
                tool_checked = True
                tool = resolve_basedpyright()
                if tool is None:
                    sys.stderr.write(
                        "test-lock: basedpyright not found on PATH or "
                        "~/.local/bin/basedpyright; locking without a type check\n")
            if tool is not None:
                result = run_basedpyright(tool, p, root, server_python(root))
                if result["status"] in ("errors", "crash", "timeout"):
                    type_refused.append((f, result))
                    continue
                if result["status"] == "warnings":
                    sys.stderr.write(
                        "test-lock: basedpyright warnings in %s:\n%s\n"
                        % (f, "\n".join(result["lines"])))
        doc = docs[root]
        if already:
            kept.append(rel)
        else:
            when = legacy.get("locked_at") if isinstance(legacy, dict) else None
            doc["files"][rel] = {"sha256": sha, "locked_at": when or stamp}
            added.append(rel)
    for doc in docs.values():
        save(doc)
        if doc.get("shared"):
            forget_legacy(doc["root"], list(doc["files"]))
    for rel in added:
        print("locked   %s" % rel)
    for rel in kept:
        print("already  %s" % rel)
    if type_refused:
        for f, result in type_refused:
            if result["status"] == "errors":
                sys.stderr.write(
                    "test-lock: refused %s, basedpyright reports errors:\n%s\n"
                    "fix these, then lock\n" % (f, "\n".join(result["lines"])))
            else:
                sys.stderr.write(
                    "test-lock: refused %s, %s\nfix these, then lock\n"
                    % (f, result["reason"]))
        return 3
    return 0


def cmd_status(path):
    root = checkout_root(real(path or "."))
    if not root:
        sys.stderr.write("test-lock: not inside a git checkout: %s\n" % (path or "."))
        return 2
    doc = load(root)
    problems = read_records(root)[1] if doc.get("shared") else []
    for name, why in problems:
        print("%-8s %s (a record that locks nothing)"
              % (why, os.path.join("global", "test-locks", name)))
    if not doc["files"]:
        if not problems:
            print("No locked tests in %s" % root)
        return 1 if problems else 0
    bad = dict(drift(doc))
    for rel in sorted(doc["files"]):
        print("%-8s %s" % (bad.get(rel, "ok"), rel))
    return 1 if bad or problems else 0


def cmd_check(path):
    'cmd check.'
    hit = locked_entry(path)
    if not hit:
        return 0
    doc, rel = hit
    entry = doc["files"].get(rel)
    stamp = entry.get("locked_at", "") if isinstance(entry, dict) else ""
    print("%s\t%s" % (rel, stamp))
    return 2


def cmd_publish():
    'Lock again every store test this host locked before records existed.'
    root = store_root()
    if not root:
        sys.stderr.write("test-lock: no agent-context store found\n")
        return 2
    legacy = load_host(root)["files"]
    if not legacy:
        print("No host-only store locks to publish.")
        return 0
    paths, gone = [], []
    for rel in sorted(legacy):
        if os.path.isfile(os.path.join(root, rel)):
            paths.append(os.path.join(root, rel))
        else:
            gone.append(rel)
    for rel in gone:
        sys.stderr.write("test-lock: not published, the locked file is gone: %s\n" % rel)
    rc = cmd_lock(paths) if paths else 0
    return rc or (1 if gone else 0)


def _forwards(cmd, rest):
    ' forwards.'
    if not store_task.targets_live_store(os.environ.get("TEST_LOCK_STORE_ROOT")):
        return False
    if cmd == "publish":
        return True
    
    
    if cmd == "lock":
        return any(in_live_store(f) for f in rest)
    if cmd == "status":
        return in_live_store(rest[0] if rest else ".")
    if cmd == "check":
        return bool(rest) and asks_daemon(rest[0])
    return False


def _forward(argv):
    "Forward this invocation to the daemon's test-lock task; print and exit as if it\n    had run here."
    try:
        result = store_task.forward("test-lock", argv[1:])
    except (store_task.store_mcp.StoreUnreachable, store_task.store_mcp.ToolError) as exc:
        sys.stderr.write("test-lock: the store did not run this task (%s)\n" % exc)
        return store_task.UNREACHABLE_EXIT
    sys.stdout.write(result.get("stdout") or "")
    sys.stderr.write(result.get("stderr") or "")
    sys.stdout.flush()
    return int(result.get("exit") or 0)


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print((__doc__ or "").strip())
        return 0
    cmd, rest = args[0], args[1:]
    flags = [a for a in rest if a.startswith("-")]
    if flags:
        sys.stderr.write("test-lock: unknown flag %s (see --help)\n" % flags[0])
        return 2
    if cmd == "lock":
        if not store_task.in_server() and _forwards(cmd, rest):
            return _forward(argv)
        return cmd_lock(rest)
    if cmd == "publish":
        if rest:
            sys.stderr.write("test-lock: publish takes no arguments\n")
            return 2
        if not store_task.in_server() and _forwards(cmd, rest):
            return _forward(argv)
        return cmd_publish()
    if cmd == "status":
        if len(rest) > 1:
            sys.stderr.write("test-lock: status takes at most one path\n")
            return 2
        if not store_task.in_server() and _forwards(cmd, rest):
            return _forward(argv)
        return cmd_status(rest[0] if rest else None)
    if cmd == "check":
        if len(rest) != 1:
            sys.stderr.write("test-lock: check needs exactly one path\n")
            return 2
        if _forwards(cmd, rest):
            return _forward(argv)
        return cmd_check(rest[0])
    sys.stderr.write("test-lock: unknown command %s (see --help)\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
