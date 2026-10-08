#!/usr/bin/env python3
'hook-test-run — exercise an enforcing hook\'s DENY and ALLOW paths.\n\nBOTH DIRECTIONS ARE REQUIRED. A test that only proves a hook denies is half a test --\nthe expensive failure on this fleet has repeatedly been the false positive, the guard\nthat fires on legitimate work and teaches everyone to route around it. Every hook here\ngets at least one `deny` case and one `allow` case, and the runner refuses a hook that\nhas only one kind.\n\nSHARED RUNNER, CASES IN ONE FILE. The cases live in hook-test-cases.py, keyed by hook\nname, and this runner\'s --hook filters to one. Consolidation Phase 3i deleted the\none-line per-hook test-<name>.sh wrappers that used to call this with --hook: a CASES\nentry here already satisfies the enforcing-hook-has-test invariant on its own (see\ninvariant-check.py\'s _hook_test_case_stems), so the wrapper was pure duplication. A\nhandful of hooks whose test needs real setup logic beyond a payload keep a standalone\ntest-<name>.py battery instead (test-guard-git-write.py, e.g.); everything else is run\nwith `python3 hook-test-run.py --hook <name>` directly.\n\nCONTRACT. A PreToolUse hook denies by exiting 2, or by printing a JSON\npermissionDecision of "deny". Both forms are accepted because this fleet uses both.\n\nTHREE OUTCOMES, TWO AXES. Not every hook blocks. The advisory ones -- Stop and\nPostToolUse hooks that inject a systemMessage and let the turn proceed -- are\nindistinguishable from a dead hook if the only question asked is "did it deny?", so a\nrun of `classify` sorts an invocation into deny / warn / silent and a case expects one\nof `deny`, `allow` (= did not refuse), `warn` or `silent`. A blocking hook is paired on\ndeny+allow, an advisory hook on warn+silent, and the both-directions rule is the same\neither way.\n\n  hook-test-run.py                 every hook\n  hook-test-run.py --hook block-deploy\n  hook-test-run.py --verbose\n  hook-test-run.py --hooks-dir <dir>   run the cases against another copy of the hooks:\n                                       the store\'s global/hooks before it is projected,\n                                       or a deliberately broken copy that proves a case\n                                       can fail\n  hook-test-run.py --via-dispatch      route every case through hook-dispatch.py\n  hook-test-run.py --via-client BIN    route every case through hook-client BIN, and so\n                                       through hook-server.py when one is running (policy)\n  hook-test-run.py --help | -h         usage only, runs nothing'
import argparse
import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOOKS = hp.hooks_dir()



FIXTURES = os.path.expanduser("~/.cache/hook-test-fixtures")






RUN_FIXTURES = ""
STALE_FIXTURE_SECS = 6 * 3600


def _claim_run_fixtures():
    global RUN_FIXTURES
    os.makedirs(FIXTURES, exist_ok=True)
    now = time.time()
    for name in os.listdir(FIXTURES):
        path = os.path.join(FIXTURES, name)
        try:
            stale = now - os.path.getmtime(path) > STALE_FIXTURE_SECS
        except OSError:
            continue
        if stale:
            shutil.rmtree(path, ignore_errors=True)
    RUN_FIXTURES = tempfile.mkdtemp(prefix="run-", dir=FIXTURES)
    return RUN_FIXTURES
















def load_cases():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-test-cases.py")
    spec = importlib.util.spec_from_file_location("hook_test_cases", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repeated = repeated_case_keys(path)
    if repeated:
        raise SystemExit("hook-test-run: CASES names %s more than once in %s; a dict literal "
                         "keeps only the last list, so the earlier cases never run. Merge them."
                         % (", ".join(repeated), path))
    return mod.CASES


def repeated_case_keys(path):
    'repeated case keys.'
    import ast
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
        if (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "CASES" for t in targets)):
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            return sorted({k for k in keys if keys.count(k) > 1})
    return []


def denied(proc):
    'Did this hook refuse the call? Exit 2, or an explicit deny decision.'
    if proc.returncode == 2:
        return True
    try:
        doc = json.loads(proc.stdout or "{}")
    except ValueError:
        return False
    out = doc.get("hookSpecificOutput") or doc
    if str(out.get("permissionDecision") or "").lower() == "deny":
        return True
    
    
    
    
    
    return str(doc.get("decision") or "").lower() == "block"


def spoke(proc):
    'Did this hook put words in front of the model without refusing anything?\n\n    The ADVISORY hooks -- verification-claim-check, scaffold-change-notice,\n    post-edit-verify -- never deny. Their entire product is a systemMessage or an\n    additionalContext block, so judged by `denied` alone every one of them is\n    indistinguishable from a hook that did nothing at all. That is exactly the\n    failure they exist to prevent, and until this function existed the suite could\n    not tell a working advisory hook from a dead one: it could only test the hooks\n    that block, which is itself the half-applied-invariant shape this store keeps\n    paying for.'
    if not (proc.stdout or "").strip():
        return False
    try:
        doc = json.loads(proc.stdout, strict=False)
    except ValueError:
        return True                    
    out = doc.get("hookSpecificOutput") or {}
    return any(str(doc.get(k) or out.get(k) or "").strip()
               for k in ("systemMessage", "additionalContext", "reason"))


def classify(proc):
    'deny (refused the call) | warn (spoke up) | silent (did nothing).'
    if denied(proc):
        return "deny"
    return "warn" if spoke(proc) else "silent"





SATISFIED = {"deny": {"deny"}, "allow": {"silent", "warn"},
             "warn": {"warn"}, "silent": {"silent"}}





AXES = [{"deny", "allow"}, {"warn", "silent"}, {"records", "no-record"}]


def axis_kinds(specs):
    "What a hook's cases actually assert, across all three axes.\n\n    A recorder like failure-trace never speaks, so every one of its cases is `silent`\n    and the warn/silent rule can never be satisfied -- yet it is paired perfectly well\n    on the axis it does use: a failed call writes a record, a successful one does not.\n    Judging it by output alone would have demanded a warn case it can never produce."
    kinds = {s["expect"] for s in specs}
    for spec in specs:
        for want in (spec.get("expect_files") or {}).values():
            kinds.add("no-record" if want is None else "records")
    return kinds


def _expand(obj: Any, tmp: str, uniq: str) -> Any:
    "Substitute {TMP} and {UNIQ} through a payload/env structure.\n\n    {UNIQ} exists because a test that reuses a session id ACCUMULATES that session's\n    state across runs. require-store-bootstrap yields after MAX_DENIALS refusals in one\n    session, so its deny case passed five times and then failed permanently -- a suite\n    that goes red on its sixth run for no reason anyone can see is worse than no suite.\n    Anything keyed on a session must vary per run."
    if isinstance(obj, str):
        return (obj.replace("{TMP}", tmp).replace("{UNIQ}", uniq)
                   .replace("{FIX}", os.path.join(RUN_FIXTURES or FIXTURES, uniq)))
    if isinstance(obj, dict):
        return {k: _expand(v, tmp, uniq) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v, tmp, uniq) for v in obj]
    return obj


def run_one(hook, case, verbose=False):
    "Run one case in a private temp dir, so a stateful hook can be reached.\n\n    Three of these hooks decide on state that is not in the payload -- a read ledger,\n    a bootstrap stamp keyed off the transcript, an LSP health verdict. Without a way to\n    stage that state their deny branch is untestable, and the first version of this\n    suite simply recorded three failures that were really missing fixtures. `setup`\n    writes files into a per-case temp dir and `{TMP}` expands to it anywhere in the\n    payload, env or cwd, which makes those branches reachable WITHOUT touching real\n    health state or a real session's ledger."
    path = os.path.join(HOOKS, hook)
    if not os.path.exists(path) and hook.endswith(".sh"):
        
        
        ported = path[:-3] + ".py"
        path = ported if os.path.exists(ported) else path
    if not os.path.exists(path):
        return False, "hook not materialized at %s" % path
    runner = [sys.executable, path] if path.endswith(".py") else ["bash", path]
    if VIA_DISPATCH:
        
        
        
        runner = [sys.executable, DISPATCH, event_of(hook, case), "--guard", path]
        if VIA_CLIENT:
            runner = [VIA_CLIENT] + runner

    tmp = tempfile.mkdtemp(prefix="hooktest-")
    uniq = os.path.basename(tmp).replace("hooktest-", "")
    try:
        for rel, content in (case.get("setup") or {}).items():
            dest = os.path.join(tmp, _expand(rel, tmp, uniq))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(_expand(content, tmp, uniq))
        
        
        
        
        
        if case.get("pre"):
            pre = subprocess.run(["bash", "-c", _expand(case["pre"], tmp, uniq)],
                                 capture_output=True, text=True, cwd=tmp,
                                 timeout=case.get("timeout", 30), env=_signed(os.environ))
            if pre.returncode != 0:
                return False, "setup command failed: %s" % (
                    (pre.stderr or pre.stdout or "").strip()[:160])
        payload = json.dumps(_expand(case["payload"], tmp, uniq))
        env = {**os.environ, **_expand(case.get("env") or {}, tmp, uniq)}
        
        
        cwd = os.path.expanduser(_expand(case.get("cwd") or "~", tmp, uniq))
        try:
            proc = subprocess.run(runner, input=payload, capture_output=True, text=True,
                                  timeout=case.get("timeout", 30), cwd=cwd, env=env)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        except OSError as exc:
            return False, str(exc)
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        shown = (proc.stdout or "") + (proc.stderr or "")
        said = []
        for want in (case.get("expect_output") or []):
            if _expand(want, tmp, uniq) not in shown:
                said.append("output lacks %r" % want)
        for unwanted in (case.get("expect_output_absent") or []):
            if _expand(unwanted, tmp, uniq) in shown:
                said.append("output should NOT contain %r" % unwanted)

        
        
        
        
        
        
        
        effects = []
        for rel, want in (case.get("expect_files") or {}).items():
            dest = os.path.join(tmp, _expand(rel, tmp, uniq))
            exists = os.path.exists(dest)
            if want is None:
                if exists:
                    effects.append("%s should not exist" % rel)
                continue
            if not exists:
                effects.append("%s was never written" % rel)
                continue
            with open(dest, encoding="utf-8", errors="replace") as fh:
                blob = fh.read()
            if _expand(want, tmp, uniq) not in blob:
                effects.append("%s lacks %r" % (rel, want))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    got = classify(proc)
    ok = (got in SATISFIED.get(case["expect"], frozenset())
          and not effects and not said)
    detail = "expected %s, got %s" % (case["expect"], got)
    if said:
        detail += " :: " + "; ".join(said)
    if effects:
        detail += " :: side effects — " + "; ".join(effects)
    if verbose or not ok:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().replace("\n", " ")
        detail += " :: " + (tail[:140] or "(no output)")
    return ok, detail


VIA_DISPATCH = False
VIA_CLIENT = ""
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DISPATCH = os.path.join(SCRIPTS_DIR, "hook-dispatch.py")
_REGISTERED = []


def event_of(hook, case):
    "The event a case belongs to: its payload's hook_event_name, else the first event\n    the hook is registered under (dispatched events expanded), else PreToolUse."
    payload = case.get("payload")
    named = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if named:
        return named
    if not _REGISTERED:
        spec = importlib.util.spec_from_file_location(
            "hook_registry", os.path.join(SCRIPTS_DIR, "hook-registry.py"))
        if spec is None or spec.loader is None:
            raise ImportError("cannot load hook-registry.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        hooks = mod.effective_hooks(hp.settings_file())
        _REGISTERED.append([(mod.script_name(h.get("command")), event)
                            for event, groups in hooks.items()
                            for g in groups for h in g.get("hooks") or []])
    return next((event for name, event in _REGISTERED[0] if name == hook), "PreToolUse")


_SIGNING_KEY = []


def _signed(env):
    "`env` set to sign every commit a case's setup commands make, with a throwaway key."
    if not _SIGNING_KEY:
        d = tempfile.mkdtemp(prefix="hook-test-key-")
        atexit.register(shutil.rmtree, d, True)
        key = os.path.join(d, "key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "hook-test",
                        "-f", key], check=True, capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=30)
        
        
        
        
        
        
        os.chmod(key, 0o600)
        _SIGNING_KEY.append(key)
    out = dict(env)
    try:
        n = int(out.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    for key, value in (("gpg.format", "ssh"),
                       ("gpg.ssh.program", shutil.which("ssh-keygen") or "ssh-keygen"),
                       ("user.signingkey", _SIGNING_KEY[0]), ("commit.gpgsign", "true")):
        out["GIT_CONFIG_KEY_%d" % n] = key
        out["GIT_CONFIG_VALUE_%d" % n] = value
        n += 1
    out["GIT_CONFIG_COUNT"] = str(n)
    return out


def build_parser():
    p = argparse.ArgumentParser(
        prog="hook-test-run.py",
        description="Exercise an enforcing hook's DENY and ALLOW paths.",
    )
    p.add_argument("--hook", metavar="NAME",
                    help="run only cases for this hook (matched by stem, ignoring .sh/.py)")
    p.add_argument("--hooks-dir", metavar="DIR",
                    help="run cases against hooks in DIR instead of ~/.agent-context/global/hooks")
    p.add_argument("--verbose", action="store_true",
                    help="print every passing case's detail line too, not just failures")
    p.add_argument("--via-dispatch", action="store_true",
                    help="route each case through hook-dispatch.py instead of the hook directly")
    p.add_argument("--via-client", metavar="BIN",
                    help="as --via-dispatch, launched through the hook-client binary BIN")
    return p


def main(argv):
    global VIA_DISPATCH, VIA_CLIENT, HOOKS
    args = build_parser().parse_args(argv[1:])
    only = args.hook
    if args.hooks_dir is not None:
        if not os.path.isdir(args.hooks_dir):
            print("hook-test-run: --hooks-dir needs an existing directory", file=sys.stderr)
            return 2
        HOOKS = os.path.abspath(args.hooks_dir)
    verbose = args.verbose
    VIA_CLIENT = os.path.abspath(args.via_client) if args.via_client else ""
    VIA_DISPATCH = args.via_dispatch or bool(VIA_CLIENT)
    cases = load_cases()
    if only:
        
        
        
        
        
        
        
        
        
        
        def _stem(name):
            return name[:-3] if name.endswith((".sh", ".py")) else name
        only_stem = _stem(only)
        cases = {k: v for k, v in cases.items() if _stem(k).startswith(only_stem)}
        if not cases:
            print("hook-test-run: no cases for %r" % only, file=sys.stderr)
            return 2

    failures = total = 0
    _claim_run_fixtures()

    for hook, specs in sorted(cases.items()):
        kinds = axis_kinds(specs)
        if not any(axis <= kinds for axis in AXES):
            print("  %-34s INCOMPLETE — needs both directions of one axis: deny+allow "
                  "(a hook that blocks), warn+silent (one that advises), or a present "
                  "and an absent expect_files (one that records)" % hook)
            failures += 1
            continue
        bad = []
        for i, spec in enumerate(specs, 1):
            total += 1
            t0 = time.time()
            print("hook-test-run: %s [%d/%d] %s starting" % (hook, i, len(specs), spec["name"]),
                  file=sys.stderr, flush=True)
            ok, detail = run_one(hook, spec, verbose)
            print("hook-test-run: %s [%d/%d] %s %s in %ds"
                  % (hook, i, len(specs), spec["name"], "ok" if ok else "FAIL",
                     time.time() - t0), file=sys.stderr, flush=True)
            if not ok:
                bad.append("%s: %s" % (spec["name"], detail))
            elif verbose:
                print("      %s: %s" % (spec["name"], detail))
        if bad:
            failures += len(bad)
            print("  %-34s FAIL" % hook)
            for b in bad:
                print("      %s" % b)
        else:
            print("  %-34s ok (%d case(s))" % (hook, len(specs)))

    shutil.rmtree(RUN_FIXTURES, ignore_errors=True)
    print("\n  %d case(s), %d failure(s)%s"
          % (total, failures, ", via hook-client" if VIA_CLIENT
             else ", via hook-dispatch.py" if VIA_DISPATCH else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:                  
        print("hook-test-run: %r" % (exc,), file=sys.stderr)
        sys.exit(2)
