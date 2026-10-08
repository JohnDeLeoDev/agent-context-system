#!/usr/bin/env python3
'user: "Upserts keep keys". store.upsert rebuilt an entity\'s frontmatter from the fields\nthe call passed, so a property added in Obsidian, a typed link, or any omitted optional\nfield was dropped by the next full upsert. The carry now lives in store.upsert, one site\nfor every kind; this battery keeps the check that no other site grows a write without it.\n\nCriteria: a function that writes entity frontmatter (a `_write_atomic` whose arguments\nbuild `emit_frontmatter(...)`, a `.meta.toml` sidecar or a `.meta.json` sidecar) must call\n`_carry_existing_keys` in the same function; a loader that only READS a sidecar, and a\nmachine row written with `emit_toml`, are left alone. Same PASS/FAIL style and exit code\nas the other batteries.'
import importlib.util
import os
import sys

STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))
ID = "upsert-carries-existing-keys"
spec = importlib.util.spec_from_file_location(
    "invariant_check_upsert_carry", os.path.join(STORE, "global", "scripts", "invariant-check.py"))
assert spec is not None and spec.loader is not None, "cannot load invariant-check.py"
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

passed = 0
failures = []


def check(label, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print("  PASS " + label)
    else:
        failures.append(label)
        print("  FAIL " + label + ("  -- " + detail if detail else ""))


FLAGGED = {
    "a frontmatter write with no carry":
        "class S:\n"
        "    def upsert(self, typ, key, fields, body=None):\n"
        "        meta = {'uuid': 1}\n"
        "        meta.update(fields)\n"
        "        self._write_atomic(path, emit_frontmatter(meta, body))\n",
    "a hook or script sidecar with no carry":
        "def write_hook(store, meta, bp):\n"
        "    store._write_atomic(bp + '.meta.toml', emit_toml(meta))\n",
    "a non-markdown doc sidecar spread over lines":
        "def write_doc(store, meta, path):\n"
        "    store._write_atomic(\n"
        "        path + \".meta.json\",\n"
        "        json.dumps(meta))\n",
    "a carry that sits in a different function":
        "def helper(meta, existing, fields):\n"
        "    _carry_existing_keys(meta, existing, fields)\n"
        "\n"
        "def write(store, meta, path, body):\n"
        "    store._write_atomic(path, emit_frontmatter(meta, body))\n",
}

CLEAN = {
    "the carry runs in the same function, around a nested def":
        "class S:\n"
        "    def upsert(self, typ, key, fields, body=None):\n"
        "        meta = {'uuid': 1}\n"
        "        _carry_existing_keys(meta, existing, fields)\n"
        "\n"
        "        def _cmp(d):\n"
        "            return d\n"
        "\n"
        "        self._write_atomic(path, emit_frontmatter(meta, body))\n",
    "a loader that only reads a sidecar":
        "def _load(full):\n"
        "    if os.path.exists(full + '.meta.toml'):\n"
        "        return parse_toml(open(full + '.meta.toml').read())\n",
    "a machine row written with emit_toml":
        "def set_name(store, e, meta):\n"
        "    store._write_atomic(e['_path'], emit_toml(meta))\n",
    "a comment that only names the write":
        "def f():\n"
        "    # never _write_atomic(path, emit_frontmatter(meta, body)) here\n"
        "    return 1\n",
}

print("invariant %s: pattern battery" % ID)
inv = next((i for i in mod.REGISTRY if i.id == ID), None)
check("the invariant is registered", inv is not None)
if inv is None:
    print("\ninvariant-upsert-carry: %d passed, %d failed" % (passed, len(failures)))
    sys.exit(1)

for label, src in FLAGGED.items():
    got = inv.violated("fixture.py", src)
    check("flags " + label, bool(got), "got %r" % (got,))
for label, src in CLEAN.items():
    got = inv.violated("fixture.py", src)
    check("leaves alone " + label, not got, "got %r" % (got,))

sites = [os.path.basename(p) for p in inv.sites()]
check("scans the server modules", "store.py" in sites and "memory.py" in sites, str(sites[:5]))
found, checked = inv.run()
check("the live server code passes", found == [], str(found))
check("it checked more than one site", checked > 1, str(checked))

print("\ninvariant-upsert-carry: %d passed, %d failed" % (passed, len(failures)))
sys.exit(1 if failures else 0)
