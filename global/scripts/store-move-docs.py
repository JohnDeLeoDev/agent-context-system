#!/usr/bin/env python3
'A client machine holds no copy of project docs, so a move made from one is get_doc,\nupsert_doc, delete_entity, with the whole body passing through the agent\'s context.\nThis runs in the daemon, on the files.\n\n--scope is `global`, `project:<Name>` or `ws:<Name>`. Paths are doc paths, as get_doc\ntakes them. The default run is a report and changes nothing; --apply writes.\n\nA move re-keys the doc\'s frontmatter (`path`, `uuid`) and rewrites, in every live file,\nthe wikilinks, typed links and get_doc pointers the move would leave dangling, label\nkept. Files under an archive segment are frozen records and are not rewritten. A row\nwhose source is missing or whose target exists is refused and named; the other rows\nstill run, and the exit code is 1.\n\nA split moves each named heading, with its subsections, to the end of the --to doc\n(created when absent) and leaves one pointer line where the section was.\n\nThe move, re-key and rewrite code is store-compact.py\'s, imported from it: one copy.\nFrom a relay, pass rows through `run_store_task("store-move-docs", [...], stdin=...)`;\n--file names a path on the daemon host.\n\nAGENT_CONTEXT_STORE names the store to act on (default ~/.agent-context).\n\nObservations guarded: #525.'
import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load_compact():
    spec = importlib.util.spec_from_file_location(
        "store_compact", os.path.join(HERE, "store-compact.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load_compact()


def norm_scope(scope):
    '`global`, `project:<Name>` or `ws:<Name>`; `workspace:` is accepted for `ws:`.'
    if scope == "global":
        return scope
    kind, _, name = scope.partition(":")
    kind = "ws" if kind == "workspace" else kind
    if kind not in ("project", "ws") or not name or "/" in name or name in (".", ".."):
        raise SystemExit(f"store-move-docs: unknown scope {scope!r}; use global, "
                         "project:<Name> or ws:<Name>")
    return f"{kind}:{name}"


def docs_dir(scope):
    if scope == "global":
        return os.path.join(C.STORE, "global", "docs")
    kind, name = scope.split(":", 1)
    return os.path.join(C.STORE, "projects" if kind == "project" else "workspaces",
                        name, "docs")


def pointer_arg(scope):
    'The second argument a get_doc pointer needs to reach this scope.'
    if scope == "global":
        return ""
    kind, name = scope.split(":", 1)
    return f', "{name}"' if kind == "project" else f', workspace="{name}"'


def bad_path(path):
    'Why `path` is no doc path, or None.'
    parts = path.split("/")
    if not path.endswith(".md"):
        return "not a .md path"
    if path.startswith("/") or "\\" in path or "\0" in path:
        return "not a relative doc path"
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return "path has an empty, dot or hidden segment"
    return None


def read_rows(text):
    '[(old, new)] from `old<TAB>new` lines; blank lines and # comments are skipped.'
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if len(cells) != 2 or not all(cells):
            raise SystemExit(f"store-move-docs: line {n} is not old_path<TAB>new_path")
        rows.append((cells[0], cells[1]))
    return rows


def plan_moves(scope, rows):
    "(moves, refused). A move is the dict store-compact's plan_rewrites and\n    write_moves take, with `old_path` and `new_path` set."
    base = docs_dir(scope)
    moves, refused, taken = [], [], set()
    for old, new in rows:
        ref = f"{scope}/{old}"

        def refuse(reason):
            refused.append({"ref": ref, "to": new, "reason": reason})

        why = bad_path(old) or bad_path(new)
        if why:
            refuse(why)
            continue
        if old == new:
            refuse("source and target are the same path")
            continue
        src, dst = os.path.join(base, *old.split("/")), os.path.join(base, *new.split("/"))
        if src in taken or dst in taken:
            refuse("an earlier row already uses this source or target")
            continue
        if not os.path.isfile(src):
            refuse("source is missing")
            continue
        meta, _ = C.frontmatter(src)
        if meta.get("type") != "doc":
            refuse("source is not a doc")
            continue
        with open(src, encoding="utf-8") as fh:
            text = C._rekey(fh.read(), scope, new)
        finish = False
        if os.path.exists(dst):
            
            
            with open(dst, encoding="utf-8") as fh:
                finish = fh.read() == text
            if not finish:
                refuse("target exists")
                continue
        taken.update((src, dst))
        moves.append({"ref": ref, "scope": scope, "fn": os.path.basename(old),
                      "src": src, "dst": dst, "text": text, "finish": finish,
                      "old_path": old, "new_path": new})
    return moves, refused


def run_moves(scope, rows, apply):
    moves, refused = plan_moves(scope, rows)
    rewrites = C.plan_rewrites(moves)
    if apply:
        C.write_moves(moves, rewrites)
    return {"applied": bool(apply),
            "moves": [{"from": m["old_path"], "to": m["new_path"]} for m in moves],
            "refused": refused, "rewrites": C._public(rewrites)}




_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^(```|~~~)")


def _headings(lines):
    '[(line index, level, title)] for every heading outside a code fence.'
    out, fence = [], None
    for i, line in enumerate(lines):
        f = _FENCE.match(line)
        if f:
            fence = None if fence == f.group(1) else (fence or f.group(1))
            continue
        if fence:
            continue
        m = _HEADING.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_key(front, key, value):
    'Frontmatter text with `key` set to the JSON of `value`, added when absent.'
    line = f"{key}: {json.dumps(value)}"
    end = front.find("\n---\n", 4)
    lines = front[4:end].split("\n")
    if any(ln.startswith(key + ":") for ln in lines):
        lines = [line if ln.startswith(key + ":") else ln for ln in lines]
    else:
        lines.append(line)
    return "---\n" + "\n".join(lines) + front[end:]


def _new_front(scope, path, title, source_front, now):
    "Frontmatter for a new archive doc: the source's `area` and `scope` lines kept."
    kept = [ln for ln in source_front[4:source_front.find("\n---\n", 4)].split("\n")
            if ln.startswith(("area:", "scope:"))]
    lines = [f"uuid: {json.dumps(C.stable_uuid('doc', scope, path))}", 'type: "doc"',
             f"path: {json.dumps(path)}", f"created_at: {json.dumps(now)}",
             f"title: {json.dumps(title)}", 'load_behavior: "lazy"',
             f"updated_at: {json.dumps(now)}", *kept]
    return "---\n" + "\n".join(lines) + "\n---\n"


def plan_split(scope, path, to, wanted, title=None):
    '{sections, refused, source, target} for moving the `wanted` headings of doc `path`\n    to doc `to`. `source` and `target` are (file, new text), or None when nothing moves.'
    base = docs_dir(scope)
    refused = []
    for p in (path, to):
        why = bad_path(p)
        if why:
            raise SystemExit(f"store-move-docs: {p}: {why}")
    if path == to:
        raise SystemExit("store-move-docs: --split and --to are the same doc")
    src, dst = os.path.join(base, *path.split("/")), os.path.join(base, *to.split("/"))
    meta, _ = C.frontmatter(src)
    if meta.get("type") != "doc":
        raise SystemExit(f"store-move-docs: {scope}/{path} is missing or is not a doc")
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    cut = text.find("\n---\n", 4) + 5
    front, lines = text[:cut], text[cut:].split("\n")
    heads = _headings(lines)

    spans = []
    for want in wanted:
        hits = [(n, h) for n, h in enumerate(heads) if h[2] == want]
        if len(hits) != 1:
            refused.append({"heading": want, "reason":
                            "no such heading" if not hits else
                            f"{len(hits)} headings carry this title"})
            continue
        n, (start, level, _title) = hits[0]
        end = next((i for i, lv, _t in heads[n + 1:] if lv <= level), len(lines))
        spans.append((start, end, level, want))
    spans.sort()
    kept = []
    for span in spans:
        if kept and span[0] < kept[-1][1]:
            refused.append({"heading": span[3],
                            "reason": f"inside the moved section {kept[-1][3]!r}"})
        else:
            kept.append(span)
    out = {"sections": [s[3] for s in kept], "refused": refused, "source": None,
           "target": None, "from": path, "to": to}
    if not kept:
        return out

    arg, now = pointer_arg(scope), _now()
    moved, rest, pos = [], [], 0
    for start, end, level, want in kept:
        rest += lines[pos:start]
        rest += [f'{"#" * level} {want}', "",
                 f'Moved to `get_doc("{to}"{arg})`.', ""]
        section = lines[start:end]
        while section and not section[-1].strip():
            section.pop()
        moved.append("\n".join(section))
        pos = end
    rest += lines[pos:]
    out["source"] = (src, _set_key(front, "updated_at", now) + "\n".join(rest))

    if os.path.exists(dst):
        tmeta, _ = C.frontmatter(dst)
        if tmeta.get("type") != "doc":
            raise SystemExit(f"store-move-docs: {scope}/{to} exists and is not a doc")
        with open(dst, encoding="utf-8") as fh:
            old = fh.read()
        tcut = old.find("\n---\n", 4) + 5
        new = (_set_key(old[:tcut], "updated_at", now) + old[tcut:].rstrip("\n")
               + "\n\n" + "\n\n".join(moved) + "\n")
    else:
        title = title or f"{meta.get('title') or path}: archived sections"
        new = (_new_front(scope, to, title, front, now)
               + f'\nSections moved out of `get_doc("{path}"{arg})`.\n\n'
               + "\n\n".join(moved) + "\n")
    out["target"] = (dst, new)
    return out


def run_split(scope, path, to, wanted, title, apply):
    plan = plan_split(scope, path, to, wanted, title)
    if apply and plan["source"]:
        
        
        for full, text in (plan["target"], plan["source"]):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".part"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, full)
    return {"applied": bool(apply and plan["source"]), "from": plan["from"],
            "to": plan["to"], "sections": plan["sections"], "refused": plan["refused"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Move docs inside one scope, or split "
                                 "headings of one doc into another, and repoint links.")
    ap.add_argument("--scope", required=True)
    ap.add_argument("--file", help="rows file on the daemon host; default is stdin")
    ap.add_argument("--split", metavar="DOC", help="doc whose headings move")
    ap.add_argument("--to", metavar="DOC", help="with --split: the doc that receives them")
    ap.add_argument("--heading", action="append", default=[], help="with --split; repeatable")
    ap.add_argument("--title", help="with --split: title for a new --to doc")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    scope = norm_scope(a.scope)

    if a.split:
        if not a.to or not a.heading:
            ap.error("--split needs --to and at least one --heading")
        res = run_split(scope, a.split, a.to, a.heading, a.title, a.apply)
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            verb = "moved" if res["applied"] else "would move"
            for s in res["sections"]:
                print(f"{verb} section {s!r}: {res['from']} -> {res['to']}")
            for r in res["refused"]:
                print(f"refused section {r['heading']!r}: {r['reason']}")
        return 1 if res["refused"] else 0

    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            rows = read_rows(fh.read())
    else:
        rows = read_rows("" if sys.stdin.isatty() else sys.stdin.read())
    res = run_moves(scope, rows, a.apply)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        verb, rverb = ("moved", "rewrote") if a.apply else ("would move", "would rewrite")
        for m in res["moves"]:
            print(f"{verb} {m['from']} -> {m['to']}")
        for r in res["rewrites"]:
            for c in r["changes"]:
                print(f"{rverb} {r['file']}: {c['from']} -> {c['to']}")
        for r in res["refused"]:
            print(f"refused {r['ref']} -> {r['to']}: {r['reason']}")
        print(f"{len(res['moves'])} move(s), {len(res['refused'])} refused, "
              f"{sum(len(r['changes']) for r in res['rewrites'])} link rewrite(s)"
              + ("" if a.apply else "; nothing written (pass --apply)"))
    return 1 if res["refused"] else 0


if __name__ == "__main__":
    if os.environ.get("AGENT_CONTEXT_SERVER_TASK") == "1":
        sys.exit(main())
    import store_task
    store_task.main_or_forward("store-move-docs", main, stdin=True)
