#!/usr/bin/env python3
"Split inventory.tsv into review batches and settle the mechanical rows.\n\n- data rows: checked here (parseable, no stray prose); marked reviewed by 'script'.\n- verify rows: installed copies compared to their store source here; a match is\n  marked reviewed, a mismatch or a file with no known source becomes a full read.\n- meta rows: digested (frontmatter, headings, test names) into batches/meta-NN.md.\n- strings rows: model-visible string literals digested into batches/strings-NN.md.\n- full rows: listed by absolute path in batches/full-NN.txt.\n\nData dir: $CONTEXT_INVENTORY_DIR, default ~/.local/state/agent-context/context-inventory.\nReads the store; writes only inventory.tsv and batches/ in the data dir."
import ast, csv, json, os, re, sys, tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp
import store_task  

CL = re.escape(hp.CLAUDE_DIRNAME)

DATA = Path(os.environ.get("CONTEXT_INVENTORY_DIR",
                           Path.home() / ".local/state/agent-context/context-inventory"))
INV = DATA / "inventory.tsv"
BATCHES = DATA / "batches"
STORE = Path(os.environ.get("AGENT_CONTEXT_STORE") or hp.store_root())
HOME = Path.home()
FULL_BUDGET = 380_000      


DIGEST_BUDGET = 100_000    
FIELDS = ["path", "bytes", "sha256", "class", "depth", "status", "reviewer", "verdict", "notes"]


def abspath(rel):
    return HOME / rel[2:] if rel.startswith("~/") else STORE / rel


def body(text):
    'Markdown body with YAML frontmatter removed, whitespace-normalized.'
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def source_for(rel):
    'Store file an installed copy is generated from, or None.'
    m = re.match(r"~/\.(claude/CLAUDE\.md|codex/AGENTS\.md|config/opencode/AGENTS\.md)$", rel)
    if m:
        return STORE / "AGENTS.md"
    for pat, repl in [
        (rf"^~/{CL}/agents/(.+)$", r"global/agents/\1"),
        (r"^~/\.pi/agent/agents/(.+)$", r"global/agents/\1"),
        (r"^~/\.config/opencode/agent/(.+)$", r"global/agents/\1"),
        (rf"^~/{CL}/commands/(.+)$", r"global/commands/\1"),
        (r"^~/\.config/opencode/commands/(.+)$", r"global/commands/\1"),
        (rf"^~/{CL}/skills/(?!synced/)(.+)$", r"global/skills/\1"),
        (r"^shared-docs/(.+)$", r"global/docs/\1"),
        (r"^shared-skills/(.+)$", r"global/skills/\1"),
    ]:
        if re.match(pat, rel):
            cand = STORE / re.sub(pat, repl, rel)
            return cand if cand.is_file() else None
    return None


def check_data(p):
    "Return '' when the file is plain data, else a note."
    text = p.read_text(errors="replace")
    suf = p.suffix
    try:
        if suf == ".json":
            json.loads(text)
        elif suf == ".jsonl":
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        elif suf == ".toml":
            tomllib.loads(text)
        elif suf == ".md":
            status = re.search(r'^status:\s*"?(\w+)', text, re.M)
            if status and status.group(1) not in ("resolved", "discarded", "superseded"):
                return f"archived observation has status {status.group(1)}"
        elif suf in (".css", ".lock", ".txt", ".csv", ".log", ""):
            pass
        else:
            return f"unexpected type {suf or 'none'}"
    except Exception as e:  
        return f"parse failure: {type(e).__name__}"
    return ""


def meta_digest(p):
    text = p.read_text(errors="replace")
    lines = []
    if p.suffix == ".md":
        if text.startswith("---\n"):
            end = text.find("\n---", 4)
            if end != -1:
                lines += [l for l in text[4:end].splitlines()
                          if not l.startswith(("uuid:", "created_at:", "updated_at:"))]
        lines += [l for l in body(text).splitlines() if l.startswith("#")]
        first = next((l for l in body(text).splitlines() if l.strip() and not l.startswith("#")), "")
        lines.append("first line: " + first[:200])
    elif p.suffix == ".py":
        try:
            tree = ast.parse(text)
            doc = ast.get_docstring(tree) or ""
            lines.append("docstring: " + doc[:400].replace("\n", " "))
            lines += ["def " + n.name for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        except SyntaxError:
            lines.append("(syntax error)")
    else:
        lines += text.splitlines()[:40]
    return "\n".join(lines)


def strings_digest(p):
    text = p.read_text(errors="replace")
    if p.suffix != ".py":
        keep = [l.strip() for l in text.splitlines()
                if re.search(r"\b(echo|printf|print|console\.(log|error)|>&2|description)\b", l)]
        return "\n".join(keep[:200])
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "(syntax error)"
    out, seen = [], set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value.strip()) >= 40:
            s = n.value.strip()
            if s not in seen:
                seen.add(s)
                out.append(f"L{n.lineno}: {s[:1500]}")
        elif isinstance(n, ast.JoinedStr) and len(ast.unparse(n)) >= 50:
            s = ast.unparse(n)
            if s not in seen:
                seen.add(s)
                out.append(f"L{n.lineno}: {s[:1500]}")
    return "\n".join(out)


def pack(items, budget):
    'Greedy, order-preserving packing of (key, size, payload) into batches.'
    batches, cur, size = [], [], 0
    for item in items:
        if cur and size + item[1] > budget:
            batches.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += item[1]
    if cur:
        batches.append(cur)
    return batches


def main():
    with open(INV, newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    BATCHES.mkdir(parents=True, exist_ok=True)
    for old in BATCHES.glob("*"):
        old.unlink()

    full, meta, strings = [], [], []
    for r in rows:
        if r["status"] != "unread":
            continue
        p = abspath(r["path"])
        if r["depth"] == "data":
            note = check_data(p)
            r.update(status="reviewed", reviewer="script",
                     verdict="flag" if note else "ok", notes=note)
        elif r["depth"] == "verify":
            src = source_for(r["path"])
            same = src is not None and (body(src.read_text(errors="replace"))
                                        == body(p.read_text(errors="replace")))
            if src is not None and same:
                r.update(status="reviewed", reviewer="script", verdict="ok",
                         notes=f"body matches {src.relative_to(STORE)}")
            else:
                r["notes"] = (f"differs from {src.relative_to(STORE)}" if src else "no store source")
                full.append(r)
        elif r["depth"] == "meta":
            meta.append(r)
        elif r["depth"] == "strings":
            strings.append(r)
        else:
            full.append(r)

    manifest = []
    full.sort(key=lambda r: (r["class"], r["path"]))
    for i, b in enumerate(pack([(r["path"], int(r["bytes"]), r) for r in full], FULL_BUDGET), 1):
        name = f"full-{i:02d}"
        lines = [f"{abspath(r['path'])}\t{r['path']}\t{r['class']}\t{r['bytes']}"
                 + (f"\t{r['notes']}" if r["notes"] else "") for _, _, r in b]
        (BATCHES / f"{name}.txt").write_text("\n".join(lines) + "\n")
        manifest.append((name, len(b), sum(s for _, s, _ in b)))

    for kind, group, fn in (("meta", meta, meta_digest), ("strings", strings, strings_digest)):
        group.sort(key=lambda r: (r["class"], r["path"]))
        items = []
        for r in group:
            d = f"=== {r['path']}  [{r['class']}, {r['bytes']}B]\n{fn(abspath(r['path']))}\n"
            items.append((r["path"], len(d), d))
        for i, b in enumerate(pack(items, DIGEST_BUDGET), 1):
            name = f"{kind}-{i:02d}"
            (BATCHES / f"{name}.md").write_text("".join(d for _, _, d in b))
            manifest.append((name, len(b), sum(s for _, s, _ in b)))

    with open(BATCHES / "manifest.tsv", "w") as f:
        f.write("batch\tfiles\tbytes\n")
        for m in manifest:
            f.write("\t".join(map(str, m)) + "\n")
    with open(INV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    settled = sum(r["reviewer"] == "script" for r in rows)
    flagged = [r for r in rows if r["verdict"] == "flag"]
    print(f"settled by script: {settled}; flagged: {len(flagged)}")
    for r in flagged[:20]:
        print("  FLAG", r["path"], r["notes"])
    for m in manifest:
        print(f"{m[0]:12s} {m[1]:5d} files {m[2]:>9d}B")


if __name__ == "__main__":
    store_task.main_or_forward("context-inventory-batch", main, store=str(STORE))
