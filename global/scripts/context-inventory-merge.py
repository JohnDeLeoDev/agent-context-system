#!/usr/bin/env python3
'Merge reviewer results (results/<batch>.tsv: path, verdict, notes) into\ninventory.tsv and report coverage: rows still unread, per batch.\n\nData dir: $CONTEXT_INVENTORY_DIR, default ~/.local/state/agent-context/context-inventory.\nReads results/ and batches/; writes only inventory.tsv in the data dir.'
import csv, os, re
from collections import Counter
from pathlib import Path

DATA = Path(os.environ.get("CONTEXT_INVENTORY_DIR",
                           Path.home() / ".local/state/agent-context/context-inventory"))
INV = DATA / "inventory.tsv"
RESULTS = DATA / "results"
BATCHES = DATA / "batches"
FIELDS = ["path", "bytes", "sha256", "class", "depth", "status", "reviewer", "verdict", "notes"]
VERDICT = re.compile(r"^(keep|trim|rewrite|split|merge:.+|move:.+|archive|delete|ok|flag)$")



HEADER = re.compile(r"^=== (.+?)  \[[^\]\n]*, \d+B\]$", re.M)


def batch_paths(batch):
    f = BATCHES / f"{batch}.txt"
    if f.exists():
        return [l.split("\t")[1] for l in f.read_text().splitlines() if l]
    f = BATCHES / f"{batch}.md"
    return HEADER.findall(f.read_text()) if f.exists() else []


def result_files():
    '(result files of this run, result files of an earlier run), each sorted by name.'
    files = sorted(RESULTS.glob("*.tsv"))
    manifest = BATCHES / "manifest.tsv"
    if not manifest.exists():
        return files, []
    cut = manifest.stat().st_mtime
    return ([f for f in files if f.stat().st_mtime >= cut],
            [f for f in files if f.stat().st_mtime < cut])


def main():
    with open(INV, newline="") as f:
        rows = {r["path"]: r for r in csv.DictReader(f, delimiter="\t")}
    bad = []
    current, earlier = result_files()
    for res in current:
        batch = res.stem
        for line in res.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[0] in ("path", ""):
                continue
            path, verdict = parts[0].strip(), parts[1].strip()
            notes = parts[2].strip() if len(parts) > 2 else ""
            if path not in rows or not VERDICT.match(verdict):
                bad.append(f"{batch}: {line[:120]}")
                continue
            rows[path].update(status="reviewed", reviewer=batch, verdict=verdict, notes=notes)
    with open(INV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: r["path"]))

    for b in sorted(p.stem for p in BATCHES.glob("*-*.*")):
        missing = [p for p in batch_paths(b) if rows.get(p, {}).get("status") == "unread"]
        if missing:
            print(f"{b}: {len(missing)} unread")
    unread = sum(r["status"] == "unread" for r in rows.values())
    print(f"unread {unread} of {len(rows)}; bad result lines {len(bad)}")
    if earlier:
        print(f"ignored {len(earlier)} result file(s) older than batches/manifest.tsv: "
              + ", ".join(f.name for f in earlier))
    for l in bad[:20]:
        print("  BAD", l)
    print(Counter(r["verdict"] for r in rows.values() if r["status"] == "reviewed").most_common())


if __name__ == "__main__":
    main()
