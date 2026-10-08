#!/usr/bin/env python3
"deploy_memory cases stub store_mcp.call in-process (the test-store-doc.py pattern) over real\ntemp git repos, so repo_evidence reads a real marker and real remotes. The store_mcp cases\nreuse test-store-mcp.py's fake daemon: `_meta` reaches tools/call, a per-call deadline\nbounds a slow daemon, and repo_evidence drops credentials and a malformed marker.\n\nRun: python3 test-deploy-memory.py"
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deploy_memory  
import store_mcp  

_spec = importlib.util.spec_from_file_location("test_store_mcp", os.path.join(HERE, "test-store-mcp.py"))
assert _spec is not None and _spec.loader is not None
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)

MARKER = "0b8f6c1e-2d3a-4b5c-9d8e-7f6a5b4c3d2e"
REMOTE = "ssh://git@example.invalid/team/site.git"
TMP_BASE = os.path.expanduser("~/.cache/tmp")

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


def repo(root, name, remotes=(), marker=None):
    path = os.path.join(root, name)
    os.makedirs(path)
    subprocess.run(["git", "-C", path, "init", "-q"], check=True)
    for i, url in enumerate(remotes):
        subprocess.run(["git", "-C", path, "remote", "add", "r%d" % i, url], check=True)
    if marker is not None:
        os.makedirs(os.path.join(path, ".agents"))
        with open(os.path.join(path, ".agents", "project-id"), "w") as fh:
            fh.write('id = "%s"\n' % marker)
    return path


def hit(slug, description="", project=None, workspace=None):
    h = {"slug": slug, "description": description}
    if project:
        h["project"] = project
    if workspace:
        h["workspace"] = workspace
    return h


class Store:
    'A stub store_mcp.call: canned answers per tool, every call recorded.'

    def __init__(self, project=None, hits=(), bodies=None, fail=None):
        self.project, self.hits, self.bodies = project, list(hits), bodies or {}
        self.fail = fail or {}
        self.calls = []

    def __call__(self, tool, args=None, env=None, meta=None, deadline=None):
        self.calls.append((tool, args, meta, deadline))
        if tool in self.fail:
            raise self.fail[tool]
        if tool == "resolve_project":
            if not self.project:
                raise store_mcp.ToolError("No project found for path")
            return self.project
        if tool == "search_all":
            assert args.get("kind") == "memory", args
            return self.hits
        if tool == "get_memory":
            return {"slug": args["slug"], "body": self.bodies.get(args["slug"], "")}
        raise AssertionError("unexpected tool " + tool)

    def tools(self):
        return [c[0] for c in self.calls]


def find_with(store, cwd, **kw):
    old = store_mcp.call
    store_mcp.call = store
    try:
        return deploy_memory.find(cwd, **kw)
    finally:
        store_mcp.call = old


def slug_of(found):
    return found and found["slug"]


def cases_deploy_memory(root):
    site = repo(root, "site", [REMOTE], marker=MARKER)
    proj = {"display_name": "site-web", "workspace": "personal", "canonical_remote": "example.invalid:team/site"}

    print("D1 a memory in the repo's project that says push-to-deploy")
    s = Store(project=proj, hits=[hit("project_site_web_deploy", "push-to-deploy via post-receive", "site-web")])
    found = find_with(s, site)
    check("found", slug_of(found) == "project_site_web_deploy", found)
    check("scope is the project", found and found["scope"] == "site-web", found)
    rp = [c for c in s.calls if c[0] == "resolve_project"]
    ev = rp and (rp[0][2] or {}).get(store_mcp.EVIDENCE_META_KEY)
    check("resolve_project carries this machine's evidence",
          bool(ev) and ev["cwd"] == site and ev["marker_id"] == MARKER and ev["remotes"] == [REMOTE], rp)
    check("every call gets a deadline inside the budget",
          all(c[3] is not None and 0 < c[3] <= deploy_memory.BUDGET for c in s.calls), s.calls)

    print("D2 a project_<repo>_deploy slug counts without a body fetch")
    s = Store(hits=[hit("project_site_deploy", "how the site ships")])
    check("found", slug_of(find_with(s, site)) == "project_site_deploy")
    check("no get_memory", "get_memory" not in s.tools(), s.tools())

    print("D3 a hit in another project never counts, even when its slug names the repo")
    s = Store(project=proj, hits=[hit("site_push_to_deploy", "push-to-deploy", "other-project")])
    check("not found", find_with(s, site) is None)

    print("D4 no project resolved: a project hit whose slug names the repo, body says it")
    s = Store(hits=[hit("site-release-notes", "release steps", "site-web")],
              bodies={"site-release-notes": "a push to origin is a push-to-deploy"})
    check("found", slug_of(find_with(s, site)) == "site-release-notes")
    gm = [c for c in s.calls if c[0] == "get_memory"]
    check("get_memory asked in the hit's project", gm and gm[0][1].get("project") == "site-web", gm)

    print("D5 a shared memory counts when its body names the marker or a remote (policy)")
    for label, body, want in (
            ("names the remote", "push-to-deploy for %s" % REMOTE, True),
            ("names the marker", "push-to-deploy, project %s" % MARKER, True),
            ("names the canonical remote", "push-to-deploy for example.invalid:team/site", True),
            ("no needle", "deploys by hand from %s" % REMOTE, False)):
        s = Store(project=proj, hits=[hit("fleet-deploys", "which repos deploy", workspace="personal")],
                  bodies={"fleet-deploys": body})
        check(label, (slug_of(find_with(s, site)) == "fleet-deploys") == want)

    print("D6 a shared body naming only the basename does not count (policy)")
    s = Store(hits=[hit("fleet-deploys", "which repos deploy")],
              bodies={"fleet-deploys": "push-to-deploy: site, app"})
    check("not found", find_with(s, site) is None)

    print("D7 shared hits are fetched only from global or the repo's workspace, and only deploy ones")
    s = Store(project=proj, hits=[hit("x-deploys", "deploy notes", workspace="example-workspace"),
                                  hit("daemon-notes", "sync daemon")])
    check("not found", find_with(s, site) is None)
    check("no get_memory", "get_memory" not in s.tools(), s.tools())

    print("D8 the exact slug wins over an earlier-ranked match")
    s = Store(project=proj, hits=[hit("site-ops", "push-to-deploy notes", "site-web"),
                                  hit("project_site_web_deploy", "push-to-deploy", "site-web")])
    check("exact slug first", slug_of(find_with(s, site)) == "project_site_web_deploy")

    print("D9 the store cannot answer: StoreUnreachable reaches the caller")
    for label, fail in (("search unreachable", {"search_all": store_mcp.StoreUnreachable("down")}),
                        ("search refused", {"search_all": store_mcp.ToolError("nope")}),
                        ("resolve unreachable", {"resolve_project": store_mcp.StoreUnreachable("down")})):
        try:
            find_with(Store(fail=fail), site)
            check(label + ": raises", False, "returned")
        except store_mcp.StoreUnreachable:
            check(label + ": raises", True)

    print("D10 an exhausted budget is unreachable, not a clean no")
    try:
        find_with(Store(), site, budget=0)
        check("raises", False, "returned")
    except store_mcp.StoreUnreachable:
        check("raises", True)

    print("D11 a repo with no remote cannot push: no call at all")
    s = Store()
    check("None", find_with(s, repo(root, "local-only")) is None)
    check("no calls", s.calls == [], s.calls)

    print("D12 a search answer that is not a list is unreachable")
    s = Store()
    s.hits = {"error-ish": 1}  
    try:
        find_with(s, site)
        check("raises", False, "returned")
    except store_mcp.StoreUnreachable:
        check("raises", True)

    print("D13 a slug holding the basename inside a longer word does not name the repo (policy)")
    for label, slug, want in (("inside a word", "project_fakesite_dev_deploy", False),
                              ("whole word", "project_site_dev_deploy", True)):
        s = Store(hits=[hit(slug, "how it ships")], bodies={slug: "deploy is push-to-deploy"})
        check(label, (slug_of(find_with(s, site)) == slug) == want)


def cases_store_mcp(root):
    print("S1 meta rides along as tools/call _meta; no meta, no key")
    seen = []

    def reply(rid, params):
        seen.append(params)
        return fake.RPCReply(message={"jsonrpc": "2.0", "id": rid, "result": fake.text_result("{}")})
    server = fake.serve(reply)
    try:
        store_mcp.call("t", {"a": 1}, env=fake.env_for(server), meta={"k": {"v": 1}})
        store_mcp.call("t", {"a": 1}, env=fake.env_for(server))
    finally:
        fake.stop(server)
    check("_meta sent", seen and seen[0].get("_meta") == {"k": {"v": 1}}, seen)
    check("no _meta without meta", len(seen) == 2 and "_meta" not in seen[1], seen)

    print("S2 a per-call deadline bounds a daemon that stalls")

    def slow(rid, params):
        time.sleep(3)
        return fake.RPCReply(message={"jsonrpc": "2.0", "id": rid, "result": fake.text_result("{}")})
    server = fake.serve(slow)
    started = time.monotonic()
    try:
        store_mcp.call("t", env=fake.env_for(server), deadline=0.5)
        check("raises", False, "returned")
    except store_mcp.StoreUnreachable:
        check("raises", True)
    finally:
        fake.stop(server)
    check("within the deadline, not TIMEOUT", time.monotonic() - started < 2, time.monotonic() - started)

    print("S3 repo_evidence")
    creds = repo(root, "creds", ["https://u:pw@example.invalid/team/x.git", REMOTE], marker="not-a-uuid")
    ev = store_mcp.repo_evidence(creds)
    check("credentials dropped", ev["remotes"][0] == "https://example.invalid/team/x.git", ev)
    check("every remote", ev["remotes"][1:] == [REMOTE], ev)
    check("malformed marker is None", ev["marker_id"] is None, ev)
    sub = os.path.join(creds, "deep")
    os.makedirs(sub)
    check("cwd is the repo root", store_mcp.repo_evidence(sub)["cwd"] == creds)
    plain = os.path.join(root, "not-a-repo")
    os.makedirs(plain)
    check("outside a repo: the dir, no remotes",
          store_mcp.repo_evidence(plain) == {"cwd": plain, "marker_id": None, "remotes": []},
          store_mcp.repo_evidence(plain))


def main():
    os.makedirs(TMP_BASE, exist_ok=True)
    root = os.path.realpath(tempfile.mkdtemp(prefix="deploy-memory-", dir=TMP_BASE))
    try:
        cases_deploy_memory(root)
        cases_store_mcp(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    total = passed + len(failures)
    print("\n%d/%d passed" % (passed, total))
    for label in failures:
        print("FAILED: " + label)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
