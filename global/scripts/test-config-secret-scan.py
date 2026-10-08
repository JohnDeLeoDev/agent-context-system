#!/usr/bin/env python3
'Tests for config-secret-scan.py.\n\nEvery fake credential is assembled at run time, so this file holds no literal token and\nthe scan of the store does not flag its own test.'
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(HERE, "config-secret-scan.py")
STORE = os.environ.get("AGENT_CONTEXT_STORE", os.path.expanduser("~/.agent-context"))

FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0"
FAKE_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"
FAKE_URL = "https://user:" + "hunter2pass" + "@example.com/x"
FAKE_LITERAL = "Zk3" + "9xQ2" + "mPv7Rt5" + "Ls8Nw4Y"

failures = []


def check(name, ok, detail=""):
    print("%s  %s%s" % ("ok  " if ok else "FAIL", name, ("  " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def load():
    spec = importlib.util.spec_from_file_location("config_secret_scan", SCAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rules(mod, text):
    return {rule for _, rule in mod.scan_text(text)}


def json_rules(mod, doc):
    return {rule for _, rule in mod.scan_json_values(doc)}


def main():
    mod = load()

    check("provider token is caught", "provider-token" in rules(mod, "x = '%s'" % FAKE_TOKEN))
    check("private key block is caught", "private-key" in rules(mod, FAKE_PEM))
    check("credential in a url is caught", "url-credential" in rules(mod, "curl " + FAKE_URL))
    check("credential assignment is caught",
          "literal-credential" in rules(mod, "export MY_API_TOKEN=%s" % FAKE_LITERAL))
    check("ordinary prose is clean", not rules(mod, "The token is read from 1Password at run time."))

    check("json credential key with a literal is caught",
          "literal-credential" in json_rules(mod, {"env": {"SERVICE_TOKEN": FAKE_LITERAL}}))
    check("op:// reference is not flagged",
          not json_rules(mod, {"env": {"SERVICE_TOKEN": "op://vault/item/field-name-long"}}))
    check("shell variable reference is not flagged",
          not json_rules(mod, {"env": {"SERVICE_TOKEN": "$SERVICE_TOKEN_FROM_ENV_X"}}))
    check("sha256 hash is not flagged", not json_rules(mod, {"secret_hash": "a" * 64}))
    check("uuid is not flagged",
          not json_rules(mod, {"auth_id": "123e4567-e89b-12d3-a456-426614174000"}))
    check("iso date under a token-named key is not flagged",
          not json_rules(mod, {"firstTokenDate": "2025-10-01T12:00:00.000Z"}))
    check("all-letters identifier is not flagged",
          not json_rules(mod, {"tokenKind": "STRING_LITERAL_FILTER_KIND"}))
    check("comment keys are skipped",
          not json_rules(mod, {"$comment-auth": FAKE_LITERAL}))

    tmp = tempfile.mkdtemp(prefix="cfg-scan-")
    try:
        allowed = os.path.join(tmp, "hook-test-cases.py")
        plain = os.path.join(tmp, "notes.md")
        for p in (allowed, plain):
            with open(p, "w") as fh:
                fh.write("secret = '%s'\n" % FAKE_TOKEN)
        found, n = mod.scan([allowed, plain])
        check("ALLOW skips a listed file and only that file",
              n == 1 and [os.path.basename(f["file"]) for f in found] == ["notes.md"])

        
        home = os.path.join(tmp, "home")
        store = os.path.join(tmp, "store")
        os.makedirs(os.path.join(store, "global", "hooks"))
        os.makedirs(os.path.join(store, "global", "docs"))
        os.makedirs(home)
        shutil.copy(os.path.join(STORE, "global", "hooks", "memory-husk-guard.py"),
                    os.path.join(store, "global", "hooks", "memory-husk-guard.py"))
        planted = os.path.join(store, "global", "docs", "planted.md")
        with open(planted, "w") as fh:
            fh.write("key: %s\n" % FAKE_TOKEN)
        env = dict(os.environ, HOME=home, AGENT_CONTEXT_STORE=store)
        run = subprocess.run([sys.executable, SCAN], env=env, capture_output=True, text=True)
        verdict = os.path.join(home, ".local", "state", "agent-context", "health", "config-scan.json")
        rec = None
        if os.path.exists(verdict):
            with open(verdict) as fh:
                rec = json.load(fh)
        check("planted secret gives exit 1", run.returncode == 1, run.stdout + run.stderr)
        check("verdict is written with ok false",
              rec is not None and rec.get("ok") is False and rec.get("failures"))
        check("the secret value is never printed or recorded",
              FAKE_TOKEN not in run.stdout and FAKE_TOKEN not in run.stderr
              and FAKE_TOKEN not in json.dumps(rec))
        os.remove(planted)
        run = subprocess.run([sys.executable, SCAN], env=env, capture_output=True, text=True)
        check("clean store gives exit 0 and removes the verdict",
              run.returncode == 0 and not os.path.exists(verdict), run.stdout + run.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
