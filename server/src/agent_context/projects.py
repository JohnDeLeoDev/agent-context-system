
'Project records, resolution and the machine-agnostic project marker.\n\nSplit out of fstools.py (build 25); `fstools` re-exports everything so callers and\ntests keep importing `fstools as T`.'
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from . import dryrun, identity, write_guard
from . import project_resolve as PR
from .paths import write_atomic
from .project_resolve import (
    normalize_remote,
)

_log = logging.getLogger(__name__)


def list_projects(store, workspace=None):
    out = []
    for e in store.entities.values():
        if e["type"] != "project":
            continue
        if workspace and e.get("workspace") != workspace:
            continue
        out.append({"id": e["uuid"], "display_name": e.get("display_name"),
                    "canonical_remote": e.get("canonical_remote"), "stack": e.get("stack"),
                    "integration_branch": e.get("integration_branch"), "workspace": e.get("workspace")})
    return out


def _find_project_by_remote(store, remote_norm):
    if not remote_norm:
        return None
    for e in store.entities.values():
        if e["type"] == "project" and (
                e.get("canonical_remote") == remote_norm
                
                
                or normalize_remote(str(e.get("canonical_remote") or "")) == remote_norm):
            return e
    
    tail = remote_norm.split(":")[-1]
    for e in store.entities.values():
        if e["type"] == "project" and str(e.get("canonical_remote", "")).split(":")[-1] == tail:
            return e
    return None








_MARKER_REL = os.path.join(".agents", "project-id")
_MARKER_HEADER = (
    "# agent-context project marker — machine-agnostic project identity.\n"
    "# `id` is the stable project UUID; resolve_project prefers this over the git\n"
    "# remote, so mirrors, renamed remotes, and linked worktrees all resolve alike.\n")


def _read_project_marker(repo_root):
    if not repo_root:
        return None
    p = os.path.join(repo_root, _MARKER_REL)
    if not os.path.isfile(p):
        return None
    from .store import parse_toml
    try:
        d = parse_toml(open(p).read())
    except Exception:
        return None
    return d if d.get("id") else None


def _write_project_marker(repo_root, project_id, display_name):
    "Idempotently write .agents/project-id at repo_root. Never writes outside it;\n    returns True on success/no-op, False if it couldn't (missing/unwritable root).\n    During a dry run it writes nothing and reports the write as if it had happened: the\n    marker lives in a checkout, outside the store copy the dry run writes to."
    if not repo_root or not os.path.isdir(repo_root) or not project_id:
        return False
    if dryrun.active():
        return True
    from .store import emit_toml
    try:
        content = _MARKER_HEADER + emit_toml({"id": project_id, "display_name": display_name})
        d = os.path.join(repo_root, ".agents")
        p = os.path.join(d, "project-id")
        if os.path.isfile(p) and open(p).read() == content:
            return True  
        
        
        write_atomic(p, content)
        return True
    except OSError:
        return False


def _find_project_by_id(store, pid):
    if not pid:
        return None
    e = store.entities.get(pid)
    if e and e.get("type") == "project":
        return e
    return next((e for e in store.entities.values()
                 if e.get("type") == "project" and e.get("uuid") == pid), None)


def resolve_project(store, cwd, evidence=None):
    'The project `cwd` belongs to.\n\n    `evidence` is what a remote relay read from ITS disk for this same cwd\n    (`{cwd, marker_id, remotes}`, see identity.project_evidence): this process cannot\n    see that disk, so the evidence stands in for the marker and the git remotes, and\n    nothing is read from or written to the local filesystem. Evidence for another cwd\n    is ignored.'
    from .project_resolve import get_repo_root
    ev = evidence if evidence and evidence.get("cwd") == cwd else None
    if ev is None and identity.current() is not None:
        
        
        return {"error": f"No project found for path: {cwd} (no project evidence from the relay)"}
    remote_caller = ev is not None
    repo_root = None if remote_caller else get_repo_root(cwd)
    via, e = None, None

    
    marker = ({"id": ev.get("marker_id")} if ev and ev.get("marker_id")
              else None if ev else _read_project_marker(repo_root))
    if marker:
        e = _find_project_by_id(store, marker.get("id"))
        if e:
            via = "marker"

    
    
    
    if not e:
        raws = (list(ev.get("remotes") or []) if ev else
                PR.get_git_remotes(cwd) or ([r] if (r := PR.get_git_remote(cwd)) else []))
        for raw in raws:
            e = _find_project_by_remote(store, normalize_remote(raw))
            if e:
                via = "remote"
                break

    if not e:
        return {"error": f"No project found for path: {cwd}"}

    
    
    
    marker_written = False
    if via == "remote" and marker is None and repo_root:
        marker_written = _write_project_marker(repo_root, e["uuid"], e.get("display_name"))

    res = {"id": e["uuid"], "display_name": e.get("display_name"),
           "canonical_remote": e.get("canonical_remote"), "stack": e.get("stack"),
           "integration_branch": e.get("integration_branch"), "workspace": e.get("workspace"),
           "branch": None if remote_caller else PR.get_git_branch(cwd),
           "paths": [{"local_path": cwd if remote_caller else str(Path(cwd).resolve()),
                      "is_primary": 1}],
           "resolved_via": via}
    if marker_written:
        res["marker_written"] = True
    return res


def resolve_workspace_root(store, cwd, max_checkouts=50):
    "The workspace a directory stands for when it holds checkouts and is not one itself.\n\n    A directory that is the parent of several project checkouts resolves no project, so\n    a session there would load none of the workspace's instructions or memories. The\n    file store keeps no per-machine paths, so the directory's own child checkouts are\n    the evidence: each resolves by its marker or\n    git remote, and when every one that resolves belongs to the same workspace, so does\n    the directory. A child project with no workspace, or a second workspace, resolves\n    to None: a guess would load one family's rules into another family's session.\n\n    Read-only: unlike resolve_project, it never writes a marker into a child."
    
    
    if PR.get_repo_root(cwd):
        return None
    try:
        names = sorted(os.listdir(cwd))
    except OSError:
        return None
    workspaces = set()
    checked = 0
    for name in names:
        child = os.path.join(cwd, name)
        if name.startswith(".") or not os.path.exists(os.path.join(child, ".git")):
            continue
        checked += 1
        if checked > max_checkouts:
            return None           
        marker = _read_project_marker(child)
        e = _find_project_by_id(store, marker.get("id")) if marker else None
        if not e:
            for raw in PR.get_git_remotes(child) or []:
                e = _find_project_by_remote(store, normalize_remote(raw))
                if e:
                    break
        if e:
            workspaces.add(e.get("workspace") or None)
    if len(workspaces) == 1 and None not in workspaces:
        return next(iter(workspaces))
    return None


def upsert_project(store, canonical_remote, display_name, stack=None, integration_branch=None,
                   workspace=None, local_path=None, overwrite=False):
    
    
    
    problem = (_path_problem(display_name)
               or (workspace and _name_problem(workspace, "workspace")))
    if problem:
        return {"error": f"cannot register project {display_name!r}"
                         f"{f' in workspace {workspace!r}' if workspace else ''}: {problem}"}
    with store.lock:
        from .store import _now, emit_toml, parse_toml, stable_uuid
        path = os.path.join(store.root, "projects", display_name, "project.toml")
        
        
        
        
        if os.path.exists(path):
            existing_remote = parse_toml(open(path).read()).get("canonical_remote")
            if existing_remote and existing_remote != canonical_remote and not overwrite:
                return {"error": f"project '{display_name}' already exists with canonical_remote "
                        f"'{existing_remote}'; writing '{canonical_remote}' would clobber it. "
                        f"Pass overwrite=True to replace it, or use a different display_name.",
                        "existing_canonical_remote": existing_remote,
                        "requested_canonical_remote": canonical_remote}
            if existing_remote and existing_remote != canonical_remote and overwrite:
                
                store.entities.pop(stable_uuid("project", "global", existing_remote), None)
        meta = {"uuid": stable_uuid("project", "global", canonical_remote), "type": "project",
                "display_name": display_name, "canonical_remote": canonical_remote, "stack": stack,
                "integration_branch": integration_branch, "workspace": workspace, "updated_at": _now()}
        meta["created_at"] = _now()
        store._write_atomic(path, emit_toml(meta))
        store._arm_commit(path)
        meta["scope"] = f"project:{display_name}"
        store._index(meta, path)
        result = {"id": meta["uuid"], "display_name": display_name, "canonical_remote": canonical_remote}
        
        
        if local_path:
            from .project_resolve import get_repo_root
            rr = get_repo_root(local_path) or (local_path if os.path.isdir(local_path) else None)
            if rr and _write_project_marker(rr, meta["uuid"], display_name):
                result["marker_written"] = True
        return result


def delete_project(store, display_name):
    'Remove a project record: delete projects/<display_name>/project.toml and\n    drop its in-memory entity (mirrors the file-removal delete_* entities).'
    with store.lock:
        path = os.path.join(store.root, "projects", display_name, "project.toml")
        ent = next((e for e in store.entities.values()
                    if e.get("type") == "project" and e.get("display_name") == display_name), None)
        if not ent and not os.path.exists(path):
            return {"deleted": None}
        
        write_guard.check_entity(store.root, "delete", "project", display_name, "global",
                                 before=ent)
        if os.path.exists(path):
            os.remove(path)
            d = os.path.dirname(path)
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        if ent:
            store.entities.pop(ent["uuid"], None)
        store._arm_commit(path)
        return {"deleted": display_name}







_PROSE_KINDS = ("memory", "doc", "instruction", "skill", "command", "agent_definition")
_SIDECAR_KINDS = ("hook", "script")


def _name_problem(name, kind="project"):
    'Why `name` cannot be a project (or workspace) directory and scope name, or None.\n    The one rule for both: an unchecked workspace name such as `../x` would put an\n    entity outside `workspaces/`.'
    if not isinstance(name, str) or not name or name != name.strip():
        return f"a {kind} name must be non-empty, with no leading or trailing whitespace"
    if name.startswith("."):
        return f"a {kind} name may not start with a dot (the loader skips dot directories)"
    if name.lower() == "global" or re.search(r"""[/\\:"'\[\]|#\x00-\x1f\x7f]""", name):
        return (f"a {kind} name may not be `global` or contain / \\ : \" ' [ ] | # or a "
                f"control character (it is a directory name, the tail of a `{kind}:` scope, "
                "and the target of quoted pointers and wikilinks)")
    return None


def _path_problem(name):
    'Why `name` cannot be one directory under projects/, or None.'
    if not isinstance(name, str) or not name or name.startswith(".") \
            or re.search(r"[/\\\x00]", name):
        return ("a project name must be non-empty, may not start with a dot and may not "
                "contain / or \\ (it is one directory under projects/)")
    return None


def _split_frontmatter(text):
    '(frontmatter, rest): the frontmatter with both `---` lines, or ("", text).'
    if text.startswith("---\n"):
        end = text.find("\n---", 3)
        while end != -1 and text[end + 4:end + 5] not in ("", "\n"):
            end = text.find("\n---", end + 4)
        if end != -1:
            return text[:end + 4], text[end + 4:]
    return "", text


def _pointer_patterns(old, new, root_page):
    '(pattern, replacement) pairs that re-address a pointer from project `old` to `new`.\n\n    Only forms that address the project: a vault path under projects/<old>/, a `project=`\n    argument, and the positional project argument of get_doc, get_memory and get_entity.\n    A bare mention of the name is history and is left alone.'
    o, n = re.escape(old), new.replace("\\", "\\\\")
    q = r"""(["'])"""
    pats = []
    if root_page:
        
        pats.append((rf"\[\[projects/{o}/docs/{o}(?=\.md\b|[\]#|])",
                     f"[[projects/{n}/docs/{n}"))
        pats.append((rf"\[\[{o}(?=[\]#|])", f"[[{n}"))
    pats += [
        (rf"\[\[projects/{o}/", f"[[projects/{n}/"),
        (rf"(\bproject\s*=\s*){q}{o}\2", rf"\g<1>\g<2>{n}\g<2>"),
        (rf"(\bget_(?:doc|memory)\(\s*{q}[^\"'\n]*\2\s*,\s*){q}{o}\3",
         rf"\g<1>\g<3>{n}\g<3>"),
        (rf"(\bget_entity\(\s*{q}[^\"'\n]*\2\s*,\s*{q}[^\"'\n]*\3\s*,\s*){q}{o}\4",
         rf"\g<1>\g<4>{n}\g<4>"),
    ]
    return [(re.compile(p), r) for p, r in pats]


def _rewrite_text(text, old, new, patterns, whole_header=False):
    '`text` with every pointer to project `old` re-addressed to `new`. In a header (the\n    frontmatter, or all of a sidecar with `whole_header`) the exact-scope target form\n    `project:<old>::` is rewritten too; in a body that form is prose about a tool\n    argument, never an edge, so it is left alone.'
    fm, body = (text, "") if whole_header else _split_frontmatter(text)
    for pat, rep in patterns:
        fm, body = pat.sub(rep, fm), pat.sub(rep, body)
    fm = fm.replace(f"project:{old}::", f"project:{new}::")
    return fm + body


def _rehome_header(text, fmt, old_scope, new_scope, old_uuid, new_uuid):
    'A moved entity file with its stored `uuid` and `scope` re-derived for the new\n    scope, the values an MCP write there would give it. Only the header is touched.'
    if fmt == "md":
        head, tail = _split_frontmatter(text)
    else:                              
        head, tail = text, ""
    if old_uuid and new_uuid:
        head = head.replace(old_uuid, new_uuid)
    if fmt == "json":
        head = re.sub(r'("scope"\s*:\s*)"' + re.escape(old_scope) + '"',
                      lambda m: m.group(1) + json.dumps(new_scope, ensure_ascii=False), head)
    elif fmt == "toml":
        head = re.sub(r'(?m)^(scope\s*=\s*)"' + re.escape(old_scope) + '"',
                      lambda m: m.group(1) + json.dumps(new_scope), head)
    else:
        head = re.sub(r'(?m)^(scope\s*:\s*)(["\']?)' + re.escape(old_scope) + r"\2\s*$",
                      lambda m: m.group(1) + json.dumps(new_scope, ensure_ascii=False), head)
    return head + tail


def _set_header_value(text, key, old_value, new_value):
    "A markdown header's `key: <old_value>` (JSON-quoted, single-quoted or plain, as\n    Obsidian may leave it) set to `new_value`, JSON-quoted as the store writes it."
    fm, body = _split_frontmatter(text)
    fm = re.sub(rf'(?m)^({re.escape(key)}\s*:\s*)(["\']?){re.escape(old_value)}\2\s*$',
                lambda m: m.group(1) + json.dumps(new_value, ensure_ascii=False), fm, count=1)
    return fm + body


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _plan_moved_file(p, old, new, old_scope, new_scope, root_page, patterns):
    'The new text of one file of the moved directory, or None when it is unchanged.\n    Runs before the move: an entity file that cannot be read raises ValueError naming\n    it, so the whole rename is refused and nothing has moved yet.'
    from .store import NATURAL_KEY, parse_frontmatter, parse_toml, stable_uuid
    if p.endswith(".meta.toml"):
        fmt = "toml"
    elif p.endswith(".meta.json"):
        fmt = "json"
    elif p.endswith(".md"):
        fmt = "md"
    else:
        return None                    
    try:
        text = _read_text(p)
        meta = (parse_toml(text) if fmt == "toml" else json.loads(text) if fmt == "json"
                else parse_frontmatter(text)[0])
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"cannot rename: {p} cannot be read ({type(exc).__name__}: {exc}); "
                         "fix or remove it first, nothing was moved") from exc
    typ = meta.get("type")
    nk = NATURAL_KEY.get(typ or "")
    key = meta.get(nk) if nk else None
    is_root = (fmt == "md" and root_page and typ == "doc" and key == f"{old}.md")
    if is_root:
        key = f"{new}.md"
    new_uuid = stable_uuid(typ, new_scope, key) if meta.get("uuid") and typ and key else None
    out = _rehome_header(text, fmt, old_scope, new_scope, meta.get("uuid"), new_uuid)
    if is_root:
        out = _set_header_value(out, "path", f"{old}.md", f"{new}.md")
        if meta.get("title") in (old, f"{old}.md"):
            out = _set_header_value(out, "title", meta["title"], new)
    if fmt == "md":
        out = _rewrite_text(out, old, new, patterns)
    elif typ in _SIDECAR_KINDS:
        out = _rewrite_text(out, old, new, patterns, whole_header=True)
    return out if out != text else None


def rename_project(store, project, new_display_name):
    'Rename a project: its record, its directory with every entity in it, and every\n    pointer elsewhere in the store that addresses it by name. See the MCP tool.\n\n    Every guard and every read runs first, and every new file body is computed before\n    the first byte moves. The move itself is journaled: a failure part way puts each\n    file back and moves the directory back, so the store is never left half renamed.'
    from .store import NATURAL_KEY, emit_toml, parse_toml
    with store.lock:
        ent = store.project_entity(project) or next(
            (e for e in store.entities.values() if e.get("type") == "project"
             and project in (e.get("uuid"), e.get("canonical_remote"))), None)
        if not ent:
            known = sorted(e.get("display_name") or "" for e in store.entities.values()
                           if e.get("type") == "project")
            return {"error": f"no project {project!r}. Registered projects: {known}"}
        old, new = ent["display_name"], new_display_name
        problem = _name_problem(new)
        if problem:
            return {"error": f"cannot rename {old!r} to {new!r}: {problem}"}
        if new == old:
            return {"error": f"project {old!r} already has that name"}
        if new.lower() == old.lower():
            return {"error": f"cannot rename {old!r} to {new!r}: a change of case only is "
                             "refused, because the fleet's macOS disks are case-insensitive "
                             "and would see one directory under two names"}
        pdir = os.path.join(store.root, "projects")
        taken = [n for n in (os.listdir(pdir) if os.path.isdir(pdir) else [])
                 if n.lower() == new.lower()]
        taken += [e["display_name"] for e in store.entities.values()
                  if e.get("type") == "project" and e is not ent
                  and str(e.get("display_name") or "").lower() == new.lower()]
        if taken:
            return {"error": f"cannot rename {old!r} to {new!r}: the name is taken "
                             f"(projects/{taken[0]}/, compared without case, since the "
                             f"fleet's macOS disks are case-insensitive)"}
        old_scope, new_scope = f"project:{old}", f"project:{new}"
        old_dir, new_dir = os.path.join(pdir, old), os.path.join(pdir, new)
        if not os.path.isdir(old_dir):
            return {"error": f"project {old!r} has no directory at projects/{old}/"}
        root_page = os.path.isfile(os.path.join(old_dir, "docs", f"{old}.md"))
        if root_page and os.path.lexists(os.path.join(old_dir, "docs", f"{new}.md")):
            return {"error": f"cannot rename {old!r} to {new!r}: the root page docs/{old}.md "
                             f"would move to docs/{new}.md, which already exists"}
        patterns = _pointer_patterns(old, new, root_page)

        moved = [e for e in store.entities.values()
                 if e.get("scope") == old_scope and e.get("type") != "project"]
        
        for e in moved:
            kind, key = e["type"], e.get(NATURAL_KEY.get(e["type"], ""), "")
            body = e.get("script_body" if kind in ("hook", "script") else "body")
            write_guard.check_entity(store.root, "delete", kind, key, old_scope, before=e,
                                     language=e.get("language"))
            write_guard.check_entity(store.root, "write", kind, key, new_scope, body=body,
                                     before=e, language=e.get("language"))
        old_files = sorted(os.path.join(dp, f) for dp, _dns, fs in os.walk(old_dir) for f in fs)
        for p in old_files:
            store._refuse_if_stale(p)

        def moved_path(p):
            rel = os.path.relpath(p, old_dir)
            if root_page and rel == os.path.join("docs", f"{old}.md"):
                return os.path.join(new_dir, "docs", f"{new}.md")
            return os.path.join(new_dir, rel)

        
        moved_writes = {}
        for p in old_files:
            out = _plan_moved_file(p, old, new, old_scope, new_scope, root_page, patterns)
            if out is not None:
                moved_writes[moved_path(p)] = out
        rec = parse_toml(_read_text(os.path.join(old_dir, "project.toml")))
        rec["display_name"] = new
        rec["updated_at"] = _now_iso()
        moved_writes[os.path.join(new_dir, "project.toml")] = emit_toml(rec)

        
        edits = {}                      
        for e in list(store.entities.values()):
            kind = e.get("type")
            if e.get("scope") == old_scope or kind not in _PROSE_KINDS + _SIDECAR_KINDS:
                continue
            path = e.get("_path")
            if kind in _SIDECAR_KINDS:
                path = (path or "") + ".meta.toml"
            if not path or not os.path.isfile(path) or (
                    kind not in _SIDECAR_KINDS and not path.endswith(".md")):
                continue
            try:
                text = _read_text(path)
            except (OSError, UnicodeDecodeError):
                continue                
            out = _rewrite_text(text, old, new, patterns, whole_header=kind in _SIDECAR_KINDS)
            if out == text:
                continue
            key = e.get(NATURAL_KEY.get(kind, ""), "")
            body = (e.get("script_body") if kind in _SIDECAR_KINDS
                    else _split_frontmatter(out)[1].strip("\n"))
            write_guard.check_entity(store.root, "write", kind, key, e["scope"], body=body,
                                     before=e, language=e.get("language"))
            store._refuse_if_stale(path)
            edits[path] = (e, text, out)

        from . import audit as A
        observations = []           
        for arch in (False, True):
            observations += [(o, arch) for o in A._audit_read_dir(A._audit_dir(store, arch))
                             if o.get("project") == old]

        
        undo = []                     
        try:
            os.rename(old_dir, new_dir)
            undo.append(lambda: os.rename(new_dir, old_dir))
            if root_page:
                src = os.path.join(new_dir, "docs", f"{old}.md")
                dst = os.path.join(new_dir, "docs", f"{new}.md")
                os.rename(src, dst)
                undo.append(lambda: os.rename(dst, src))
            for path, out in moved_writes.items():
                before = _read_text(path)
                store._write_atomic(path, out)
                undo.append(lambda path=path, before=before: store._write_atomic(path, before))
            for path, (_e, before, out) in edits.items():
                store._write_atomic(path, out)
                undo.append(lambda path=path, before=before: store._write_atomic(path, before))
            for o, arch in observations:
                prior = dict(o)
                o["project"] = new
                A._audit_write(store, o, archive=arch)
                undo.append(lambda prior=prior, arch=arch: A._audit_write(store, prior,
                                                                          archive=arch))
        except BaseException:
            for step in reversed(undo):
                try:
                    step()
                except Exception as exc:          
                    _log.error("rename_project: an undo step failed: %s", exc)
            store.reload()
            raise

        new_files = [moved_path(p) for p in old_files]
        
        
        touched = old_files + new_files + list(edits)
        for p in touched:
            store._arm_commit(p)
        store.reload()
        for p in new_files + list(edits):
            store._note_write(p)
        for p in old_files:
            store._note_write(p, op="delete")
        store._fire_write_callbacks(touched)

        kinds: dict[str, int] = {}
        for e in moved:
            kinds[e["type"]] = kinds.get(e["type"], 0) + 1
        return {"renamed": {"from": old, "to": new}, "id": ent["uuid"],
                "moved_files": len(old_files), "moved_entities": kinds,
                "root_page": f"docs/{new}.md" if root_page else None,
                "pointers_rewritten": sorted(
                    f"{e['type']} {e['scope']} {e.get(NATURAL_KEY.get(e['type'], ''), '')}"
                    for e, _b, _o in edits.values()),
                "audit_observations": sorted(int(o["id"]) for o, _a in observations),
                "note": "Checkouts keep resolving: the `.agents/project-id` marker holds the "
                        "id, which did not change. Re-run project-materialize in each checkout."}


def _now_iso():
    from .store import _now
    return _now()
