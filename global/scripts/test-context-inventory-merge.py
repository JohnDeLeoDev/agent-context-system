#!/usr/bin/env python3
'Run: python3 test-context-inventory-merge.py'
import csv
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MERGE = os.path.join(HERE, "context-inventory-merge.py")
BATCH = os.path.join(HERE, "context-inventory-batch.py")
TMP_BASE = os.path.expanduser("~/.cache/tmp")
FIELDS = ["path", "bytes", "sha256", "class", "depth", "status", "reviewer", "verdict", "notes"]

SPACED = "projects/example-helper/docs/example-helper.md"
PLAIN = "global/docs/plain.md"
FULL = "global/hooks/some-hook.py"

passed = 0
failures = []


def check(label, ok, detail: object = ""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + str(detail) if detail else ""))


def header(path, cls="proj-doc", size=893):
    'The digest header, spelled as context-inventory-batch.py writes it.'
    return "=== %s  [%s, %dB]\n" % (path, cls, size)


def data_dir(root, name, manifest=True):
    data = os.path.join(root, name)
    os.makedirs(os.path.join(data, "batches"))
    os.makedirs(os.path.join(data, "results"))
    with open(os.path.join(data, "inventory.tsv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        for path, depth in ((SPACED, "meta"), (PLAIN, "meta"), (FULL, "full")):
            w.writerow({"path": path, "bytes": "893", "sha256": "0" * 64, "class": "proj-doc",
                        "depth": depth, "status": "unread", "reviewer": "", "verdict": "", "notes": ""})
    with open(os.path.join(data, "batches", "meta-01.md"), "w") as fh:
        fh.write(header(SPACED) + "title: x\n=== not a header, a line of the digest\n"
                 + "=== %s\n" % FULL + header(PLAIN) + "title: y\n")
    with open(os.path.join(data, "batches", "full-01.txt"), "w") as fh:
        fh.write("/abs/%s\t%s\thook\t893\n" % (FULL, FULL))
    if manifest:
        with open(os.path.join(data, "batches", "manifest.tsv"), "w") as fh:
            fh.write("batch\tfiles\tbytes\nfull-01\t1\t893\nmeta-01\t2\t1786\n")
    return data


def result(data, batch, lines, age=0):
    'Write results/<batch>.tsv; age > 0 dates it that many seconds before the manifest.'
    path = os.path.join(data, "results", batch + ".tsv")
    with open(path, "w") as fh:
        fh.write("".join("%s\t%s\t%s\n" % line for line in lines))
    manifest = os.path.join(data, "batches", "manifest.tsv")
    if age and os.path.exists(manifest):
        then = os.path.getmtime(manifest) - age
        os.utime(path, (then, then))
    return path


def merge(data):
    proc = subprocess.run([sys.executable, MERGE], capture_output=True, text=True,
                          env=dict(os.environ, CONTEXT_INVENTORY_DIR=data))
    with open(os.path.join(data, "inventory.tsv"), newline="") as fh:
        rows = {r["path"]: r for r in csv.DictReader(fh, delimiter="\t")}
    return proc, rows


def load_merge(data):
    old = os.environ.get("CONTEXT_INVENTORY_DIR")
    os.environ["CONTEXT_INVENTORY_DIR"] = data
    try:
        spec = importlib.util.spec_from_file_location("context_inventory_merge", MERGE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            del os.environ["CONTEXT_INVENTORY_DIR"]
        else:
            os.environ["CONTEXT_INVENTORY_DIR"] = old


def cases(root):
    print("H1 a header whose path holds spaces names the whole path")
    data = data_dir(root, "h1")
    mod = load_merge(data)
    check("both headers, whole paths", mod.batch_paths("meta-01") == [SPACED, PLAIN], mod.batch_paths("meta-01"))
    check("a full batch lists its paths", mod.batch_paths("full-01") == [FULL], mod.batch_paths("full-01"))
    proc, rows = merge(data)
    check("exit 0", proc.returncode == 0, proc.stderr)
    check("the batch reports both rows unread", "meta-01: 2 unread" in proc.stdout, proc.stdout)

    print("H2 the batch step writes the header the merge step parses")
    with open(BATCH, encoding="utf-8") as fh:
        writer = fh.read()
    check("writer format unchanged",
          "f\"=== {r['path']}  [{r['class']}, {r['bytes']}B]\\n" in writer)

    print("H3 a result for the spaced path is merged")
    data = data_dir(root, "h3")
    result(data, "meta-01", [(SPACED, "trim", "rubric 2"), (PLAIN, "ok", "")])
    proc, rows = merge(data)
    check("spaced row reviewed", rows[SPACED]["status"] == "reviewed" and rows[SPACED]["verdict"] == "trim", rows[SPACED])
    check("no meta-01 unread line", not re.search(r"^meta-01: ", proc.stdout, re.M), proc.stdout)
    check("full-01 still unread", "full-01: 1 unread" in proc.stdout, proc.stdout)
    check("nothing ignored", "ignored" not in proc.stdout, proc.stdout)

    print("R1 a result file older than the manifest is ignored and named")
    data = data_dir(root, "r1")
    result(data, "meta-01", [(PLAIN, "ok", "this run")])
    
    result(data, "meta-09", [(PLAIN, "delete", "earlier run"), (SPACED, "archive", "earlier run"),
                             ("gone/from/inventory.md", "ok", "")], age=3600)
    proc, rows = merge(data)
    check("this run's verdict stands", rows[PLAIN]["verdict"] == "ok" and rows[PLAIN]["reviewer"] == "meta-01", rows[PLAIN])
    check("an earlier run's verdict marks nothing reviewed", rows[SPACED]["status"] == "unread", rows[SPACED])
    check("the ignored file is named",
          "ignored 1 result file(s) older than batches/manifest.tsv: meta-09.tsv" in proc.stdout, proc.stdout)
    check("its lines are not counted as bad", "bad result lines 0" in proc.stdout, proc.stdout)

    print("R2 a bad line in a result of this run is still reported")
    data = data_dir(root, "r2")
    result(data, "meta-01", [("gone/from/inventory.md", "ok", ""), (PLAIN, "not-a-verdict", "")])
    proc, rows = merge(data)
    check("two bad lines", "bad result lines 2" in proc.stdout, proc.stdout)
    check("row left unread", rows[PLAIN]["status"] == "unread", rows[PLAIN])

    print("R3 no manifest: every result file is read")
    data = data_dir(root, "r3", manifest=False)
    old = result(data, "meta-01", [(PLAIN, "keep", "")])
    os.utime(old, (1_000_000_000, 1_000_000_000))
    proc, rows = merge(data)
    check("merged", rows[PLAIN]["verdict"] == "keep", rows[PLAIN])
    check("nothing ignored", "ignored" not in proc.stdout, proc.stdout)


def main():
    os.makedirs(TMP_BASE, exist_ok=True)
    root = os.path.realpath(tempfile.mkdtemp(prefix="context-inventory-merge-", dir=TMP_BASE))
    try:
        cases(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    total = passed + len(failures)
    print("\n%d/%d passed" % (passed, total))
    for label in failures:
        print("FAILED: " + label)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
