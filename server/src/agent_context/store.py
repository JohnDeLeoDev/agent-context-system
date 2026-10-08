
'ContextStore — file-tree + in-memory index backing the agent-context daemon.\n\nSource of truth is the git working tree at ~/.agent-context (see its FORMAT.md).\nThis module loads that tree into an in-memory index and provides CRUD, search,\nand scope resolution over it, plus git sync. There is no database.\n\nConcurrency contract: the daemon is the single owner of the working tree and\ngit. All mutations and all git operations are serialized behind `self.lock`.\nReads operate on the in-memory index and are lock-free. Sessions never run git\ndirectly — they call `sync()` and the daemon performs it under the lock.'
from __future__ import annotations

import collections
import contextlib
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

log = logging.getLogger("agent-context")



_SERVER_DIRTY_LOGGED = False



_SERVER_DIRTY_STREAK = 0



_PUSH_FAIL_STREAK = 0






from . import write_guard, write_ledger  
from .store_tasks import SERVER_TASK_ENV  
from .paths import _fsync_enabled, write_atomic  

NS = uuid.UUID("a6f7c2e0-0000-5000-a000-000000000001")
EXT = {"sh": "sh", "bash": "sh", "python": "py", "py": "py", "python3": "py", "fish": "fish",
       "js": "js", "javascript": "js", "node": "js",
       
       
       
       
       "ts": "ts", "typescript": "ts",
       
       
       "rs": "rs", "rust": "rs"}
DEFAULT_ROOT = os.path.expanduser(os.environ.get("AGENT_CONTEXT_STORE", "~/.agent-context"))


NATURAL_KEY = {"instruction": "title", "memory": "slug", "doc": "path", "skill": "name",
               "command": "name", "hook": "name", "script": "name", "agent_definition": "name"}


def _now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")












_OBJECT_NAME = re.compile(r"[0-9a-f]{38}")



UPLOADED_ROW_FILES = ("deps.json", "token-usage/*.json")


def _may_commit_row(rel, me):
    "False for another machine's row, except its UPLOADED_ROW_FILES."
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "machines" or parts[1] == me:
        return True
    return _uploaded_row_file(parts[2:])


def _uploaded_row_file(rest):
    'Is machines/<uuid>/<rest...> (rest as path parts) an UPLOADED_ROW_FILES entry?'
    return rest == ["deps.json"] or (len(rest) == 2 and rest[0] == "token-usage"
                                     and rest[1].endswith(".json"))

_STALE_GIT_HINTS = re.compile(
    r"^.*(apply stashed changes with|git stash pop|Created autostash:).*$\n?",
    re.MULTILINE | re.IGNORECASE)


def _scrub_stale_git_hints(text):
    'git stderr with advice about an autostash the caller has already restored.'
    return _STALE_GIT_HINTS.sub("", text or "").strip()


def slugify(s):
    return re.sub(r"[^a-z0-9._-]+", "-", str(s).strip().lower()).strip("-._") or "item"


def stable_uuid(typ, scope, key):
    return str(uuid.uuid5(NS, f"{typ}|{scope}|{key}"))


def _default_ssh_cmd(root=None):
    
    
    
    
    
    
    
    
    
    
    
    
    opts = ("-o BatchMode=yes -o ConnectTimeout=5 "
            "-o ServerAliveInterval=5 -o ServerAliveCountMax=3")
    helper = ""
    if root:
        try:
            r = subprocess.run(["git", "-C", root, "config", "--get", "core.sshCommand"],
                               capture_output=True, text=True, timeout=5)
            helper = r.stdout.strip()
        except Exception:
            helper = ""
    if not helper:
        for cand in ("~/.local/bin/git-ssh-op", "~/.ssh/git-ssh-op.sh"):
            path = os.path.expanduser(cand)
            if os.access(path, os.X_OK):
                helper = path
                break
    return f"{helper} {opts}" if helper else f"ssh {opts}"





CONT_KEY = re.compile(r"\s*[\w.-]+\s*:")

LIST_ITEM = re.compile(r"\s*-(?:\s+(.*))?$")


def _frontmatter_scalar(v):
    'One value: JSON first (the form the store writes), then a single-quoted YAML\n    string. Anything else stays the raw text, as it always has.'
    try:
        return json.loads(v)
    except Exception:
        pass
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse_frontmatter(text):
    
    
    
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    last = None
    open_list = None    
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        
        
        item = LIST_ITEM.match(line)
        if (item and open_list is not None and open_list == last
                and (meta.get(last) == "" or isinstance(meta.get(last), list))):
            if not isinstance(meta[last], list):
                meta[last] = []
            meta[last].append(_frontmatter_scalar((item.group(1) or "").strip()))
            continue
        
        
        
        if (last and line[:1].isspace() and not CONT_KEY.match(line)
                and isinstance(meta.get(last), str)):
            meta[last] = f"{meta[last]} {line.strip()}".strip()
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        last = k.strip()
        meta[last] = _frontmatter_scalar(v.strip())
        open_list = last if not v.strip() else None
    body = text[end + 5:]
    body = body.removeprefix("\n")
    return meta, body




_NOT_CARRIED = frozenset({"uuid", "type", "scope", "body", "script_body", "files",
                          "created_at", "updated_at"})


def _empty_value(v):
    return v is None or v == "" or v == []


def _carry_existing_keys(meta, existing, fields):
    'Carry each stored key that the call did not pass into `meta` (context graph T5:\n    upserts keep keys).\n\n    Rebuilding `meta` from `fields` alone would drop every key a full upsert did not\n    repeat: a property added in Obsidian, a typed link, an optional field the caller\n    left out. A key the call passes wins, and a key passed with an empty value\n    (None, "", []) stays removed. `_`-prefixed keys are index-private and never carried.'
    for k, v in existing.items():
        if k in meta or k in fields or k in _NOT_CARRIED or k.startswith("_"):
            continue
        meta[k] = v


def emit_frontmatter(meta, body):
    out = ["---"]
    for k, v in meta.items():
        if v is None:
            continue
        out.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    out.append("---")
    return "\n".join(out) + "\n\n" + (body or "") + "\n"


def parse_toml(text):
    
    
    
    
    try:
        import tomllib
        return tomllib.loads(text)
    except Exception:
        pass
    d = {}
    for line in text.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        k, _, v = line.partition("=")
        try:
            d[k.strip()] = json.loads(v.strip())
        except Exception:
            d[k.strip()] = v.strip().strip('"')
    return d


def emit_toml(d):
    return "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in d.items() if v is not None) + "\n"









_STEM_SUFFIXES = ("ization", "isation", "ations", "ation", "ments", "ment", "ings", "ing",
                  "ies", "ers", "ied", "ed", "es", "er", "ly", "y", "s")


_SEARCH_STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how i in is it my of on or the "
    "to use using what when where which with".split())
_SEARCH_ARCHIVE_WEIGHT = 0.5
_SEARCH_GLOBAL_WEIGHT = 1.25


def _keywords_text(value):
    '`keywords` frontmatter as one string: the words a reader would search by that the\n    title and description do not hold (`authenticate` for a memory that says `auth`).'
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _archived_path(path):
    'A doc under an `archive` or `*-archive` directory segment (graph.is_archived).'
    return any(segment == "archive" or segment.endswith("-archive")
               for segment in path.split("/")[:-1])


def _stem(term):
    if len(term) < 4:
        return term
    for suf in _STEM_SUFFIXES:
        if term.endswith(suf) and len(term) - len(suf) >= 3:
            stem = term[:-len(suf)]
            if len(stem) >= 4 and stem[-1] == stem[-2]:
                stem = stem[:-1]
            return stem
    return term


class StaleWriteError(ValueError):
    'An entity write refused because a remote already holds a newer version of\n    that entity which this checkout has not integrated. See _refuse_if_stale.'


class ContextStore:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = root
        write_guard.register_root(root)
        
        
        self.ledger = write_ledger.register(root)
        self.outside_edits: list[str] = []   
        self._outside_logged: tuple[str, ...] = ()
        self.lock = threading.RLock()
        self._sync_mutex = threading.Lock()  
        self._push_mutex = threading.Lock()  
        self._guard_fetch_at = 0.0           
        self._maintenance_at = 0.0           
        self._fleet_refspecs_done = False    
        
        
        
        self._recent_writes = collections.deque(maxlen=64)
        self._write_warning = None           
        self._fleet_rows_cache = (0.0, [])   
        
        
        self.caller_pid_fn: Callable[[], int | None] | None = None
        
        
        self._write_callbacks: list[Callable[[list[str]], None]] = []
        self.commit_on_write = None   
        self.entities = {}        
        self.by_key = {}          
        self.reload()

    
    @staticmethod
    def _scope_from_relpath(rel):
        parts = rel.split(os.sep)
        if parts[0] == "global":
            return "global"
        if parts[0] == "projects" and len(parts) > 1:
            return f"project:{parts[1]}"
        if parts[0] == "workspaces" and len(parts) > 1:
            return f"ws:{parts[1]}"
        return "global"

    @staticmethod
    def _scope_for(project):
        return "global" if project in (None, "", "global") else f"project:{project}"

    def _reject_phantom_project(self, project, scope):
        'Refuse a write into a project scope that names no registered project.\n\n        A JSON tool call has no Python `None`, so an agent following a doc that says\n        `project=None` sends the string "None", and `_scope_for` would turn that into\n        `project:None`, spawning `projects/None/…` on disk where no session\'s inbox\n        resolver finds it.\n\n        Only new phantoms are refused: an existing `projects/<name>/` directory still\n        accepts writes, so entities left behind by a de-registered project stay\n        editable (and repairable) rather than becoming read-only. Reads and deletes\n        are untouched for the same reason. `upsert_project` writes `project.toml`\n        directly and never comes through here, so registration is unaffected.'
        if project in (None, "", "global"):
            return
        if self.project_entity(project) or os.path.isdir(self._base_dir(scope)):
            return
        known = sorted(e.get("display_name") for e in self.entities.values()
                       if e.get("type") == "project" and e.get("display_name"))
        raise ValueError(
            f"unknown project {project!r}: no registered project has that display_name, "
            f"and writing it would create a phantom project at projects/{project}/. "
            f"Registered projects: {known or '(none)'}. For a global entity, OMIT the "
            f"project argument entirely — do not pass the string \"None\". Register a new "
            f"project with upsert_project first.")

    def workspaces(self):
        'Every workspace name this store knows — named by a project, present on disk,\n        or both. Both halves matter: a workspace dir can exist before any project joins\n        it, and a project can name one whose dir has not been created yet.'
        from .projects import _name_problem
        names = {e["workspace"] for e in self.entities.values()
                 if e.get("type") == "project" and e.get("workspace")}
        d = os.path.join(self.root, "workspaces")
        if os.path.isdir(d):
            names.update(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)))
        
        
        return sorted(n for n in names if not _name_problem(n, "workspace"))

    def scope_from_addressing(self, project=None, workspace=None, writing=True):
        "Resolve a caller's addressing to a scope, or None to use the `project` path.\n\n        `_scope_for` yields only global or project, so an entity that belongs to a\n        family of repos instead of one (workspace instructions, a workspace skill) needs\n        this route to be written to workspace scope.\n\n        An unknown name is refused and never created, for the reason\n        `_reject_phantom_project` exists: a typo would otherwise mkdir\n        `workspaces/NSYTA/` and put the entity somewhere no session resolves, where it\n        looks written and is inert. There is no `upsert_workspace` to register one\n        first, so a new workspace is admitted by any project naming it or by the\n        directory existing -- both listed in the error.\n\n        One validator for reads and writes, reached through `scope_for_write` and\n        `scope_for_read`. Only the consequence clause differs: a typo that strands a\n        write is a different problem from a typo that reads an empty scope. Two\n        validators for one rule would drift."
        
        
        
        if (isinstance(project, str) and project.startswith("ws:")
                and not self.project_entity(project)):
            name = project[len("ws:"):]
            known = self.workspaces()
            if name not in known:
                raise ValueError(
                    f"unknown workspace {name!r}: project={project!r} looks like a workspace "
                    f"scope, and no workspace has that name. Known workspaces: "
                    f"{known or '(none)'}; pass one as workspace=.")
            raise ValueError(
                f"project={project!r} names a workspace scope, not a project: pass "
                f'workspace="{name}" instead.')
        if not workspace:
            return None
        if project:
            raise ValueError(
                f"pass project OR workspace, not both (got project={project!r}, "
                f"workspace={workspace!r}). A workspace-scoped entity belongs to the "
                f"whole workspace; scoping it to one project as well is contradictory.")
        from .projects import _name_problem
        problem = _name_problem(workspace, "workspace")
        if problem:
            raise ValueError(f"workspace {workspace!r} is refused: {problem}")
        known = self.workspaces()
        if workspace not in known:
            consequence =("so writing it would strand the entity where no session "
                           "resolves it" if writing else
                           "so there is no such scope to read from")
            raise ValueError(
                f"unknown workspace {workspace!r}: no registered project names it and "
                f"workspaces/{workspace}/ does not exist, {consequence}. Known workspaces: "
                f"{known or '(none)'}. Set `workspace=` on a project with upsert_project, "
                f"or create workspaces/{workspace}/ first.")
        return f"ws:{workspace}"

    def scope_for_write(self, project=None, workspace=None):
        'Addressing for a write. See `scope_from_addressing`.'
        return self.scope_from_addressing(project, workspace, writing=True)

    def scope_for_read(self, project=None, workspace=None):
        'Addressing for a read, edit or delete. See `scope_from_addressing`.\n\n        Returning None for a plain project read is intended: the caller then takes\n        the ordinary `project` path, which resolves project -> workspace -> global.\n        The `workspace` parameter addresses one scope exactly, which `project` cannot.'
        return self.scope_from_addressing(project, workspace, writing=False)

    def scopes_holding(self, typ, key):
        'Every scope whose index holds (typ, key), sorted.\n\n        `by_key` is keyed (type, scope, key), so this is a scan of keys rather than\n        of entities. It exists to turn a bare "not found" into a sentence that says\n        where the thing actually is -- see `shaping._not_found`.'
        return sorted(s for (t, s, k) in self.by_key if t == typ and k == key)

    def _base_dir(self, scope):
        if scope == "global":
            return os.path.join(self.root, "global")
        kind, _, name = scope.partition(":")
        return os.path.join(self.root, "projects" if kind == "project" else "workspaces", name)

    def adopt_body(self, typ, key, scope, language="sh"):
        'Body of an unindexed file already sitting at this entity\'s own path.\n\n        None when there is no such file, or when the type has no deterministic path.\n\n        This lets a caller register a file that already exists without round-tripping\n        its entire contents through the conversation: `upsert_*` otherwise requires the\n        full body, and a mistranscription of a large script is not recoverable under\n        the git-safety rule, which forbids `git checkout --`.\n\n        Scripts and hooks only: they are the kinds whose on-disk location is a pure\n        function of (scope, type, name, language), so "the file for this entity" is\n        unambiguous. A doc or memory is addressed by a path or slug that the caller\n        could point anywhere, and adopting whatever happens to be there would be a\n        different and worse tool.'
        if typ not in ("hook", "script"):
            return None
        ext = EXT.get(language or "sh")
        if not ext:
            return None
        p = os.path.join(self._base_dir(scope), typ + "s", f"{key}.{ext}")
        try:
            with open(p) as f:
                return f.read()
        except OSError:
            return None

    def _body_to_keep(self, typ, existing, scope) -> str:
        'The body a write must preserve when the caller passed none.\n\n        The file first, then the index copy, and an empty body only when nothing is\n        stored yet. The middle step is the one that matters: `exact_body` answers None\n        for a file whose frontmatter block is gone, whose bytes are not UTF-8, or that\n        was deleted while the index still held the entity, and reading that None as an\n        empty body would replace the whole prose with nothing. The index copy costs a\n        stripped trailing newline, which is one byte.'
        if not existing or existing.get("scope") != scope:
            return ""                      
        exact = self.exact_body(existing)
        if exact is not None:
            return exact
        stored = existing.get("script_body" if typ in ("hook", "script") else "body")
        return stored if isinstance(stored, str) else ""

    def exact_body(self, e):
        'The entity\'s body as the file holds it, so re-emitting reproduces its bytes.\n\n        The index holds a body with every trailing newline stripped (`_load_file`) and\n        `emit_frontmatter` adds one back, so any read-modify-write that hands the index\n        copy back collapses a trailing blank line: a patch billed as frontmatter-only\n        would land a prose diff in a file it never read, on a tree that syncs to every\n        machine (context graph T8, T9).\n\n        None when there is no file to read, so a caller can tell "nothing stored" from\n        "an empty body" and fall back to the index copy.'
        path = (e or {}).get("_path")
        if not path:
            return None
        try:
            
            
            
            with open(path, encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError):
            return None
        typ = e.get("type")
        key = e.get(NATURAL_KEY.get(typ, ""), "")
        
        
        if typ in ("hook", "script") or (typ == "doc" and not str(key).endswith(".md")):
            return raw
        meta, body = parse_frontmatter(raw)
        if not meta:
            return None
        return body.removesuffix("\n")

    
    def reload(self):
        with self.lock:
            self.entities, self.by_key = {}, {}
            self.load_errors = []   
            for dirpath, dirnames, files in os.walk(self.root):
                
                
                
                
                
                
                
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in files:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, self.root)
                    self._load_file(full, rel, fn)

    
    
    graph_generation = 0

    def _index(self, ent, path):
        ent["_path"] = path
        try:
            ent["_mtime"] = os.path.getmtime(path)
        except OSError:
            ent["_mtime"] = None
        self.graph_generation += 1
        self.entities[ent["uuid"]] = ent
        nk = NATURAL_KEY.get(ent["type"])
        if nk and ent.get(nk) is not None:
            self.by_key[(ent["type"], ent["scope"], ent[nk])] = ent["uuid"]

    @staticmethod
    def _really_gone(path):
        'True only when `path` is absent and not momentarily unreadable.\n\n        A single failed stat is not proof of deletion. A file mid-replace, a network\n        mount blinking, an interrupted syscall and an unlink are indistinguishable\n        from one `os.path.exists`, and acting on one costs the entity until the next\n        full reload, which is minutes away. That asymmetry is the reason for a second\n        look: a false negative here delays an eviction by one cycle, while a false\n        positive makes a live document unreachable to every `get_*` and every write.\n\n        Re-reading the directory before the second check makes it a second opinion\n        and not the same answer twice: it forces the filesystem to resolve the parent\n        again and not serve a cached negative. If the directory itself cannot be read,\n        the file is presumed to stand: an unreadable tree is a filesystem problem and\n        no evidence that anything was deleted.'
        if os.path.exists(path):
            return False
        try:
            os.listdir(os.path.dirname(path) or ".")
        except OSError:
            return False
        return not os.path.exists(path)

    def _forget(self, ent):
        'Drop one entity from both indexes. The inverse of `_index`.\n\n        Call under `self.lock`. Clears `by_key` only when it still points at this\n        uuid: a natural key can have been re-taken by a different entity (a doc\n        recreated at the same path gets a fresh uuid), and blanking the key then\n        would unindex the live one along with the dead.'
        self.graph_generation += 1
        self.entities.pop(ent["uuid"], None)
        nk = NATURAL_KEY.get(ent["type"])
        if nk and ent.get(nk) is not None:
            k = (ent["type"], ent["scope"], ent[nk])
            if self.by_key.get(k) == ent["uuid"]:
                self.by_key.pop(k, None)

    def sweep_vanished(self):
        'Evict every indexed entity whose file is gone. Returns how many.\n\n        `_fresh` covers the read paths, but a whole-store scan never goes through\n        it: `check_integrity` and `_known_targets` walk `self.entities` directly,\n        which is where a ghost does its damage. One stat per entity is cheaper than\n        the scan that follows.'
        with self.lock:
            gone = [e for e in list(self.entities.values())
                    if e.get("_path") and self._really_gone(e["_path"])]
            for e in gone:
                self._forget(e)
        return len(gone)

    def _load_file(self, full, rel, fn):
        parts = rel.split(os.sep)
        scope = self._scope_from_relpath(rel)
        try:
            
            if fn.endswith(".meta.toml"):
                meta = parse_toml(open(full).read())
                body_path = full[: -len(".meta.toml")]
                meta["script_body"] = open(body_path).read() if os.path.exists(body_path) else ""
                meta["scope"] = scope
                meta.setdefault("uuid", stable_uuid(meta.get("type", "script"), scope, meta.get("name", fn)))
                self._index(meta, body_path)
                return
            if fn.endswith(".meta.json"):  
                meta = json.loads(open(full).read())
                meta["scope"] = scope
                body_path = full[: -len(".meta.json")]
                meta["body"] = open(body_path).read() if os.path.exists(body_path) else ""
                self._index(meta, body_path)
                return
            
            if os.path.exists(full + ".meta.toml") or os.path.exists(full + ".meta.json"):
                return
            if fn in ("README.md", "FORMAT.md", ".gitignore") and scope == "global" and len(parts) == 1:
                return
            if fn == "project.toml":
                d = parse_toml(open(full).read())
                d["scope"] = scope
                self._index(d, full)
                return
            if fn == "workspace.toml" or (parts[0] == "machines" and fn.endswith(".toml")):
                d = parse_toml(open(full).read())
                d["scope"] = scope
                d.setdefault("uuid", stable_uuid(d.get("type", "machine"), scope, fn))
                self._index(d, full)
                return
            if parts[0] == "templates":
                return  
            if fn.endswith(".md"):
                meta, body = parse_frontmatter(open(full).read())
                
                
                
                
                
                
                
                typ = nk = key = None
                if "uuid" not in meta and parts[0] in ("global", "projects", "workspaces"):
                    base = 1 if parts[0] == "global" else 2
                    kind_dir = parts[base] if len(parts) > base + 1 else None
                    if kind_dir == "memory" and len(parts) == base + 2:
                        typ, nk, key = "memory", "slug", fn[:-len(".md")]
                    elif kind_dir == "docs":
                        typ, nk, key = "doc", "path", "/".join(parts[base + 1:])
                held = self.by_key.get((typ, scope, key)) if typ else None
                holder = self.entities.get(held) if held else None
                if holder is not None and holder.get("_path") != full:
                    
                    
                    
                    other = os.path.relpath(holder.get("_path") or "", self.root)
                    self.load_errors.append({
                        "path": rel,
                        "error": f"shadowed: this note's path names the entity that {other} "
                                 "already holds in its frontmatter; rename one of them"})
                    return
                if typ and nk:
                    meta.update({"uuid": stable_uuid(typ, scope, key), "type": typ, nk: key,
                                 "_path_derived": True})
                    if typ == "memory":
                        
                        
                        
                        meta.setdefault("memory_type", "reference")
                        meta.setdefault("description", "")
                    else:
                        meta.setdefault("title", fn[:-len(".md")])
                if "uuid" not in meta:
                    
                    
                    
                    
                    
                    
                    
                    in_scope_root = parts[0] in ("global", "projects", "workspaces")
                    at_entity_path = fn == "SKILL.md" or any(
                        p in ("memory", "instructions", "commands", "agents", "docs")
                        for p in parts[:-1])
                    if in_scope_root and at_entity_path:
                        self.load_errors.append({
                            "path": rel,
                            "error": "unindexed: entity file has no `uuid:` store "
                                     "frontmatter, so it is invisible to every get_*/list_*"})
                    return  
                meta["scope"] = scope
                meta["body"] = body.rstrip("\n")
                
                if meta.get("type") == "skill":
                    sk_dir = os.path.dirname(full)
                    meta["files"] = sorted(
                        os.path.relpath(os.path.join(r, f), sk_dir)
                        for r, _, fs in os.walk(sk_dir) for f in fs if f != "SKILL.md")
                
                
                real_nk = NATURAL_KEY.get(meta.get("type") or "")
                held = self.by_key.get((meta.get("type"), scope, meta.get(real_nk))) \
                    if real_nk and not meta.get("_path_derived") else None
                prior = self.entities.get(held) if held else None
                if prior is not None and prior.get("_path_derived") and prior.get("_path") != full:
                    self._forget(prior)
                    self.load_errors.append({
                        "path": os.path.relpath(prior.get("_path") or "", self.root),
                        "error": f"shadowed: this note's path names the entity that {rel} "
                                 "already holds in its frontmatter; rename one of them"})
                self._index(meta, full)
        except Exception as ex:
            
            
            self.load_errors.append({"path": rel, "error": f"{type(ex).__name__}: {ex}"})

    
    def project_entity(self, display_name):
        'Look up a project record by display_name.\n\n        Projects are absent from NATURAL_KEY: their file lives at\n        `projects/<name>/project.toml`, so it indexes under scope `project:<name>`\n        while `get()` would look under `global`, and both lookups miss. This scans the\n        entities because the project count is small and correctness beats a hash here.'
        if not display_name:
            return None
        return next((e for e in self.entities.values()
                     if e.get("type") == "project" and e.get("display_name") == display_name), None)

    def _ws_scope(self, project):
        '`ws:<workspace>` for a project, or None.\n\n        Workspace scope sits between project and global for every entity type, not\n        just instructions. get_instructions, get() and list() all resolve it, so a\n        workspace-scoped skill is visible to get_skill/list_skills as well as projected\n        to disk by project-materialize.'
        if not project or project == "global":
            return None
        ws = (self.project_entity(project) or {}).get("workspace")
        return f"ws:{ws}" if ws else None

    def get(self, typ, key, project=None, scope=None):
        'Resolve an entity. `scope` reads one scope only, without the upward walk.\n\n        The third leg of explicit addressing, alongside upsert/delete. An upsert that\n        knows its destination scope must read the same scope to carry a body or\n        description forward: the resolving lookup would hand it a global entity and\n        the "update" would fork a second one at the write scope, carrying the wrong\n        body with it.'
        if scope is not None:
            uid = self.by_key.get((typ, scope, key))
            return self._fresh(self.entities.get(uid)) if uid else None
        scope = self._scope_for(project)
        uid = self.by_key.get((typ, scope, key))
        if uid is None and scope != "global":          
            ws = self._ws_scope(project)
            if ws:
                uid = self.by_key.get((typ, ws, key))
            if uid is None:
                uid = self.by_key.get((typ, "global", key))
        return self._fresh(self.entities.get(uid)) if uid else None

    def _fresh(self, ent):
        'Re-read one entity if its file changed under us.\n\n        The index is built at startup and on explicit reload, so a targeted edit to a\n        store file (by an editor, a script, or another session) would leave `get_*`\n        serving the pre-edit body while the file on disk said something else. A stat per\n        read is cheap; being confidently stale is not.'
        if not ent or not ent.get("_path"):
            return ent
        try:
            m = os.path.getmtime(ent["_path"])
        except OSError:
            
            
            
            
            
            
            
            if not self._really_gone(ent["_path"]):
                return ent          
            with self.lock:
                self._forget(ent)
            return None
        if ent.get("_mtime") == m:
            return ent
        path = ent["_path"]
        rel = os.path.relpath(path, self.root)
        with self.lock:
            self._load_file(path, rel, os.path.basename(path))
        return self.entities.get(ent["uuid"], ent)

    def list(self, typ, project=None, include_global=True, workspace=None):
        "`workspace` adds that workspace's scope when no project brings one: a session\n        started in a workspace root has no project, and still works in the workspace."
        scope = self._scope_for(project)
        ws = self._ws_scope(project) or (f"ws:{workspace}" if workspace else None)
        out = []
        for e in self.entities.values():
            if e["type"] != typ:
                continue
            if e["scope"] == scope or (ws and e["scope"] == ws) \
               or (include_global and e["scope"] == "global" and scope != "global") \
               or (scope == "global" and e["scope"] == "global"):
                out.append(e)
        return out

    def get_instructions(self, project=None, load_behavior=None, workspace=None):
        "Instructions in force for a session: global + the project's workspace + the\n        project itself, which is what the tool contract promises.\n\n        Dropping a workspace instruction marked `always` from a session it was written\n        for would discard a rule the user authored, which is the worst failure mode this\n        store has, so the workspace leg is part of the lookup."
        scopes = {"global"}
        if project:
            scopes.add(self._scope_for(project))
            ws = (self.project_entity(project) or {}).get("workspace")
            if ws:
                scopes.add(f"ws:{ws}")
        if workspace:      
            scopes.add(f"ws:{workspace}")
        out = [e for e in self.entities.values()
               if e["type"] == "instruction" and e["scope"] in scopes
               and (load_behavior is None or e.get("load_behavior") == load_behavior)]
        return sorted(out, key=lambda e: (e.get("sort_order") or 0, e.get("title") or ""))

    def search(self, query, types=("memory", "doc"), project=None, limit=20):
        words = [t for t in re.split(r"\W+", query.lower()) if t]
        
        
        
        
        terms = [_stem(t) for t in words if t not in _SEARCH_STOPWORDS] or [_stem(t) for t in words]
        if not terms:
            return []
        scope = self._scope_for(project) if project else None  
        scored = []
        for e in self.entities.values():
            if e["type"] not in types:
                continue
            if scope is not None and e["scope"] not in ("global", scope):
                continue
            hay_title = (str(e.get("title") or e.get("slug") or e.get("name") or "") + " "
                         + str(e.get("description") or "") + " "
                         + _keywords_text(e.get("keywords"))).lower()
            
            
            
            body = str(e.get("body") or "").lower() if e["type"] in ("memory", "doc") else ""
            
            
            
            
            
            
            
            body_norm = 1.0 + math.log1p(len(body) / 400.0)
            title_score = 0.0
            body_score = 0.0
            matched = 0
            for t in terms:
                ct = hay_title.count(t)
                cb = body.count(t)
                if ct or cb:
                    matched += 1
                title_score += math.log1p(ct)          
                body_score += math.log1p(cb) / body_norm  
            if not matched:
                continue
            coverage = matched / len(terms)
            
            score = (8 * title_score + body_score) * (coverage ** 2)
            if e["type"] == "doc" and _archived_path(str(e.get("path") or "")):
                
                
                score *= _SEARCH_ARCHIVE_WEIGHT
            if scope is None and e["scope"] == "global":
                
                score *= _SEARCH_GLOBAL_WEIGHT
            idx = body.find(terms[0])
            snip = (e.get("description") or "")[:160] if idx < 0 else body[max(0, idx - 40):idx + 120]
            scored.append((score, {"entity_type": e["type"], "scope": e["scope"],
                                   "name": e.get("slug") or e.get("path") or e.get("name"),
                                   "description": e.get("description") or e.get("title") or "",
                                   "snippet": snip.strip(), "rank": -score,
                                   
                                   **({"title": e.get("title")}
                                      if e["type"] == "doc" and e.get("description") else {}),
                                   
                                   
                                   **({"workspace": e["scope"][len("ws:"):]}
                                      if e["scope"].startswith("ws:") else
                                      {"project": e["scope"][len("project:"):]}
                                      if e["scope"].startswith("project:") else {})}))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]

    
    def _file_for(self, typ, scope, key):
        base = self._base_dir(scope)
        if typ == "memory":
            return os.path.join(base, "memory", slugify(key) + ".md")
        if typ == "doc":
            return os.path.join(base, "docs", key)
        if typ == "instruction":
            return os.path.join(base, "instructions", slugify(key) + ".md")
        if typ == "skill":
            return os.path.join(base, "skills", key, "SKILL.md")
        if typ == "command":
            return os.path.join(base, "commands", key + ".md")
        if typ == "agent_definition":
            return os.path.join(base, "agents", key + ".md")
        raise ValueError(f"_file_for unsupported type {typ}")

    @staticmethod
    def _write_atomic(path, text):
        'Write `text` to `path` leaving no window in which the file is truncated.\n\n        `open(path, "w")` empties the target before the first byte is written. Any\n        failure in that window (an exception building the payload, the process\n        killed, the machine out of memory) leaves a 0-byte file and the previous\n        content is gone, and autosync would commit and push the empty result.\n\n        Every doc, memory, instruction, skill, command, hook and script this store\n        holds is written this way, and invariant-check.py asserts it:\n        `entity-writes-are-atomic`.\n\n        os.replace is atomic on POSIX and on Windows, so a concurrent reader (or a\n        `git add` from the autocommit hook, which is the reader that matters here)\n        sees either the whole old file or the whole new one, never a partial. fsync\n        before the rename so the content is durable before it is named.\n\n        The implementation, the durability switch and the cost of the fsync are all\n        documented on `paths.write_atomic`, which this delegates to.'
        write_atomic(path, text)

    @staticmethod
    def _corruption_suspected(errs, remotes) -> bool:
        "Is this machine's object store the likely cause of the push failures?\n\n        Only when every mirror refused, each with a pack-level error. One mirror\n        refusing is that mirror's fault, and blaming local objects there would send\n        the diagnosis to a clean machine."
        markers = ("unpacker error", "bad object", "did not send all necessary objects")
        return (bool(remotes) and all(r in errs for r in remotes)
                and all(any(m in (errs[r] or "") for m in markers) for r in remotes))

    def _empty_loose_objects(self):
        'Zero-byte loose object files in this repo, as `ab/cdef…` paths.\n\n        A write cut off mid-way (a read-only remount, power loss) leaves the object\'s\n        name with no content. git then packs garbage for every push and each remote\n        answers "unpacker error". A full fsck is slow on small disks; a size scan of\n        the loose objects is fast and names the files a human has to move aside.'
        rel = self._git("rev-parse", "--git-path", "objects", check=False).stdout.strip()
        if not rel:
            return []
        objdir = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
        out = []
        with contextlib.suppress(OSError):
            for sub in sorted(os.listdir(objdir)):
                if len(sub) != 2:
                    continue            
                d = os.path.join(objdir, sub)
                for name in sorted(os.listdir(d)):
                    
                    
                    
                    if not _OBJECT_NAME.fullmatch(name):
                        continue
                    with contextlib.suppress(OSError):
                        if os.path.getsize(os.path.join(d, name)) == 0:
                            out.append(f"{sub}/{name}")
        return out

    def upsert(self, typ, key, fields, body: str | None = None, project=None, scope=None):
        'Write an entity. `scope` addresses one directly, bypassing `project`.\n\n        `_reject_phantom_project` is skipped for an explicit scope: that guard\n        catches a project name invented by a caller, and a scope read off an entity\n        already in the index is one that exists.'
        
        
        
        self._maybe_fetch_for_guard()
        with self.lock:
            if scope is None:
                scope = self._scope_for(project)
                self._reject_phantom_project(project, scope)
                existing = self.get(typ, key, project)
            else:
                
                
                
                existing = self._fresh(self.entities.get(self.by_key.get((typ, scope, key))))
            
            
            write_guard.check_entity(
                self.root, "upsert", typ, key, scope, body=body, before=existing,
                language=fields.get("language") or (existing or {}).get("language"))
            now = _now()
            meta = {"uuid": stable_uuid(typ, scope, key), "type": typ, NATURAL_KEY[typ]: key}
            if existing and existing["scope"] == scope:
                meta["created_at"] = existing.get("created_at", now)
                old_path = existing.get("_path")
            else:
                meta["created_at"] = now
                old_path = None
            
            
            
            
            
            if body is None:
                body = self._body_to_keep(typ, existing, scope)
            
            
            
            
            kept = existing if existing and existing["scope"] == scope else {}
            meta.update({k: v for k, v in fields.items()
                         if k not in meta and (not _empty_value(v) or (k in kept and kept[k] == v))})
            if kept:
                _carry_existing_keys(meta, kept, fields)
                
                
                meta = {**{k: meta[k] for k in kept if k in meta}, **meta}
            
            
            
            if "area" not in meta:
                if scope == "global":
                    meta["area"] = "Agent Context"
                else:
                    workspace = None
                    if scope.startswith("ws:"):
                        workspace = scope[3:]
                    elif scope.startswith("project:"):
                        workspace = (self.project_entity(scope[8:]) or {}).get("workspace")
                    if workspace:
                        meta["area"] = workspace
            meta["updated_at"] = now
            meta["scope"] = scope
            
            
            
            
            
            
            
            
            
            if existing and existing.get("scope") == scope:
                _body_key = "script_body" if typ in ("hook", "script") else "body"
                _old_body = existing.get(_body_key)
                
                
                _new_body = _old_body if body is None else body
                _skip = ("updated_at", "body", "script_body", "files")

                def _cmp(d):
                    return {k: v for k, v in d.items()
                            if k not in _skip and not k.startswith("_")}

                if (_cmp(meta) == _cmp(existing)
                        and (_new_body or "").rstrip("\n") == (_old_body or "").rstrip("\n")):
                    meta["updated_at"] = existing.get("updated_at", now)

            if typ in ("hook", "script"):
                lang = meta.get("language") or "sh"
                
                
                if lang not in EXT:
                    raise ValueError(f"unknown language {lang!r} for a {typ}; one of "
                                     f"{sorted(EXT)}")
                
                
                
                
                
                stem, _, suffix = key.rpartition(".")
                if stem and suffix.lower() in set(EXT.values()):
                    raise ValueError(
                        f"{typ} name {key!r} must not end in a file extension — the "
                        f"language ({lang!r}) decides it, and one is appended, so this "
                        f"would materialize as {key}.{EXT[lang]}. Use {stem!r}.")
                bp = os.path.join(self._base_dir(scope), typ + "s", f"{key}.{EXT[lang]}")
                self._refuse_if_stale(bp)
                self._write_atomic(bp, body)
                sidecar = {k: v for k, v in meta.items()
                           if k not in ("script_body", "body", "files") and not k.startswith("_")}
                self._write_atomic(bp + ".meta.toml", emit_toml(sidecar))
                self._note_write(bp)
                written = [bp]
                meta["script_body"] = body
                
                
                if old_path and os.path.abspath(old_path) != os.path.abspath(bp):
                    for stale in (old_path, old_path + ".meta.toml"):
                        if os.path.exists(stale):
                            os.remove(stale)
                            written.append(stale)
                self._index(meta, bp)
            else:
                path = self._file_for(typ, scope, key)
                self._refuse_if_stale(path)
                if typ == "doc" and not key.endswith(".md"):
                    self._write_atomic(path, body)
                    self._write_atomic(
                        path + ".meta.json",
                        json.dumps({k: v for k, v in meta.items()
                                    if k not in ("body", "_path")}, indent=1) + "\n")
                    meta["body"] = body
                else:
                    
                    
                    
                    wmeta = {k: v for k, v in meta.items()
                             if k not in ("body", "files") and not k.startswith("_")}
                    self._write_atomic(path, emit_frontmatter(wmeta, body))
                    meta["body"] = body.rstrip("\n")
                if old_path and old_path != meta.get("_path") and os.path.exists(old_path):
                    pass
                self._note_write(path)
                written = [path]
                self._index(meta, path)
            self._fire_write_callbacks(written)
            for p in written:
                self._arm_commit(p)
            return self.entities[meta["uuid"]]

    
    def _note_write(self, path, op="write"):
        'Record a write or, with op="delete", a delete, for the fleet row, and\n        decide whether another session is on the same entity right now. Never\n        raises: a warning is diagnostics and must not be able to fail the write\n        it describes.'
        try:
            rel = os.path.relpath(path, self.root)
            pid, session = self._caller()
            if pid and not session:
                
                
                from . import claims
                made = claims.ensure_server_claim(pid, file=os.path.abspath(path))
                session = (made or {}).get("session")
            self._recent_writes.append((rel, time.time(), session, op))
            self._write_warning = None if op == "delete" else self.claim_warning(rel, session)
        except Exception:
            self._write_warning = None

    @staticmethod
    def _entry(e):
        '(path, ts, session, op) from a recent-writes entry. Entries recorded before\n        deletes were tracked carry no op, and are writes.'
        return (e[0], e[1], e[2], e[3] if len(e) > 3 else "write")

    def _caller(self):
        '(pid, claim session) of the calling session, or (None, None).'
        try:
            pid = self.caller_pid_fn() if self.caller_pid_fn else None
            if not pid:
                return None, None
            from . import claims
            me = claims.caller(claims.read_live(), pid)
            return pid, (me or {}).get("session")
        except Exception:
            return None, None

    def recent_writes(self, window=1800):
        t = time.time()
        out = []
        for e in self._recent_writes:
            p, ts, s, op = self._entry(e)
            if t - ts <= window:
                out.append({"path": p, "ts": int(ts), **({"session": s} if s else {}),
                            **({"op": op} if op != "write" else {})})
        return out[-30:]

    def pop_write_warning(self):
        w, self._write_warning = self._write_warning, None
        return w

    def _fleet_rows(self, max_age=60.0):
        'fleet.read_all, at most once a minute: it shells out to git for every ref\n        row, which is too much to pay on every write.'
        from . import fleet
        t = time.time()
        at, rows = self._fleet_rows_cache
        if t - at > max_age:
            rows = fleet.read_all(self.root, now=t)
            self._fleet_rows_cache = (t, rows)
        return rows

    def claim_warning(self, rel, session=None) -> str | None:
        'Is another session on this entity? On this machine: a write of the same\n        path in the last thirty minutes attributed to a different session (the\n        daemon knows its caller, server._CALLER). On another machine, from the fleet\n        rows: a recent write of the same path published by its daemon, or a live\n        session there that edited the file by hand. Warn, then allow: a hand-off has\n        to stay possible, and the stale-write guard refuses the writes that are\n        behind.\n\n        A write to a path whose newest earlier entry is a delete is a re-creation, and\n        is said first: a different session could otherwise re-create an entity from a\n        copy it still holds, and neither session would be told. The same known session\n        undoing its own delete is deliberate and is not warned.'
        try:
            from . import claims, machine
            t = time.time()
            me = str(machine.get_machine_uuid())
            notes, deleted = [], []
            prior = [self._entry(e) for e in list(self._recent_writes)[:-1]]
            prior = [e for e in prior if e[0] == rel and t - e[1] <= 1800]
            if prior and prior[-1][3] == "delete":
                _, ts, s, _ = prior[-1]
                if not (session and s == session):
                    who = f"session {s}" if s else "a session"
                    deleted.append(f"{who} on this machine deleted it "
                                   f"{max(1, int((t - ts) // 60))} min ago")
            last_local = prior[-1][1] if prior else 0
            if session:
                local = {s.get("session"): s for s in claims.read_live(now=t)}
                seen = set()
                for _p, ts, s, op in prior:
                    if op != "write" or not s or s == session or s in seen:
                        continue
                    seen.add(s)
                    where = (local.get(s) or {}).get("cwd") or "unknown directory"
                    notes.append(f"session {s} on this machine ({where}) wrote it "
                                 f"{max(1, int((t - ts) // 60))} min ago")
            rows = self._fleet_rows()
            for row in rows:
                if str(row.get("machine_uuid")) == me:
                    continue
                who = row.get("machine_id") or row.get("hostname") or "another machine"
                mine = [w for w in row.get("recent_writes") or []
                        if isinstance(w, dict) and w.get("path") == rel]
                for w in mine:
                    age = t - float(w.get("ts") or 0)
                    if not 0 <= age <= 1800:
                        continue
                    if w.get("op") == "delete":
                        
                        
                        if w is mine[-1] and float(w.get("ts") or 0) > last_local:
                            deleted.append(f"{who} deleted it {max(1, int(age // 60))} min ago")
                    else:
                        notes.append(f"{who} wrote it {max(1, int(age // 60))} min ago")
            touched = claims.store_paths_touched(claims.fleet_sessions(rows, now=t))
            for s in touched.get(rel, []):
                if s.get("machine_uuid") == me:
                    continue
                notes.append("a live session edited it by hand: " + claims.describe(s, t))
            if not notes and not deleted:
                return None
            parts = []
            if deleted:
                parts.append("This entity was deleted, then re-created here: "
                             + "; ".join(deleted[:3])
                             + ". Your write went through. Unless this is a deliberate restore, "
                               "say so to user before keeping it.")
            if notes:
                parts.append("Another session is on this entity: " + "; ".join(notes[:3])
                             + ". Your write went through. Read it back with get_entity before "
                               "writing it again, and say so to user if you're duplicating work.")
            return " ".join(parts)
        except Exception:
            return None

    def delete(self, typ, key, project=None, scope=None):
        'Remove an entity. `scope` addresses one directly (see `upsert`).\n\n        The scope equality below is a safety rail: `get()` resolves upward, so without\n        it a delete aimed at a project would remove the global entity that project\n        inherits. A workspace-scoped entity is deleted through `scope`, since\n        `_scope_for` never yields `ws:`.'
        with self.lock:
            e = (self._fresh(self.entities.get(self.by_key.get((typ, scope, key))))
                 if scope else self.get(typ, key, project))
            if not e or e["scope"] != (scope or self._scope_for(project)):
                return {"deleted": None}
            write_guard.check_entity(self.root, "delete", typ, key, e["scope"], before=e,
                                     language=e.get("language"))
            
            
            
            removed = []
            for p in (e["_path"], e["_path"] + ".meta.toml", e["_path"] + ".meta.json"):
                if os.path.exists(p):
                    os.remove(p)
                    removed.append(p)
            d = os.path.dirname(e["_path"])
            if typ == "skill" and os.path.isdir(d):
                import shutil
                removed += [os.path.join(dp, f) for dp, _dns, fns in os.walk(d) for f in fns]
                shutil.rmtree(d, ignore_errors=True)
            self.graph_generation += 1
            self.entities.pop(e["uuid"], None)
            nk = NATURAL_KEY.get(typ)
            if nk:
                self.by_key.pop((typ, e["scope"], e[nk]), None)
            self._note_write(e["_path"], op="delete")
            self._fire_write_callbacks([e["_path"]])
            for p in removed or [e["_path"]]:
                self._arm_commit(p)
            return {"deleted": key}

    def enable_commit_on_write(self, clock=None, push=None, start=True):
        'Turn on debounced commit-and-push after writes; returns the CommitOnWrite, or\n        None when AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS is 0 (the feature is off and every\n        write behaves as before). Default window 3 s, capped at 30 s.'
        from .commit_on_write import CommitOnWrite
        if self.commit_on_write is not None:
            self.commit_on_write.stop()
            self.commit_on_write = None
        raw = os.environ.get("AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS", "")
        try:
            debounce = float(raw) if raw.strip() else 3.0
            if not math.isfinite(debounce):
                raise ValueError(raw)
        except ValueError:
            log.warning("agent-context: bad AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS=%r, using 3", raw)
            debounce = 3.0
        if debounce <= 0:
            return None
        kw = {"clock": clock} if clock is not None else {}
        cow = CommitOnWrite(self, debounce=debounce, cap=max(30.0, debounce * 2),
                            push=push, **kw)
        self.commit_on_write = cow
        if start:
            cow.start()
        return cow

    def _arm_commit(self, path):
        'Tell the commit timer a file changed. Separate from the write callbacks:\n        audit, session and project writes never fired those, and firing them\n        would change the content_changed broadcast. Never raises.'
        rel = self.ledger.relative(path)
        if rel is not None:
            self.ledger.add([rel])
        cow = self.commit_on_write
        if cow is None:
            return
        try:
            cow.note_write([os.path.relpath(path, self.root)])
        except Exception as e:
            log.warning("commit timer arm failed: %s", e)

    def _commit_dirty(self, message, hold_lock=True, commit_timeout=None):
        "Stage the dirty files this daemon wrote (the write ledger) and commit them.\n        Returns the git process, or None when nothing of ours is dirty. The repo's own\n        config signs the commit. A failed signing shows in the returncode, which sync()\n        has never looked at.\n\n        Only ledger paths are staged (policy): `git add -A` on the whole tree would also\n        commit every edit made outside MCP (Obsidian, an editor, a stray script).\n        A dirty path the ledger does not hold is an outside edit: it is named in\n        `outside_edits` and left uncommitted. Never server/ (a release commits it) and\n        never another machine's row, except the files relay_report writes for it.\n\n        `hold_lock=False` is for the write-triggered commit: the store lock then covers\n        only the staging, and the signing commit runs without it, so a slow signer\n        cannot hold up a write. Writes replace files atomically and never touch the\n        index, so a write that lands meanwhile is committed whole or armed again."
        with self.lock:
            
            
            
            self._discard_foreign_machine_drift()
            from . import machine as machine_mod
            me = str(machine_mod.get_machine_uuid())
            dirty = self._dirty_paths()
            pending = self.ledger.snapshot()
            stage, outside = [], []
            for rel in sorted(dirty):
                if rel.startswith("server/") or not _may_commit_row(rel, me):
                    continue
                (stage if rel in pending else outside).append(rel)
            
            self.ledger.discard(pending - dirty)
            self._note_outside_edits(outside)
            if not stage:
                return None
            add = self._git("add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
                            check=False, input="\0".join(":(literal)" + r for r in stage))
            if add.returncode != 0:
                
                
                
                
                log.warning("agent-context: staging %d store write(s) failed, retrying: %s",
                            len(stage), (add.stderr or add.stdout or "").strip()[-200:])
                return None
            staged = set(self._git("diff", "--cached", "--name-only", "-z",
                                   check=False).stdout.split("\0")) - {""}
            if not staged:
                
                self.ledger.discard(set(stage) - self._dirty_paths())
                return None
            if hold_lock:
                proc = self._commit_staged(message, commit_timeout)
                if proc.returncode == 0:
                    self.ledger.discard(staged)
                return proc
        proc = self._commit_staged(message, commit_timeout)
        if proc.returncode == 0:
            self.ledger.discard(staged)
        return proc

    def _dirty_paths(self):
        'Every changed, deleted or untracked path in the worktree (store-relative).'
        out = self._git("status", "--porcelain", "-z", "--untracked-files=all",
                        check=False).stdout
        paths_, entries, i = set(), out.split("\0"), 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            if entry[0] in "RC":
                i += 1        
            paths_.add(entry[3:])
        return paths_

    def _note_outside_edits(self, outside):
        'Remember the dirty paths no store write made, and say so once per change.'
        self.outside_edits = outside
        key = tuple(outside)
        if key and key != self._outside_logged:
            log.warning("agent-context: %d file(s) changed outside MCP are left uncommitted "
                        "(policy; edit through an MCP tool): %s", len(outside),
                        ", ".join(outside[:8]))
        self._outside_logged = key

    def record_task_writes(self, rels):
        'A server task (store_tasks) changed these store-relative paths: commit them like\n        any MCP write, tell connected relays, and reload the index so reads see them.'
        rels = [r for r in rels if r and not r.startswith("server/")]
        if not rels:
            return
        self.ledger.add(rels)
        self.reload()
        self._fire_write_callbacks([os.path.join(self.root, r) for r in rels])
        cow = self.commit_on_write
        if cow is not None:
            try:
                cow.note_write(rels)
            except Exception as e:
                log.warning("commit timer arm failed: %s", e)

    def _commit_staged(self, message, timeout=None):
        return self._git("-c", "user.name=agent", "-c", "user.email=agent@example.invalid",
                         "commit", "-m", message, check=False, timeout=timeout)

    _UNFINISHED_GIT_OPS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge",
                           "rebase-apply")

    def _unfinished_git_op(self):
        'Name of a half-finished merge, rebase or cherry-pick, or None.'
        for name in self._UNFINISHED_GIT_OPS:
            path = self._git("rev-parse", "--git-path", name, check=False).stdout.strip()
            if path and os.path.exists(os.path.join(self.root, path)):
                return name
        return None

    def commit_local(self, message=None):
        'The commit half of sync(), for the write-triggered commit. Serialized with\n        sync() on the same mutex. Returns {"committed": bool} or {"commit_skipped": why};\n        a refusal or failure leaves the tree as it was, to be retried. The signing\n        commit runs outside the store lock and under a 120 s cap.'
        with self._sync_mutex:
            truncated = self._truncated_tracked()
            if truncated:
                return {"commit_skipped": "tracked file(s) truncated to 0 bytes: "
                        + ", ".join(truncated[:5])}
            op = self._unfinished_git_op()
            if op:
                return {"commit_skipped": f"{op} present: a merge, rebase or cherry-pick "
                        "is unfinished"}
            try:
                proc = self._commit_dirty(message or f"sync {_now()}", hold_lock=False,
                                          commit_timeout=120)
            except subprocess.TimeoutExpired:
                return {"commit_skipped": "commit timed out after 120 s"}
            if proc is None:
                return {"committed": False}
            if proc.returncode != 0:
                return {"commit_skipped": "commit failed: "
                        + (proc.stderr or proc.stdout).strip()[-300:]}
            return {"committed": True}

    def _unsigned_hold(self, branch):
        'The oldest unsigned commit that publishing HEAD would carry, or None.\n\n        Never publish a range containing an unsigned commit. The sync loop and the\n        write-triggered push both call this; since policy the daemon is the only\n        publisher. GitHub does not verify signatures so it accepts an unsigned commit,\n        but ls/s1/s2 then refuse every push, and curing it costs a history rewrite plus\n        a force-push across the fleet. Caught here, while the commit is still local, it\n        is a one-command amend.'
        with self.lock:
            bad = self._first_unsigned(branch)
            if bad is not None:
                head = self._git("rev-parse", "HEAD", check=False).stdout.strip()
                
                
                if (bad == head
                        and self._git("commit", "--amend", "--no-edit", "-S",
                                      check=False).returncode == 0
                        and self._has_sig("HEAD")):
                    log.warning("agent-context: re-signed unsigned tip commit %s "
                                "before publishing", bad[:8])
                    return None
            
            
            
            return bad

    def push_all(self):
        'The push half of sync(), for the write-triggered push: HEAD to every remote,\n        origin last, one dead mirror never stalling the rest. No fetch and no merge (the\n        loop owns those); a remote that moved refuses and the loop reconciles it.\n\n        Only the snapshot of guards runs under the sync mutex. The network spans run\n        outside it, so a hung mirror cannot hold up the next commit. Returns\n        {"push": bool, ...} with `push_error` per remote, `push_deferred` or\n        `unsigned_hold` when it refused to publish.'
        with self._push_mutex:
            with self._sync_mutex:
                if self._git("status", "--porcelain", "--", "server",
                             check=False).stdout.strip():
                    return {"push": False,
                            "push_deferred": "uncommitted server/ edits (policy)"}
                branch = (self._git("rev-parse", "--abbrev-ref", "HEAD",
                                    check=False).stdout.strip() or "main")
                if branch == "HEAD" or self._unfinished_git_op():
                    return {"push": False,
                            "push_deferred": "detached HEAD or an unfinished merge/rebase"}
                bad = self._unsigned_hold(branch)
                if bad is not None:
                    return {"push": False, "unsigned_hold": bad}
                sha =self._git("rev-parse", "HEAD", check=False).stdout.strip()
                remotes = self._git("remote", check=False).stdout.split()
            errs = {}
            for r in [r for r in remotes if r != "origin"] + [r for r in remotes
                                                              if r == "origin"]:
                pu = self._git("push", r, f"{sha}:refs/heads/{branch}", check=False,
                               timeout=30)
                if pu.returncode != 0:
                    errs[r] = (pu.stderr or pu.stdout)[-500:]
            out = {"push": not errs}
            if errs:
                out["push_error"] = errs
            return out

    def set_write_callback(self, cb: Callable[[list[str]], None]) -> None:
        'Register `cb`, called with the changed store-relative paths after every\n        upsert and delete. Callbacks run under the store lock and must not block.'
        self._write_callbacks.append(cb)

    def _fire_write_callbacks(self, paths: list[str]) -> None:
        'Never raises: a listener is a notification, and must not be able to fail the\n        write it announces.'
        if not self._write_callbacks:
            return
        rel = [os.path.relpath(p, self.root) for p in paths]
        for cb in self._write_callbacks:
            try:
                cb(rel)
            except Exception as e:
                log.warning("write callback failed: %s", e)

    
    
    
    
    
    
    
    
    
    _SSH_CMD = None

    def _ssh_cmd(self):
        if self._SSH_CMD is None:
            self._SSH_CMD = _default_ssh_cmd(self.root)
        return self._SSH_CMD

    def _register_merge_drivers(self):
        "Register the store's merge drivers in this clone, before merging.\n\n        The definitions live in global/scripts/register-merge-drivers.py. A machine\n        that merged without a driver would stop its sync on the conflict that driver\n        resolves, so the daemon registers them before every merge. There is no copy of\n        the list here to drift from the script.\n\n        Best effort, like the script's own exit 0: a machine that cannot register a\n        driver still gets git's ordinary merge. A failure here must never stop a sync."
        script = os.path.join(self.root, "global", "scripts",
                              "register-merge-drivers.py")
        if not os.path.exists(script):
            return
        with contextlib.suppress(Exception):
            subprocess.run([sys.executable, script, self.root], capture_output=True,
                           text=True, timeout=20)

    def _git(self, *args, check=True, timeout=None, input=None):
        
        
        
        env = None
        if timeout is not None:
            env = {**os.environ, "GIT_SSH_COMMAND": self._ssh_cmd(),
                   "GIT_TERMINAL_PROMPT": "0"}
        if "commit" in args or "merge" in args:
            
            
            env = {**(env if env is not None else os.environ), SERVER_TASK_ENV: "1"}
        cmd = ["git", "-C", self.root, *args]
        if timeout is None:
            return subprocess.run(cmd, capture_output=True, text=True, check=check, env=env,
                                  input=input)
        
        
        
        
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                proc.communicate(timeout=5)
            op = args[0] if args else "git"
            return subprocess.CompletedProcess(
                cmd, returncode=124, stdout="",
                stderr=f"git {op} timed out after {timeout}s (remote unreachable?)")
        res = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        return res

    def _remote_mains(self, branch):
        '(remotes, refs): every configured remote, and the subset of their\n        `<remote>/<branch>` tracking refs that currently exist.'
        remotes = self._git("remote", check=False).stdout.split()
        refs = [f"{r}/{branch}" for r in remotes
                if self._git("rev-parse", "--verify", "--quiet", f"{r}/{branch}",
                             check=False).returncode == 0]
        return remotes, refs

    def _reconcile_diverged(self, refs, branch):
        'Make every remote tip reachable from HEAD, so that every push can then\n        fast-forward. Returns (merged_refs, {remote: orphan_sha}, stranded_refs).\n\n        A mirror set splits because sync() rebases local commits onto the newest tip,\n        rewriting their SHAs, and then pushes each mirror separately. Any mirror that\n        already accepted the pre-rebase SHA is left holding a commit unreachable from\n        the new history, and non-fast-forward is then permanent: the base picker keeps\n        choosing one tip and stranding the rest, so every cycle re-reports\n        diverged_remotes and every push is refused. Two dispositions, both lossless,\n        for each tip HEAD does not contain:\n\n        - **Rebase orphan**: `git cherry HEAD <ref>` marks every commit the tip has\n          and HEAD lacks with `-`, meaning a patch-equivalent commit is already in\n          HEAD. The tip holds no unique work, only pre-rebase spellings of work that\n          landed. Recorded for a force-with-lease pinned to that exact sha, so a\n          mirror that moved after the fetch refuses the push instead of losing a\n          commit. Never a bare --force.\n        - **Real work** (any `+` line): commits from another machine this one has\n          not integrated. Merged into HEAD, which preserves them and makes the tip an\n          ancestor, so its next push fast-forwards with no force at all.\n\n        A tip whose merge conflicts is left alone and reported: that is a content\n        conflict between machines and wants a human, not a strategy. Runs on every\n        cycle and not only under diverged_remotes: the cost is one\n        `merge-base --is-ancestor` per remote, and the invariant holds without\n        exceptions: after integration, every remote tip is reachable from HEAD or named\n        as an orphan.'
        merged, orphans, stranded = [], {}, []
        for ref in refs:
            if self._git("merge-base", "--is-ancestor", ref, "HEAD",
                         check=False).returncode == 0:
                continue                    
            remote = ref[:-(len(branch) + 1)]
            cherry = self._git("cherry", "HEAD", ref, check=False)
            lines = [ln for ln in cherry.stdout.splitlines() if ln.strip()]
            if cherry.returncode == 0 and lines and all(ln.startswith("-") for ln in lines):
                sha = self._git("rev-parse", ref, check=False).stdout.strip()
                if sha:
                    orphans[remote] = sha
                    log.info("agent-context: %s holds only rebase-orphaned commits "
                             "(%d, all patch-equivalent to HEAD) — will force-with-lease "
                             "onto %s", ref, len(lines), sha[:8])
                continue
            mrg = self._git("merge", "--no-edit", ref, check=False, timeout=30)
            if mrg.returncode == 0:
                merged.append(ref)
                log.info("agent-context: merged straggler %s — it carried commits no "
                         "other remote had", ref)
            else:
                self._git("merge", "--abort", check=False)
                stranded.append(ref)
                log.warning("agent-context: %s has unique commits that will not merge "
                            "cleanly — needs a human", ref)
        return merged, orphans, stranded

    def _has_sig(self, rev):
        'True when commit `rev` carries a `gpgsig` header. Signature presence is\n        the test, not validity: verifying against a key would fail open into "hold\n        every push" on a machine whose allowed_signers is not configured, and the\n        defect this guards is a commit with no gpgsig header at all.'
        out = self._git("cat-file", "commit", rev, check=False).stdout
        return any(ln.startswith("gpgsig ") for ln in out.split("\n\n", 1)[0].splitlines())

    def _first_unsigned(self, branch):
        'The oldest commit on HEAD that origin/<branch> does not have and that\n        carries no signature, or None.'
        if self._git("rev-parse", "--verify", "--quiet",
                     f"origin/{branch}", check=False).returncode != 0:
            return None
        revs = self._git("rev-list", f"origin/{branch}..HEAD", check=False).stdout.split()
        for c in reversed(revs):          
            if not self._has_sig(c):
                return c
        return None

    def _truncated_tracked(self):
        'Tracked files whose worktree copy is empty but whose committed copy is not.\n\n        This is the signature of a half-finished git operation, not of local work.\n        Nothing in this store empties a tracked file: a memory, doc, script or hook is\n        edited or deleted, never blanked. So a non-empty -> 0-byte transition is\n        wreckage, and committing it publishes that wreckage to every machine as though\n        somebody meant it. A `fatal: Cannot autostash` that aborts an integration\n        mid-flight can leave files truncated this way.\n\n        Checked before anything is staged, so refusing costs nothing and needs no\n        `git reset` to undo. The worktree is never touched: the bytes stay where they\n        are for a human to look at.'
        out = self._git("diff", "--name-only", "HEAD", "--", ":!server", check=False)
        bad = []
        for rel in (out.stdout or "").splitlines():
            rel = rel.strip()
            if not rel:
                continue
            try:
                if os.path.getsize(os.path.join(self.root, rel)) != 0:
                    continue
            except OSError:
                continue          
            sz = self._git("cat-file", "-s", f"HEAD:{rel}", check=False)
            with contextlib.suppress(ValueError, AttributeError):
                if int((sz.stdout or "").strip()) > 0:
                    bad.append(rel)
        return bad

    def _resolve_observation_collisions(self):
        'Mid-conflict repair for an audit-observation id collision. Returns\n        {path: new_id} when it resolved the whole conflict, else None.\n\n        Called with a merge still in progress. An add/add conflict under\n        global/audit-observations/ is no disagreement about content: it is two\n        machines that independently handed the same number to different defects, and\n        the correct resolution is always the same: keep both records, renumber one.\n        The obvious shortcut (take one side) would destroy a real observation.\n\n        Theirs keeps the contested number. It is the side already published to the\n        other machines, so renumbering it would strand references everywhere else,\n        while ours is still unpushed and can be renumbered for free.\n\n        Two conditions, both required, or this declines and leaves the merge for a\n        human:\n\n        - Every conflicted path is an observation file. One unrelated conflict means\n          this is an ordinary divergence that happens to include observations.\n        - Every one of them is add/add, i.e. no stage-1 ancestor. A stage 1 means both\n          machines edited the same observation, which is a content conflict with a\n          real question in it: resolving that by renumbering would fork one record\n          into two and keep both.'
        import re
        unmerged = self._git("diff", "--name-only", "--diff-filter=U", check=False)
        paths = [p for p in (unmerged.stdout or "").splitlines() if p.strip()]
        if not paths:
            return None
        pat = re.compile(r"^global/audit-observations(?:-archive)?/(\d+)\.json$")
        if not all(pat.match(p) for p in paths):
            return None
        for p in paths:
            stages = self._git("ls-files", "-u", "--", p, check=False)
            if any(line.split()[2] == "1"
                   for line in (stages.stdout or "").splitlines() if line.split()):
                return None     

        from .audit import _audit_id_taken, _audit_next_id
        
        
        
        
        
        next_id = _audit_next_id(self) - 1
        renumbered = {}
        for p in paths:
            ours = self._git("show", f":2:{p}", check=False)
            theirs = self._git("show", f":3:{p}", check=False)
            if ours.returncode != 0 or theirs.returncode != 0:
                return None     
            try:
                mine = json.loads(ours.stdout)
            except ValueError:
                return None
            next_id += 1
            
            
            while _audit_id_taken(self, next_id):
                next_id += 1
            mine["id"] = next_id
            base = p.rsplit("/", 1)[0]
            newp = f"{base}/{next_id:04d}.json"
            full = os.path.join(self.root, newp)
            if os.path.exists(full):
                return None     
            
            
            
            
            self._write_atomic(os.path.join(self.root, p), theirs.stdout)
            self._write_atomic(
                full, json.dumps(mine, indent=1, ensure_ascii=False) + "\n")
            self._git("add", "--", p, newp, check=False)
            renumbered[p] = next_id

        
        
        done = self._git("-c", "user.name=agent", "-c", "user.email=agent@example.invalid",
                         "commit", "--no-edit", check=False)
        if done.returncode != 0:
            return None         
        log.warning("agent-context: resolved %d audit-observation id collision(s) by "
                    "renumbering the local side: %s. Both records are kept.",
                    len(renumbered),
                    ", ".join(f"{p} -> {i:04d}" for p, i in renumbered.items()))
        return renumbered

    def _discard_foreign_machine_drift(self):
        "Put every tracked file under another machine's machines/<uuid>/ back to its\n        HEAD content. Returns the paths restored.\n\n        Only the owning daemon writes its row. A local copy that differs from HEAD came\n        from a merge, abort or autostash, never from that machine, so it is stale, and\n        committing it would make the owner conflict on its own row every cycle after.\n        Excluding the path from `add` alone is not enough: the dirty file would ride\n        every `merge --autostash`.\n\n        Exception: UPLOADED_ROW_FILES (deps.json, token-usage/). No machine writes those\n        locally; relay_report on the receiving daemon is their only writer, so its write is\n        never stale drift -- see _commit_dirty's matching `git add` exception."
        from . import machine as machine_mod
        me = str(machine_mod.get_machine_uuid())
        st = self._git("status", "--porcelain", "--untracked-files=no", "--",
                       "machines/", check=False)
        restored = []
        for line in (st.stdout or "").splitlines():
            p = line[3:].strip()
            parts = p.split("/")
            if len(parts) < 3 or parts[0] != "machines" or parts[1] == me:
                continue
            if _uploaded_row_file(parts[2:]):
                continue
            head = self._git("show", f"HEAD:{p}", check=False)
            if head.returncode != 0:
                continue
            self._write_atomic(os.path.join(self.root, p), head.stdout)
            self._git("add", "--", p, check=False)   
            restored.append(p)
        if restored:
            log.warning("agent-context: discarded local drift in %d file(s) owned by "
                        "another machine: %s", len(restored),
                        ", ".join(restored[:5]))
        return restored

    def _resolve_own_machine_row(self):
        "Mid-conflict: take OURS for a conflict on this machine's own\n        machines/<uuid>/ files. Returns True when that left the merge with no\n        conflict at all and it was committed.\n\n        This machine's daemon is the only writer of those files, so the local side is\n        always current and there is no question for a human. Other\n        conflicts stay conflicted for the resolvers after this one."
        from . import machine as machine_mod
        me = str(machine_mod.get_machine_uuid())
        unmerged = self._git("diff", "--name-only", "--diff-filter=U", check=False)
        paths = [p for p in (unmerged.stdout or "").splitlines() if p.strip()]
        mine = [p for p in paths if p.startswith(f"machines/{me}/")]
        if not mine:
            return False
        for p in mine:
            ours = self._git("show", f":2:{p}", check=False)
            if ours.returncode != 0:
                return False     
            self._write_atomic(os.path.join(self.root, p), ours.stdout)
            self._git("add", "--", p, check=False)
        log.warning("agent-context: kept this machine's own side of %d conflicted "
                    "file(s) under machines/%s/", len(mine), me)
        if len(mine) != len(paths):
            return False
        done = self._git("-c", "user.name=agent", "-c", "user.email=agent@example.invalid",
                         "commit", "--no-edit", check=False)
        return done.returncode == 0

    def _collided_local_observations(self):
        'Mid-conflict: the local side of every audit-observation id collision in\n        the merge, as [(path, text)], regardless of what else is conflicted.\n\n        The sibling above resolves a merge that is only collisions. This is for the\n        merge that is going to a human anyway: it collects what the abort is about to\n        throw away, so `_renumber_after_abort` can move the local records out of the\n        way on a clean tree. Same refusal as the sibling for a stage-1 ancestor: an\n        edit-vs-edit on one observation is a real conflict and stays one.'
        import re
        unmerged = self._git("diff", "--name-only", "--diff-filter=U", check=False)
        pat = re.compile(r"^global/audit-observations(?:-archive)?/(\d+)\.json$")
        out = []
        for p in (unmerged.stdout or "").splitlines():
            p = p.strip()
            if not p or not pat.match(p):
                continue
            stages = self._git("ls-files", "-u", "--", p, check=False)
            if any(line.split()[2] == "1"
                   for line in (stages.stdout or "").splitlines() if line.split()):
                continue
            ours = self._git("show", f":2:{p}", check=False)
            if ours.returncode != 0:
                continue
            try:
                json.loads(ours.stdout)
            except ValueError:
                continue
            out.append((p, ours.stdout))
        return out

    def _renumber_after_abort(self, pending):
        'On a clean tree after `merge --abort`: move each local colliding record to\n        the next free id and commit. Returns {old_path: new_id} for what moved.\n\n        The contested number stays with the side that is already on a mirror, as in\n        `_resolve_observation_collisions`; the local record is unpushed and can be\n        renumbered for free. The commit is local and ordinary: the next sync cycle\n        merges it like any other, and that merge no longer has this collision in it.\n        Best-effort throughout: a failure here leaves the tree as the abort left it,\n        which is the state the caller already reports.'
        from .audit import _audit_id_taken, _audit_next_id
        moved = {}
        try:
            
            
            next_id = _audit_next_id(self) - 1
            for p, text in pending:
                try:
                    rec = json.loads(text)
                except ValueError:
                    continue
                base = p.rsplit("/", 1)[0]
                for _ in range(1000):
                    next_id += 1
                    newp = f"{base}/{next_id:04d}.json"
                    if not _audit_id_taken(self, next_id):
                        break
                else:
                    return moved
                rec["id"] = next_id
                self._write_atomic(os.path.join(self.root, newp),
                                   json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
                self._git("rm", "-q", "--", p, check=False)
                self._git("add", "--", newp, check=False)
                moved[p] = next_id
            if not moved:
                return moved
            msg = "renumber audit observation(s) whose id collided with a mirror: " + ", ".join(
                f"{p.rsplit('/', 1)[1][:-5]} -> {i:04d}" for p, i in moved.items())
            done = self._git("-c", "user.name=agent", "-c", "user.email=agent@example.invalid",
                             "commit", "-q", "-m", msg, check=False)
            if done.returncode != 0:
                log.warning("agent-context: could not commit the renumbered observation(s): %s",
                            (done.stderr or done.stdout)[-300:])
                return {}
            log.warning("agent-context: %s. Other merge conflicts still need a human.", msg)
            return moved
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            log.warning("agent-context: renumber after abort failed: %s", e)
            return moved

    

    _PARK_AFTER_CYCLES = 4          
    _PARK_IDLE_SECS = 1200.0        

    def _park_dirty_server_if_abandoned(self, streak: int):
        'Move uncommitted server/ edits out of the main checkout onto their own\n        branch once they look abandoned; return {branch, commit, files} or None.\n\n        Two conditions, both required: the deferral has run `_PARK_AFTER_CYCLES`\n        cycles, and the newest dirty file under server/ is older than\n        `_PARK_IDLE_SECS`. A person or session still typing keeps the tree alone;\n        the hook that refuses these edits at the source is the real guard, and this\n        is the backstop for a machine that has not received it (the hook itself\n        arrives through the merge that a dirty tree defers).\n\n        Nothing is discarded: every changed file goes to a `parked/<host>-<stamp>`\n        branch in a linked worktree, committed (gate skipped: work in progress, not\n        landing), and main is restored to its index with checkout-index. Best-effort:\n        any failure leaves the tree as it was and the deferral continues.'
        if streak < self._PARK_AFTER_CYCLES:
            return None
        try:
            st = self._git("status", "--porcelain", "--", "server", check=False).stdout
            entries = [(ln[:2], ln[3:]) for ln in st.splitlines() if len(ln) > 3]
            if not entries:
                return None
            newest = 0.0
            for _code, rel in entries:
                p = os.path.join(self.root, rel)
                if os.path.exists(p):
                    newest = max(newest, os.path.getmtime(p))
            if newest and time.time() - newest < self._PARK_IDLE_SECS:
                return None
            import shutil
            import socket
            host = socket.gethostname().split(".")[0] or "host"
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            branch = f"parked/{host}-{stamp}"
            wt = os.path.join(self.root, ".claude", "worktrees", f"parked-{host}-{stamp}")
            if self._git("worktree", "add", "-q", wt, "-b", branch, "HEAD",
                         check=False).returncode != 0:
                return None
            for _code, rel in entries:
                src, dst = os.path.join(self.root, rel), os.path.join(wt, rel)
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                elif os.path.exists(dst):
                    os.remove(dst)                      
            subprocess.run(["git", "-C", wt, "add", "-A", "--", "server"],
                           capture_output=True, text=True, check=False)
            msg = (f"parked: uncommitted server/ edits found in the main checkout on {host}\n\n"
                   f"{len(entries)} file(s) sat uncommitted under server/ for {streak} sync "
                   f"cycles, blocking every merge on this machine. Preserved here unchanged, "
                   f"unreviewed and ungated. Land them through a worktree if wanted.\n\n"
                   + "\n".join(f"  {c} {r}" for c, r in entries))
            env = {**os.environ, "AGENT_CONTEXT_SKIP_GATE": "1", SERVER_TASK_ENV: "1"}
            done = subprocess.run(["git", "-C", wt, "-c", "user.name=agent",
                                   "-c", "user.email=agent@example.invalid",
                                   "commit", "-q", "-m", msg],
                                  capture_output=True, text=True, check=False, env=env)
            if done.returncode != 0:
                log.warning("agent-context: could not park dirty server/ edits: %s",
                            (done.stderr or done.stdout)[-300:])
                self._git("worktree", "remove", "--force", wt, check=False)
                self._git("branch", "-D", branch, check=False)
                return None
            sha = subprocess.run(["git", "-C", wt, "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, check=False).stdout.strip()
            
            
            tracked = [r for c, r in entries if c.strip() != "??"]
            if tracked:
                self._git("checkout-index", "-f", "--", *tracked, check=False)
            for c, r in entries:
                if c.strip() == "??":
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(self.root, r))
            self._git("worktree", "remove", "--force", wt, check=False)
            still = self._git("status", "--porcelain", "--", "server", check=False).stdout.strip()
            log.warning("agent-context: parked %d abandoned server/ edit(s) on branch %s (%s); "
                        "main checkout is %s", len(entries), branch, sha,
                        "clean again" if not still else "STILL dirty: " + still[:200])
            return {"branch": branch, "commit": sha, "files": [r for _c, r in entries]}
        except Exception as e:          
            log.warning("agent-context: parking dirty server/ edits failed: %s", e)
            return None

    
    
    
    
    
    
    
    
    
    
    
    

    _GUARD_FETCH_SECS = 60.0

    
    
    
    
    
    
    
    _MAINTENANCE_SECS = 3600.0

    @staticmethod
    def _stale_guard_enabled() -> bool:
        return os.environ.get("AGENT_CONTEXT_STALE_GUARD", "1").strip().lower() not in (
            "0", "false", "no", "off")

    def _maybe_fetch_for_guard(self) -> None:
        if not self._stale_guard_enabled():
            return
        if not os.path.isdir(os.path.join(self.root, ".git")):
            return
        now = time.time()
        if now - self._guard_fetch_at < self._GUARD_FETCH_SECS:
            return
        self._guard_fetch_at = now    
        with contextlib.suppress(Exception):
            self._git("fetch", "--all", "--prune", check=False, timeout=10)

    def _refuse_if_stale(self, path) -> None:
        'Raise StaleWriteError when a remote-tracking main holds a commit touching\n        `path` that HEAD does not. Silent when git cannot say.'
        if not self._stale_guard_enabled():
            return
        try:
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(self.root))
            if rel.startswith(".."):
                return
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
            if not branch or branch == "HEAD":
                return
            _remotes, refs = self._remote_mains(branch)
            for ref in refs:
                if self._git("merge-base", "--is-ancestor", ref, "HEAD",
                             check=False).returncode == 0:
                    continue           
                out = self._git("log", f"HEAD..{ref}", "--format=%h %cr", "--", rel,
                                check=False).stdout.strip()
                if not out:
                    continue
                first = out.splitlines()[0]
                raise StaleWriteError(
                    f"stale write refused: {rel} was changed on {ref} (commit {first}) "
                    f"and that change is not integrated into this checkout yet. Writing "
                    f"now would produce a merge conflict on another machine. Wait for "
                    f"the daemon's next sync (every 5 minutes), then re-read the entity "
                    f"and retry.")
        except StaleWriteError:
            raise
        except Exception as e:      
            log.warning("agent-context: stale-write guard skipped: %s", e)

    

    def _ensure_fleet_refspecs(self) -> None:
        "Once per process: make `fetch --all` bring every remote's refs/fleet/* in as\n        refs/fleet/<remote>/*, so read_all can see rows that never reached main."
        if self._fleet_refspecs_done:
            return
        self._fleet_refspecs_done = True
        with contextlib.suppress(Exception):
            for r in self._git("remote", check=False).stdout.split():
                want = f"+refs/fleet/*:refs/fleet/{r}/*"
                have = self._git("config", "--get-all", f"remote.{r}.fetch",
                                 check=False).stdout.split("\n")
                if want not in have:
                    self._git("config", "--add", f"remote.{r}.fetch", want, check=False)

    def _publish_fleet_ref(self) -> list[str]:
        "Push this machine's status row to refs/fleet/<machine_id> on every remote\n        whose copy differs. Returns the remotes updated. Never raises past the caller's\n        suppress; a remote that refuses is simply not updated this cycle."
        from . import fleet, machine
        uuid_ = machine.get_machine_uuid()
        mid = machine.get_chezmoi_machine_id() or str(uuid_)
        row_path = fleet.status_path(self.root, uuid_)
        if not row_path.exists():
            return []
        
        
        
        try:
            from . import claims, daemon
            text = fleet.with_live(row_path.read_text(encoding="utf-8"),
                                   sessions=claims.read_live(),
                                   recent_writes=self.recent_writes(),
                                   starts_last_hour=daemon.starts_last_hour())
        except Exception:
            try:
                text = row_path.read_text(encoding="utf-8")
            except OSError:
                return []
        hashed = subprocess.run(["git", "-C", self.root, "hash-object", "-w", "--stdin"],
                                input=text, capture_output=True, text=True, check=False)
        blob = hashed.stdout.strip() if hashed.returncode == 0 else ""
        if not blob:
            return []
        remotes = self._git("remote", check=False).stdout.split()
        stale = []
        for r in remotes:
            have = self._git("rev-parse", "--verify", "--quiet",
                             f"refs/fleet/{r}/{mid}:daemon-status.json", check=False)
            if have.returncode != 0 or have.stdout.strip() != blob:
                stale.append(r)
        if not stale:
            return []
        tree = subprocess.run(["git", "-C", self.root, "mktree"],
                              input=f"100644 blob {blob}\tdaemon-status.json\n",
                              capture_output=True, text=True, check=False)
        if tree.returncode != 0:
            return []
        
        
        
        
        
        
        
        
        sign = self._git("config", "--type=bool", "--get", "commit.gpgsign",
                         check=False).stdout.strip() == "true"
        commit = self._git("commit-tree", *(["-S"] if sign else []), tree.stdout.strip(),
                           "-m", f"fleet status: {mid}", check=False)
        if commit.returncode != 0:
            log.warning("agent-context: fleet ref not published (commit-tree failed): %s",
                        (commit.stderr or "")[-200:])
            return []
        sha = commit.stdout.strip()
        done = []
        for r in stale:
            pu = self._git("push", "--force", "--quiet", r, f"{sha}:refs/fleet/{mid}",
                           check=False, timeout=20)
            if pu.returncode == 0:
                done.append(r)
                
                
                self._git("update-ref", f"refs/fleet/{r}/{mid}", sha, check=False)
            else:
                log.info("agent-context: fleet ref not accepted by %s: %s", r,
                         (pu.stderr or pu.stdout)[-160:].strip())
        return done

    def divergence(self):
        "Measure HEAD against every remote-tracking ref: {remote: {ahead, behind}}.\n\n        This is an independent audit of what sync() claims, and it exists because\n        sync() reports trouble by returning it, so every failure shape the result\n        dict does not name is recorded as a healthy heartbeat, and the health signal an\n        agent reads would point nowhere.\n\n        The distinction that makes this trustworthy: sync_verdict interprets a\n        report, this measures an outcome. A stall has to survive both, and the two\n        cannot fail the same way: a bug in sync()'s error reporting cannot make\n        `rev-list --count` return zero.\n\n        Local refs only: no network, no ssh, no 1Password reads, so it is safe on\n        every cycle including a backed-off one. It measures what the cycle's own\n        fetch just wrote. `behind` is the number that matters -- commits a mirror\n        holds that HEAD does not -- since `ahead` is the ordinary state between the\n        commit and the push.\n\n        Never raises: a health probe that can throw would take down the sync loop it\n        exists to observe. An unreadable ref is simply omitted."
        out = {}
        try:
            refs = self._git("for-each-ref", "--format=%(refname)",
                             "refs/remotes/", check=False)
        except Exception:
            return out
        for full in (refs.stdout or "").split():
            
            
            
            if full.endswith("/HEAD"):
                continue        
            ref = full.removeprefix("refs/remotes/")
            try:
                rl = self._git("rev-list", "--left-right", "--count", f"HEAD...{ref}",
                               check=False)
                if rl.returncode != 0:
                    continue
                ahead, behind = (int(n) for n in rl.stdout.split())
            except Exception:
                continue
            out[ref] = {"ahead": ahead, "behind": behind}
        return out

    def sync(self, message=None, push=True):
        "Commit local changes, then fetch / merge / push across ALL remotes so\n        every remote converges on the newest commit.\n\n        Fetches all remotes, merges the furthest-ahead remote tip (never rebases:\n        see the note at the merge itself), then pushes HEAD to every remote. If the\n        remotes have diverged from each other (a mirror split), integration falls\n        back to origin and `diverged_remotes` is flagged, and because that base\n        contains only some of the tips, `_reconcile_diverged` then settles the rest:\n        merging any tip that carries unique work, force-with-lease'ing any tip whose\n        extra commits are pre-rebase spellings of work already in HEAD, and\n        naming in `stranded_remotes` the ones that conflict and want a human. Nothing\n        is force-overwritten without a lease pinned to a sha proved redundant.\n\n        Locking: syncs are serialized against each other by `_sync_mutex` (the\n        5-min loop and an explicit sync() call can overlap), but the\n        store lock is held only for the spans that mutate the worktree/index\n        (commit, merge, reload). The network spans (fetch and push) run\n        outside the store lock: fetch only updates remote-tracking refs and push\n        only reads objects, both safe alongside reads and writes. A slow or dead\n        remote inside the lock would stall every MCP request for the full network\n        timeout."
        with self._sync_mutex:
            results = {}
            
            
            
            
            
            
            
            
            
            
            
            
            
            truncated = self._truncated_tracked()
            if truncated:
                results["pull"] = False
                results["commit_skipped"] = "tracked file(s) truncated to 0 bytes"
                results["truncated"] = truncated[:20]
                results["truncated_hold"] = truncated[:20]
                log.error("agent-context: REFUSING to sync — %d tracked file(s) are "
                          "0 bytes in the worktree but non-empty in HEAD, which means an "
                          "interrupted git operation, not local work: %s. "
                          "Nothing has been staged or changed. Restore them from HEAD or "
                          "finish the interrupted operation by hand.",
                          len(truncated), ", ".join(truncated[:5]))
                return results

            with self.lock:
                if self._commit_dirty(message or f"sync {_now()}") is not None:
                    results["committed"] = True

                branch = (self._git("rev-parse", "--abbrev-ref", "HEAD",
                                    check=False).stdout.strip() or "main")

            
            
            
            
            
            
            
            
            
            global _SERVER_DIRTY_LOGGED, _SERVER_DIRTY_STREAK
            if self._git("status", "--porcelain", "--", "server",
                         check=False).stdout.strip():
                _SERVER_DIRTY_STREAK += 1
                results["pull"] = False
                results["pull_skipped"] = "uncommitted server/ edits (policy)"
                results["pull_skip_streak"] = _SERVER_DIRTY_STREAK
                if not _SERVER_DIRTY_LOGGED:
                    log.warning("agent-context: sync deferring merge/push — server/ has "
                                "uncommitted edits that --autostash could revert (policy)")
                    _SERVER_DIRTY_LOGGED = True
                
                
                
                
                
                parked = self._park_dirty_server_if_abandoned(_SERVER_DIRTY_STREAK)
                if parked:
                    results["parked"] = parked
                    _SERVER_DIRTY_STREAK = 0
                    _SERVER_DIRTY_LOGGED = False
                return results
            _SERVER_DIRTY_LOGGED = False
            _SERVER_DIRTY_STREAK = 0

            
            
            
            
            
            
            results["network"] = True
            self._ensure_fleet_refspecs()
            fetch = self._git("fetch", "--all", "--prune", check=False, timeout=30)
            results["fetch"] = fetch.returncode == 0
            if results["fetch"]:
                self._guard_fetch_at = time.time()
            else:
                results["fetch_error"] = (fetch.stderr or fetch.stdout)[-400:]

            
            
            
            now = time.time()
            if now - self._maintenance_at >= self._MAINTENANCE_SECS:
                self._maintenance_at = now
                with contextlib.suppress(Exception):
                    maint = self._git("maintenance", "run", "--auto", check=False,
                                      timeout=30)
                    results["maintenance"] = maint.returncode == 0
            
            
            
            
            
            with contextlib.suppress(Exception):
                pushed = self._publish_fleet_ref()
                if pushed:
                    results["fleet_ref_pushed"] = pushed

            with self.lock:
                remotes, refs = self._remote_mains(branch)
                if refs:
                    
                    
                    
                    base = next((r for r in refs
                                 if all(self._git("merge-base", "--is-ancestor", o, r,
                                                  check=False).returncode == 0 for o in refs)),
                                None)
                    if base is None:
                        base = f"origin/{branch}" if f"origin/{branch}" in refs else refs[0]
                        results["diverged_remotes"] = True
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    
                    self._register_merge_drivers()
                    mrg = self._git("merge", "--no-edit", "--autostash", base,
                                    check=False, timeout=30)
                    results["pull"] = mrg.returncode == 0
                    results["pull_via"] = "merge"
                    if not results["pull"] and self._resolve_own_machine_row():
                        results["pull"] = True
                        results["pull_via"] = "merge+own-row"
                    if not results["pull"]:
                        
                        
                        
                        
                        renumbered = self._resolve_observation_collisions()
                        if renumbered:
                            results["pull"] = True
                            results["pull_via"] = "merge+renumber"
                            results["renumbered"] = renumbered
                    if not results["pull"]:
                        
                        
                        
                        
                        
                        
                        
                        pending = self._collided_local_observations()
                        self._git("merge", "--abort", check=False)  
                        if pending:
                            moved = self._renumber_after_abort(pending)
                            if moved:
                                results["renumbered_after_abort"] = moved
                        results["pull_error"] = _scrub_stale_git_hints(
                            mrg.stderr or mrg.stdout)[-400:]
                        
                        
                        
                        
                        
                        
                        
                        
                        if "cannot autostash" in results["pull_error"].lower():
                            results["autostash_failed"] = True
                            log.error("agent-context: merge could not autostash on this "
                                      "host — the worktree may be mid-operation. This is "
                                      "an environment fault, not a content conflict. "
                                      "Check that `git stash` works here.")
                        
                        
                        
                        results["base"] = base
                        cnt = self._git("rev-list", "--count", f"HEAD..{base}",
                                        check=False)
                        with contextlib.suppress(ValueError, AttributeError):
                            results["behind"] = int(cnt.stdout.strip())
                    
                    
                    
                    if results.get("pull"):
                        merged, orphans, stranded = self._reconcile_diverged(refs, branch)
                        if merged:
                            results["merged_stragglers"] = merged
                        if orphans:
                            results["rebase_orphans"] = orphans
                        if stranded:
                            results["stranded_remotes"] = stranded
                else:
                    results["pull"] = True  

                self.reload()

            
            
            
            
            
            if push and results.get("pull"):
                bad = self._unsigned_hold(branch)
                if bad is not None:
                    results["push"] = False
                    results["unsigned_hold"] = bad
                    return results
                orphans = dict(results.get("rebase_orphans") or {})
                
                
                
                needs_human = orphans.pop("origin", None)
                if needs_human:
                    results["orphan_needs_human"] = {"origin": needs_human}
                
                
                
                order = ([r for r in remotes if r in orphans]
                         + [r for r in remotes if r not in orphans and r != "origin"]
                         + [r for r in remotes if r == "origin"])
                errs = {}
                for r in order:
                    args = ["push", r, f"HEAD:refs/heads/{branch}"]
                    lease = orphans.get(r)
                    if lease:
                        
                        
                        
                        args.insert(1, f"--force-with-lease=refs/heads/{branch}:{lease}")
                    pu = self._git(*args, check=False, timeout=30)
                    if pu.returncode != 0:
                        
                        
                        errs[r] = (pu.stderr or pu.stdout)[-2000:]
                    elif lease:
                        log.warning("agent-context: reconciled rebase-orphaned mirror %s "
                                    "(was %s) with a lease-pinned force-push", r, lease[:8])
                results["push"] = not errs
                
                
                
                if self._corruption_suspected(errs, order):
                    empty = self._empty_loose_objects()
                    if empty:
                        results["local_corruption"] = {"count": len(empty),
                                                       "empty_objects": empty[:5]}
                
                
                
                
                
                global _PUSH_FAIL_STREAK
                if errs:
                    _PUSH_FAIL_STREAK += 1
                    results["push_error"] = errs
                    results["push_fail_streak"] = _PUSH_FAIL_STREAK
                else:
                    _PUSH_FAIL_STREAK = 0
            return results
