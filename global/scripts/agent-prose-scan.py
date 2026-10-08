#!/usr/bin/env python3
"Count blocked plain-language hits in recent Claude Code transcripts, by writer.\n\nReads assistant text blocks (no thinking, no tool calls) from transcripts modified in\nthe last N days and matches them against plain-language-words.py, the lists the gates\nblock on. Splits the count three ways: main (the session user reads), subagent (a\nworker's own transcript, where narration between tool calls lives and no hook sees\nit) and eval (eval-run sandboxes). Read-only.\n\nUsage: agent-prose-scan.py [--days N] [--root DIR] [--json]\n  --days N    transcripts modified in the last N days (default 3)\n  --root DIR  transcript root (default ~/.claude/projects)\n  --json      one JSON object: {kind: {messages, hits, patterns}}"
import glob
import importlib.util
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

FENCE = re.compile(r"```.*?```", re.S)
SPAN = re.compile(r"`[^`\n]*`")
KINDS = ("main", "subagent", "eval")
OPTIONS_WITH_VALUE = ("--days", "--root")
FLAGS = ("--json", "-h", "--help")


def words():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plain-language-words.py")
    if not os.path.exists(path):
        path = os.path.expanduser("~/.agent-context/global/scripts/plain-language-words.py")
    spec = importlib.util.spec_from_file_location("pl_words_scan", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def kind_of(rel):
    "main, subagent or eval, from the transcript's path under the root."
    if "/subagents/" in "/" + rel:
        return "subagent"
    top = rel.split(os.sep, 1)[0]
    if "-eval-" in top or "cache-tmp-eval" in top:
        return "eval"
    return "main"


def texts(path):
    'Distinct assistant text blocks in one transcript.'
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    seen = set()
    with fh:
        for line in fh:
            if '"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if text.strip() and text not in seen:
                        seen.add(text)
                        yield text


def scan(root, days):
    pl = words()
    patterns = [re.compile(p, re.I) for p in [pl.alt(pl.HARD)] + list(pl.HARD_PHRASE)]
    cut = time.time() - days * 86400
    out = {k: {"messages": 0, "hits": 0, "patterns": {}} for k in KINDS}
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cut:
                continue
        except OSError:
            continue
        bucket = out[kind_of(os.path.relpath(path, root))]
        for text in texts(path):
            bucket["messages"] += 1
            clean = SPAN.sub(" ", FENCE.sub(" ", text))
            found = [m.group(0).strip().lower() for rx in patterns for m in rx.finditer(clean)]
            found += ["em dash"] * clean.count(pl.EM_DASH)
            for key in found:
                bucket["hits"] += 1
                bucket["patterns"][key] = bucket["patterns"].get(key, 0) + 1
    return out


def main(argv):
    args = argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0
    values = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in OPTIONS_WITH_VALUE:
            if i + 1 >= len(args):
                print("%s needs a value" % a, file=sys.stderr)
                return 2
            values[a] = args[i + 1]
            i += 2
            continue
        if a not in FLAGS:
            print("unknown argument: %s" % a, file=sys.stderr)
            return 2
        i += 1
    try:
        days = float(values.get("--days", "3"))
    except ValueError:
        print("--days needs a number", file=sys.stderr)
        return 2
    root = os.path.expanduser(values.get("--root", hp.projects_dir()))
    if not os.path.isdir(root):
        print("no transcript root at %s" % root, file=sys.stderr)
        return 1
    out = scan(root, days)
    if "--json" in args:
        print(json.dumps(out, indent=1))
        return 0
    print("%-9s %9s %7s %8s" % ("writer", "messages", "hits", "per 100"))
    for k in KINDS:
        b = out[k]
        rate = 100.0 * b["hits"] / b["messages"] if b["messages"] else 0.0
        print("%-9s %9d %7d %8.1f" % (k, b["messages"], b["hits"], rate))
    for k in KINDS:
        top = sorted(out[k]["patterns"].items(), key=lambda kv: -kv[1])[:10]
        if top:
            print("\n%s top hits: %s" % (k, ", ".join("%s %d" % kv for kv in top)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
