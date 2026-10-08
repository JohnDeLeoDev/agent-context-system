#!/usr/bin/env python3
'Search benchmark for the agent-context store (context graph T1, T6).\n\ncollect --out FILE\n    Scan Claude Code transcripts for an agent-context search call (search_all,\n    search_memories, search_docs) followed, within the next 3 agent-context calls\n    of the same transcript, by get_memory, get_doc or get_entity. The fetched key\n    is the label, whether or not the search returned it. A fetch that failed is\n    not a label. A later search ends the window, so a fetch belongs to the nearest\n    search before it. Rows are deduped by (query, project) with their labels\n    merged. Queries shaped like a secret value are dropped.\n    Row: {"query", "tool", "project", "expected": [{"kind", "key"}]}\n\nscore --server-src PATH [--pairs FILE] [--root DIR] [--param MODULE.NAME=VALUE ...] [--dump FILE]\n    Import agent_context from PATH, build ContextStore on the store tree, and run\n    each row through the tool function its tool names (integrity.search_all,\n    memory.search_memories, docs.search_docs, the latter two with the row\'s\n    project), so ranking done in the tool layer is scored too. Those functions\n    count a search hit per returned row, so the run replaces usage.record and\n    usage.record_many with no-ops before the first query: scoring writes no\n    counters. --param sets a module constant first. ContextStore.__init__ and\n    reload only read files: no writes, no git.\n    Prints recall@1, recall@5, MRR and pair count per set, overall, per tool and\n    per half, scored on the main rows (search_all rows marked "group": "other"\n    are left out). Then, for search_all rows with labels outside memory and doc,\n    which of those labels the other group returned; and the bytes that group adds\n    to a default search_all call (limit 20), mean and p95.\n    --dump writes each row\'s label ranks, other-group labels and a sha256 of its\n    main rows at limit 20, for `compare`.\n\ntune --server-src PATH --grid MODULE.NAME=V1,V2,... [--grid ...] [--pairs FILE] [--root DIR]\n    Score every combination of the grid values on the TUNE half only, searchable\n    labels, and print the combinations best first by recall@5, then MRR. The\n    holdout half is for judging, never for picking weights.\n\ncompare BEFORE AFTER [--real FILE]\n    Read two --dump files taken on the same store tree. First, rows whose main\n    rows at limit 20 differ in bytes. Then rows improved, worsened and unchanged:\n    a row improved when its recall@5 rose, or held while its reciprocal rank\n    rose. Sets: searchable holdout, searchable (all halves), and the\n    hand-verified real pairs from --real (default context-graph/search-ceiling.json,\n    entries with "real": true), where a pair improved when its label\'s rank moved up.\n\nSets. "all" scores every label. "searchable" scores only the labels T1\'s search\ncould return: memory and doc for search_all, memory for search_memories, doc for\nsearch_docs. That definition is frozen at the T1 baseline, so before and after\ncompare the same 117 rows. "dropped" scores the labels outside it, and\n"unsearchable" the rows with no searchable label.\n\nOwn docs. The benchmark\'s docs under context-graph/ quote its queries, so they are\ndropped from every ranking. No label names one.\n\nHalves. A row is "tune" when the first byte of sha256(query) is even, else\n"holdout". Stable across runs and machines.\n\nrecall@k is the share of a row\'s labels in its top k, averaged over rows. MRR uses\nthe first label in the full ranking.'
import argparse
import glob
import hashlib
import importlib
import itertools
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

HOME = os.path.expanduser("~")
PREFIX = "mcp__agent-context__"
SEARCH = ("search_all", "search_memories", "search_docs")
FETCH = ("get_memory", "get_doc", "get_entity")
WINDOW = 3

TYPES = {"search_all": ("memory", "doc"), "search_memories": ("memory",), "search_docs": ("doc",)}
SETS = ("all", "searchable", "dropped", "unsearchable")
HALVES = ("tune", "holdout")
UNLIMITED = 10 ** 9
DEFAULT_LIMIT = 20
OWN_DOCS = "context-graph/"
DOCS = os.path.join(HOME, ".agent-context", "global", "docs", "context-graph")
DEFAULT_PAIRS = os.path.join(DOCS, "search-benchmark.json")
DEFAULT_REAL = os.path.join(DOCS, "search-ceiling.json")


_SECRET = re.compile(
    r"sk-[A-Za-z0-9]|gh[pousr]_[A-Za-z0-9]|github_pat_|xox[abpr]-|AKIA[0-9A-Z]{8}"
    r"|eyJ[A-Za-z0-9_-]{10}|op://|-----BEGIN"
    r"|(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S", re.I)


def looks_secret(query):
    if _SECRET.search(query):
        return True
    for run in re.findall(r"[A-Za-z0-9+/=_]{24,}", query):
        if re.search(r"\d", run) and re.search(r"[A-Za-z]", run):
            return True
    return False


def result_text(block):
    if not block:
        return ""
    content = block.get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def payload(text):
    'Decode a store tool result, {"result": "<json>"}. None when it does not decode.'
    try:
        outer = json.loads(text)
        inner = outer.get("result") if isinstance(outer, dict) else None
        return json.loads(inner) if isinstance(inner, str) else inner
    except (TypeError, ValueError):
        return None


def fetched_ok(block):
    text = result_text(block)
    
    if text.startswith("Error: result ("):
        return True
    if not block or block.get("is_error"):
        return False
    body = payload(text)
    return body is not None and not (isinstance(body, dict) and "error" in body)


def returned_names(block):
    rows = payload(result_text(block))
    if not isinstance(rows, list):
        return set()
    return {r.get("slug") or r.get("path") or r.get("name") for r in rows if isinstance(r, dict)}


def label(name, inp):
    if name == "get_memory":
        return "memory", inp.get("slug")
    if name == "get_doc":
        return "doc", inp.get("path")
    return inp.get("kind"), inp.get("key")


def store_calls(path):
    'Agent-context calls in transcript order, each with its tool_result block.'
    calls, by_id = [], {}
    with open(path, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and str(b.get("name", "")).startswith(PREFIX):
                    call = {"name": b["name"][len(PREFIX):],
                            "input": b.get("input") if isinstance(b.get("input"), dict) else {},
                            "result": None}
                    by_id[b.get("id")] = call
                    calls.append(call)
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    by_id[b["tool_use_id"]]["result"] = b
    return calls


def collect(args):
    files = sorted(glob.glob(os.path.join(args.transcripts, "**", "*.jsonl"), recursive=True))
    rows, order = {}, []
    stats = {"transcripts": len(files), "search_calls": 0, "raw_pairs": 0, "secret_dropped": 0,
             "raw_pairs_from_eval_dirs": 0, "labels_missed_in_session": 0}
    for path in files:
        calls = store_calls(path)
        for i, call in enumerate(calls):
            if call["name"] not in SEARCH:
                continue
            stats["search_calls"] += 1
            query = call["input"].get("query")
            if not isinstance(query, str) or not query.strip():
                continue
            expected = []
            for nxt in calls[i + 1:i + 1 + WINDOW]:
                if nxt["name"] in SEARCH:
                    break
                if nxt["name"] in FETCH and fetched_ok(nxt["result"]):
                    kind, key = label(nxt["name"], nxt["input"])
                    if isinstance(kind, str) and isinstance(key, str) and key:
                        expected.append((kind, key))
            if not expected:
                continue
            if looks_secret(query):
                stats["secret_dropped"] += 1
                continue
            stats["raw_pairs"] += 1
            if "-eval-" in path:
                stats["raw_pairs_from_eval_dirs"] += 1
            returned = returned_names(call["result"])
            project = None if call["name"] == "search_all" else call["input"].get("project")
            k = (query.strip(), project)
            if k not in rows:
                rows[k] = {"query": query.strip(), "tool": call["name"], "project": project,
                           "expected": [], "_missed": set()}
                order.append(k)
            row = rows[k]
            for kind, key in expected:
                item = {"kind": kind, "key": key}
                if item not in row["expected"]:
                    row["expected"].append(item)
                if key not in returned:
                    row["_missed"].add((kind, key))
    out = []
    for k in order:
        row = rows[k]
        stats["labels_missed_in_session"] += len(row.pop("_missed"))
        out.append(row)
    stats["pairs"] = len(out)
    stats["labels"] = sum(len(r["expected"]) for r in out)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(stats, indent=1))


def half(query):
    return "tune" if hashlib.sha256(query.encode()).digest()[0] % 2 == 0 else "holdout"


def open_store(args):
    sys.path.insert(0, os.path.abspath(args.server_src))
    from agent_context import usage
    from agent_context.store import ContextStore

    
    
    usage.record = lambda *a, **k: None
    usage.record_many = lambda *a, **k: None
    return ContextStore(root=args.root)


def set_param(spec):
    name, _, value = spec.partition("=")
    mod, _, const = name.rpartition(".")
    if not mod or not const or not value:
        sys.exit(f"expected MODULE.NAME=VALUE, got {spec!r}")
    module = importlib.import_module("agent_context." + mod)
    if not hasattr(module, const):
        sys.exit(f"agent_context.{mod} has no {const}")
    setattr(module, const, type(getattr(module, const))(value))


def dumps(obj):
    "The server's _dumps: compact separators."
    return json.dumps(obj, default=str, separators=(",", ":"))


def run_tool(store, row, limit):
    from agent_context import docs, integrity, memory

    query = row["query"]
    if row["tool"] == "search_all":
        return integrity.search_all(store, query, limit=limit)
    if row["tool"] == "search_memories":
        return memory.search_memories(store, query, row.get("project"), limit)
    return docs.search_docs(store, query, row.get("project"), limit)


def key_of(tool, hit):
    if tool == "search_memories":
        return "memory", hit["slug"]
    if tool == "search_docs":
        return "doc", hit["path"]
    return hit["entity_type"], hit["name"]


def is_other(hit):
    return hit.get("group") == "other"


def ranking(store, row):
    '(main ranking, other-group keys) for one row, own docs dropped from the ranking.'
    hits = run_tool(store, row, UNLIMITED)
    main = [key_of(row["tool"], h) for h in hits if not is_other(h)]
    other = [key_of(row["tool"], h) for h in hits if is_other(h)]
    return [k for k in main if not (k[0] == "doc" and k[1].startswith(OWN_DOCS))], other


def label_sets(row):
    want_all = {(e["kind"], e["key"]) for e in row["expected"]}
    want_reach = {w for w in want_all if w[0] in TYPES[row["tool"]]}
    return {"all": want_all, "searchable": want_reach, "dropped": want_all - want_reach,
            "unsearchable": set() if want_reach else want_all}


def measure(rows, rankings):
    '{(set, group): [pairs, recall@1 sum, recall@5 sum, reciprocal rank sum]}.\n    group is "" for every row, else the row\'s tool or half.'
    totals = {}
    for row, ranked in zip(rows, rankings):
        for name, want in label_sets(row).items():
            if not want:
                continue
            first = next((n for n, h in enumerate(ranked, 1) if h in want), None)
            got = (len(want & set(ranked[:1])) / len(want),
                   len(want & set(ranked[:5])) / len(want),
                   1.0 / first if first else 0.0)
            for group in ("", row["tool"], half(row["query"])):
                t = totals.setdefault((name, group), [0, 0.0, 0.0, 0.0])
                t[0] += 1
                for j in range(3):
                    t[j + 1] += got[j]
    return totals


def load_json(path):
    with open(path) as f:
        return json.load(f)


def score(args):
    store = open_store(args)
    for spec in args.param:
        set_param(spec)
    rows = load_json(args.pairs)
    ranked = [ranking(store, row) for row in rows]
    totals = measure(rows, [main for main, _ in ranked])
    print(f"{'set':<32} {'pairs':>5} {'recall@1':>9} {'recall@5':>9} {'MRR':>6}")
    for name in SETS:
        for group in ("",) + SEARCH + HALVES:
            n, r1, r5, rr = totals.get((name, group), [0, 0, 0, 0])
            if n:
                title = f"{name}, {group}" if group else name
                print(f"{title:<32} {n:>5} {r1 / n:>9.3f} {r5 / n:>9.3f} {rr / n:>6.3f}")

    print("\nsearch_all rows with labels outside memory and doc: found in the other group")
    rows_found = labels_found = labels_total = 0
    for row, (_, other) in zip(rows, ranked):
        sets = label_sets(row)
        if row["tool"] != "search_all" or not sets["dropped"]:
            continue
        want = sorted(sets["dropped"])
        found = [w for w in want if w in other]
        rows_found += bool(found)
        labels_found += len(found)
        labels_total += len(want)
        tag = "unsearchable" if not sets["searchable"] else "dropped"
        print(f"  {tag:<12} {len(found)}/{len(want)}  {row['query'][:56]!r}  "
              f"found: {', '.join(':'.join(w) for w in found) or '-'}  "
              f"group: {', '.join(':'.join(k) for k in other) or '-'}")
    print(f"  rows with a label found {rows_found}; labels found {labels_found} of {labels_total}")

    added, main_sha = [], []
    for row in rows:
        out = run_tool(store, row, DEFAULT_LIMIT)
        main_rows = [h for h in out if not is_other(h)]
        main_sha.append(hashlib.sha256(dumps(main_rows).encode()).hexdigest())
        if row["tool"] == "search_all":
            added.append(len(dumps(out).encode()) - len(dumps(main_rows).encode()))
    if added:
        added.sort()
        p95 = added[max(0, math.ceil(0.95 * len(added)) - 1)]
        print(f"\nbytes the other group adds to search_all (limit {DEFAULT_LIMIT}, "
              f"{len(added)} queries): mean {sum(added) / len(added):.0f}, p95 {p95}, "
              f"max {added[-1]}, zero on {added.count(0)}")

    if args.dump:
        out = []
        for row, (main, other), sha in zip(rows, ranked, main_sha):
            pos = {}
            for n, h in enumerate(main, 1):
                pos.setdefault(h, n)
            out.append({"query": row["query"], "tool": row["tool"], "project": row.get("project"),
                        "main_sha": sha, "other": [":".join(k) for k in other],
                        "ranks": {f"{e['kind']}:{e['key']}": pos.get((e["kind"], e["key"]))
                                  for e in row["expected"]}})
        with open(args.dump, "w") as f:
            json.dump(out, f, indent=1)
            f.write("\n")


def tune(args):
    store = open_store(args)
    rows = [r for r in load_json(args.pairs) if half(r["query"]) == "tune"]
    axes = []
    for spec in args.grid:
        name, _, values = spec.partition("=")
        axes.append([f"{name}={v}" for v in values.split(",") if v])
    results = []
    for combo in itertools.product(*axes):
        for spec in combo:
            set_param(spec)
        rankings = [ranking(store, row)[0] for row in rows]
        n, r1, r5, rr = measure(rows, rankings).get(("searchable", ""), [0, 0, 0, 0])
        if n:
            results.append((r5 / n, rr / n, r1 / n, n, combo))
    results.sort(key=lambda x: (-x[0], -x[1]))
    print(f"{'recall@5':>9} {'MRR':>6} {'recall@1':>9} {'pairs':>5}  params")
    for r5, rr, r1, n, combo in results:
        print(f"{r5:>9.3f} {rr:>6.3f} {r1:>9.3f} {n:>5}  {' '.join(combo)}")


def _row_score(ranks, labels):
    got = [ranks[k] for k in labels]
    r5 = sum(1 for r in got if r and r <= 5) / len(labels)
    best = min((r for r in got if r), default=None)
    return r5, (1.0 / best if best else 0.0)


def compare(args):
    before = {(r["query"], r["project"], r["tool"]): r for r in load_json(args.before)}
    after = {(r["query"], r["project"], r["tool"]): r for r in load_json(args.after)}
    real = {(p["query"], p["label"]) for p in load_json(args.real) if p.get("real")}
    changed = [k for k, b in before.items() if b.get("main_sha") != after[k].get("main_sha")]
    print(f"rows whose main rows at limit {DEFAULT_LIMIT} changed: {len(changed)} of {len(before)}")
    for query, _, tool in changed:
        print(f"  {tool}: {query!r}")
    counts = {}

    def tally(name, old, new, r5=None):
        c = counts.setdefault(name, [0, 0, 0, 0.0, 0.0])
        c[0 if new > old else 1 if new < old else 2] += 1
        if r5:
            c[3] += r5[0]
            c[4] += r5[1]

    for key, b in before.items():
        a = after[key]
        labels = list(b["ranks"])
        reach = [k for k in labels if k.split(":", 1)[0] in TYPES[b["tool"]]]
        if reach:
            old, new = _row_score(b["ranks"], reach), _row_score(a["ranks"], reach)
            tally("searchable", old, new, (old[0], new[0]))
            if half(b["query"]) == "holdout":
                tally("searchable, holdout", old, new, (old[0], new[0]))
        for k in labels:
            if (b["query"], k) in real:
                
                tally("real pairs (rank)", -(b["ranks"][k] or UNLIMITED),
                      -(a["ranks"][k] or UNLIMITED))
    print(f"{'set':<22} {'n':>4} {'improved':>9} {'worsened':>9} {'unchanged':>10} "
          f"{'r@5 before':>11} {'r@5 after':>10}")
    for name in ("searchable, holdout", "searchable", "real pairs (rank)"):
        if name not in counts:
            continue
        up, down, same, rb, ra = counts[name]
        n = up + down + same
        r5 = (f"{rb / n:>11.3f} {ra / n:>10.3f}" if not name.startswith("real")
              else f"{'':>11} {'':>10}")
        print(f"{name:<22} {n:>4} {up:>9} {down:>9} {same:>10} {r5}")


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--transcripts", default=hp.projects_dir(HOME))
    c.add_argument("--out", required=True)
    for mode in ("score", "tune"):
        s = sub.add_parser(mode)
        s.add_argument("--server-src", required=True)
        s.add_argument("--pairs", default=DEFAULT_PAIRS)
        s.add_argument("--root", default=os.path.join(HOME, ".agent-context"))
        if mode == "score":
            s.add_argument("--param", action="append", default=[])
            s.add_argument("--dump")
        else:
            s.add_argument("--grid", action="append", required=True)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("before")
    cmp_.add_argument("after")
    cmp_.add_argument("--real", default=DEFAULT_REAL)
    return ap.parse_args(argv)


def dispatch(args):
    {"collect": collect, "score": score, "tune": tune, "compare": compare}[args.mode](args)


def main():
    dispatch(parse_args(sys.argv[1:]))


if __name__ == "__main__":
    
    
    
    
    
    _args = parse_args(sys.argv[1:])
    if _args.mode in ("score", "tune"):
        store_task.main_or_forward("context-graph-search-bench", lambda: dispatch(_args),
                                   store=_args.root)
    else:
        dispatch(_args)
