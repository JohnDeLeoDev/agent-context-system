'T3.3 — report-only index-integrity scan (check_integrity).'
from agent_context import fstools as T


def _raw_memory(store, slug, description, project=None):
    'Write a memory BELOW the guard. upsert_memory refuses over-length descriptions\n    since build 17; the hygiene report must still find the ones written before that.'
    store.upsert("memory", slug, {"memory_type": "reference", "description": description,
                                  "origin": "agent"}, body="b", project=project)


def test_detects_cross_scope_duplicate_slug(store):
    T.upsert_project(store, "github.com:org/p", "P")   
    T.upsert_memory(store, "dup", "reference", "d", "global body", project=None)
    T.upsert_memory(store, "dup", "reference", "d", "proj body", project="P")
    f = T.check_integrity(store)
    slugs = [x["slug"] for x in f["cross_scope_duplicate_slugs"]]
    assert "dup" in slugs
    entry = next(x for x in f["cross_scope_duplicate_slugs"] if x["slug"] == "dup")
    assert set(entry["scopes"]) == {"global", "project:P"}
    assert f["summary"]["cross_scope_duplicate_slugs"] >= 1


def _agent_definition_prose(n=20):
    'Body shaped like a real agent_definition body: enough shared paragraphs that\n    similarity vs a genuinely unrelated document of the same size is unambiguous.'
    return "\n\n".join(
        f"Paragraph {i}: shared prose about worker behavior, step {i}, describing "
        f"what the agent must do during phase {i} of the workflow in some detail."
        for i in range(1, n + 1))


def _condensed_rewrite(global_body):
    ' condensed rewrite.'
    paras = global_body.split("\n\n")
    del paras[5:8]
    paras[2] = "Paragraph 3 REWRITTEN: a project-specific tweak to this one paragraph."
    return "\n\n".join(paras)


def _unrelated_prose(n=20):
    'Same shape and roughly the same size as _agent_definition_prose, but shares no\n    substance with it — two documents that happen to share a kind+key, matching the\n    real worktrees.md case: three project docs of similar SIZE to the global one,\n    holding genuinely different per-project content.'
    return "\n\n".join(
        f"Section {i}: an entirely different, project-specific landing step, "
        f"covering an unrelated setup concern at position {i} of this document."
        for i in range(1, n + 1))


def test_detects_cross_scope_duplicate_entity_agent_definition(store):
    T.upsert_project(store, "github.com:org/p2", "P2")
    global_body = _agent_definition_prose()
    T.upsert_agent_definition(store, "worker-explore", description="d",
                              body=global_body, project=None)
    T.upsert_agent_definition(store, "worker-explore", description="d",
                              body=_condensed_rewrite(global_body), project="P2")
    f = T.check_integrity(store)
    hits = [x for x in f["cross_scope_duplicate_entities"]
            if x["kind"] == "agent_definition" and x["key"] == "worker-explore"]
    assert len(hits) == 1
    assert hits[0]["scope"] == "project:P2"
    assert hits[0]["global_scope"] == "global"
    assert "delete_entity" not in hits[0]["fix"]
    assert f["summary"]["cross_scope_duplicate_entities"] >= 1


def test_detects_cross_scope_duplicate_entity_script(store):
    T.upsert_project(store, "github.com:org/p3", "P3")
    global_body = _agent_definition_prose()
    T.upsert_script(store, "some-tool", global_body, description="d", project=None)
    T.upsert_script(store, "some-tool", _condensed_rewrite(global_body),
                    description="d", project="P3")
    f = T.check_integrity(store)
    hits = [x for x in f["cross_scope_duplicate_entities"]
            if x["kind"] == "script" and x["key"] == "some-tool"]
    assert len(hits) == 1
    assert hits[0]["scope"] == "project:P3"


def test_similar_size_but_different_content_is_not_flagged_as_fork(store):
    'test similar size but different content is not flagged as fork.'
    T.upsert_project(store, "github.com:org/p6", "P6")
    T.upsert_doc(store, "worktrees.md", _agent_definition_prose(), title="Worktrees",
                project=None)
    T.upsert_doc(store, "worktrees.md", _unrelated_prose(), title="Worktrees",
                project="P6")
    f = T.check_integrity(store)
    assert all(x["key"] != "worktrees.md" for x in f["cross_scope_duplicate_entities"])


def test_shim_script_forwarding_to_global_is_not_reported(store):
    T.upsert_project(store, "github.com:org/p4", "P4")
    T.upsert_script(store, "wt-sweep", "the real logic\n", description="d", project=None)
    shim_body = (
        "#!/usr/bin/env bash\n"
        "# Shim. The husk sweep is identical for every project, so the logic lives once at\n"
        "# the store's global/scripts/wt-sweep.py.\n"
        "set -euo pipefail\n"
        'g="${AGENT_CONTEXT_STORE:-$HOME/.agent-context}/global/scripts/wt-sweep.py"\n'
        '[ -f "$g" ] || { echo "wt-sweep: store copy missing at $g" >&2; exit 0; }\n'
        'exec python3 "$g" "$@"\n'
    )
    T.upsert_script(store, "wt-sweep", shim_body, description="d", project="P4")
    f = T.check_integrity(store)
    hits = [x for x in f["cross_scope_duplicate_entities"] if x["key"] == "wt-sweep"]
    assert hits == [], hits


def test_project_entity_with_no_global_counterpart_is_not_reported(store):
    T.upsert_project(store, "github.com:org/p5", "P5")
    T.upsert_script(store, "only-here", "echo hi\n", description="d", project="P5")
    f = T.check_integrity(store)
    assert all(x["key"] != "only-here" for x in f["cross_scope_duplicate_entities"])


def test_global_only_entity_is_not_reported(store):
    T.upsert_script(store, "global-only", "echo hi\n", description="d", project=None)
    f = T.check_integrity(store)
    assert all(x["key"] != "global-only" for x in f["cross_scope_duplicate_entities"])


def test_empty_store_reports_no_cross_scope_duplicate_entities(store):
    f = T.check_integrity(store)
    assert f["cross_scope_duplicate_entities"] == []
    assert f["summary"]["cross_scope_duplicate_entities"] == 0


def test_fork_length_guard_skips_ratio_computation_for_far_apart_sizes(monkeypatch):
    "A fork of a body is never a tenth of that body's size, so a pair further\n    apart than that must never reach `SequenceMatcher.ratio()` (O(n*m)) at all —\n    not just fail its threshold. Spy on the real comparator to prove the guard\n    actually short-circuits, rather than merely happening to score low."
    import agent_context.integrity as I
    calls = []
    real_matcher = I.difflib.SequenceMatcher

    def spy(*args, **kwargs):
        calls.append(args)
        return real_matcher(*args, **kwargs)

    monkeypatch.setattr(I.difflib, "SequenceMatcher", spy)

    assert I._is_fork("x" * 10000, "y" * 100) is False
    assert calls == [], "far-apart sizes must never reach the ratio() comparison"

    prose = _agent_definition_prose()
    assert I._is_fork(prose, _condensed_rewrite(prose)) is True
    assert calls, "a close-sized pair must reach the actual comparison"


def test_detects_load_error(store):
    
    bad = store.root + "/global/broken.meta.json"
    open(bad, "w").write("{ this is not valid json ")
    store.reload()
    f = T.check_integrity(store)
    assert f["summary"]["load_errors"] >= 1
    assert any("broken.meta.json" in le["path"] for le in f["load_errors"])


def test_detects_dangling_link(store):
    T.upsert_memory(store, "real", "reference", "d", "I am real", project=None)
    T.upsert_memory(store, "src", "reference", "d",
                    "points to [[real]] and to [[ghost-note]]", project=None)
    f = T.check_integrity(store)
    targets = {d["target"] for d in f["dangling_links"]}
    assert "ghost-note" in targets      
    assert "real" not in targets        


def test_path_qualified_file_links_resolve_without_hiding_missing_paths(store):
    T.upsert_project(store, "github.com:org/p", "P")
    T.upsert_doc(store, "core-system-health.md", "body", project=None, title="Core health")
    T.upsert_doc(store, "architecture.md", "body", project="P", title="Architecture")
    T.upsert_skill(store, "triage", "A skill", "body", project=None)
    T.upsert_command(store, "run", "body", description="A command", project="P")
    T.upsert_memory(
        store, "links", "reference", "links", "\n".join([
            "[[global/docs/core-system-health|health]]",
            "[[projects/P/docs/architecture|architecture]]",
            "[[global/skills/triage/SKILL|triage]]",
            "[[projects/P/commands/run|run]]",
            "[[core-system-health.md]]",
            "[[projects/P/docs/missing|missing]]",
        ]), project=None,
    )
    targets = {d["target"] for d in T.check_integrity(store)["dangling_links"]}
    assert targets == {"projects/P/docs/missing"}


def test_prose_in_brackets_is_not_a_dangling_link(store):
    
    
    prose = (
        "see [[Rider MCP tools]], the [[.only(WindowInsetsSides.Horizontal)]] rule, "
        'the [[-n "$ONBOX"]] guard, and [[Full Reset clears data, doesn\'t drop schema]]'
    )
    T.upsert_memory(store, "src", "reference", "d", prose, project=None)
    f = T.check_integrity(store)
    assert f["summary"]["dangling_links"] == 0, f["dangling_links"]


def test_code_spans_are_not_scanned_for_links(store):
    
    
    body = (
        "add one `[[git_mirror.repos]]` entry; `.chezmoidata.toml` `[[ssh_servers]]` "
        "opts a host out. Double-backtick ``[[weird]]`` too.\n"
        "```toml\n[[git_mirror.repos]]\nname = \"agent-context\"\n```\n"
    )
    T.upsert_memory(store, "src", "reference", "d", body, project=None)
    f = T.check_integrity(store)
    assert f["summary"]["dangling_links"] == 0, f["dangling_links"]


def test_link_outside_code_still_flagged_when_body_has_code(store):
    
    T.upsert_memory(store, "src", "reference", "d",
                    "config `[[ssh_servers]]` — see [[ghost-note]] for context", project=None)
    f = T.check_integrity(store)
    targets = {d["target"] for d in f["dangling_links"]}
    assert targets == {"ghost-note"}, f["dangling_links"]


def test_slug_and_docpath_shaped_dangling_still_flagged(store):
    
    
    T.upsert_memory(store, "src", "reference", "d",
                    "[[feedback_fold_variants_into_existing_types]] and "
                    "[[example-workspace/Projects/example-api/Documentation/getting-started]]", project=None)
    f = T.check_integrity(store)
    targets = {d["target"] for d in f["dangling_links"]}
    assert "feedback_fold_variants_into_existing_types" in targets
    assert "example-workspace/Projects/example-api/Documentation/getting-started" in targets


def test_detects_long_description(store):
    long_desc = "x" * 200
    _raw_memory(store, "verbose", long_desc)
    T.upsert_memory(store, "terse", "reference", "short", "body", project=None)
    f = T.check_integrity(store)
    slugs = {d["slug"] for d in f["long_memory_descriptions"]}
    assert "verbose" in slugs
    assert "terse" not in slugs
    entry = next(d for d in f["long_memory_descriptions"] if d["slug"] == "verbose")
    assert entry["length"] == 200


def test_bootstrap_footprint_counts_rendered_rows_and_tiers_lazy_cheaply(store):
    T.upsert_memory(store, "a", "reference", "x" * 50, "body", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "b", "reference", "y" * 30, "body", project=None, load_behavior="lazy")
    f = T.check_integrity(store)
    g = f["bootstrap_footprint"]["global"]
    assert g["loaded_memory_count"] == 1
    
    
    assert g["memory_row_bytes"] == len("r a — " + "x" * 50) + 1
    
    assert g["lazy_memory_count"] == 1
    assert g["lazy_roster_bytes"] == len("b") + 1
    assert g["est_bootstrap_bytes"] == (g["always_instruction_bytes"] + g["memory_row_bytes"]
                                        + g["lazy_roster_bytes"])
    assert g["est_bootstrap_tokens"] == round(g["est_bootstrap_bytes"] / 4)
    assert g["over_budget"] is False and f["over_budget_scopes"] == []


def test_over_budget_scope_is_reported_as_a_finding(store):
    for i in range(200):
        T.upsert_memory(store, f"m-{i:03}", "project", "d" * 130, "b", project=None,
                        load_behavior="always")
    f = T.check_integrity(store)
    assert f["over_budget_scopes"] == ["global"]
    assert f["bootstrap_footprint"]["global"]["over_budget"] is True


def test_clean_store_reports_nothing(store):
    T.upsert_memory(store, "solo", "reference", "fine", "body [[solo-self]]", project=None)
    
    T.upsert_doc(store, "solo-self", "x", title="solo-self")
    f = T.check_integrity(store)
    assert f["summary"]["cross_scope_duplicate_slugs"] == 0
    assert f["summary"]["long_memory_descriptions"] == 0
    assert all(d["target"] != "solo-self" for d in f["dangling_links"])


def test_worktree_copy_of_the_store_is_not_indexed(store):
    "A git worktree under .claude/worktrees/ is a full second copy of this tree.\n    Walking into it double-indexed every entity AND mis-scoped project memories to\n    global (relpath starts with '.claude'), leaking them into every session."
    import os
    T.upsert_project(store, "gh:org/repo", "P")
    T.upsert_memory(store, "scoped", "project", "d", "real body", project="P")
    wt = os.path.join(store.root, ".claude", "worktrees", "wt", "projects", "P", "memory")
    os.makedirs(wt, exist_ok=True)
    with open(os.path.join(wt, "scoped.md"), "w") as f:
        f.write('---\nuuid: "dead-beef"\ntype: "memory"\nslug: "scoped"\n'
                'memory_type: "project"\ndescription: "copy"\n---\n\ncopy body\n')
    store.reload()
    mems = [e for e in store.entities.values() if e.get("type") == "memory"]
    assert [e["scope"] for e in mems] == ["project:P"]   
    assert "copy" not in T.get_session_context(store, "/no/project")["memory_index"]["global"]["rows"]


def test_workspace_instructions_load_for_member_projects(store):
    'A workspace instruction marked `always` was silently dropped from every\n    session: get_instructions fetched the project record and never read it, and the\n    lookup it used could not have resolved a project anyway.'
    import os
    T.upsert_project(store, "gh:org/repo", "P")
    store.project_entity("P")["workspace"] = "W"
    os.makedirs(os.path.join(store.root, "workspaces", "W", "instructions"), exist_ok=True)
    with open(os.path.join(store.root, "workspaces", "W", "instructions", "w.md"), "w") as f:
        f.write('---\nuuid: "w-uuid"\ntype: "instruction"\ntitle: "W rules"\n'
                'load_behavior: "always"\n---\n\nworkspace body\n')
    store.reload()
    store.project_entity("P")["workspace"] = "W"
    titles = lambda p: [e["title"] for e in store.get_instructions(p, "always")]
    assert "W rules" in titles("P")          
    assert "W rules" not in titles(None)     


def test_reported_size_matches_what_the_bootstrap_actually_emits(store):
    '_index_health and check_integrity must agree with the rendered payload —\n    a report drifting from the thing it measures is what let the index grow unseen.'
    for i in range(12):
        T.upsert_memory(store, f"m-{i}", "project", "d" * 60, "b", project=None)
    for i in range(5):
        T.upsert_memory(store, f"z-{i}", "reference", "d" * 60, "b", project=None,
                        load_behavior="lazy")
    block = T.get_session_context(store, "/no/project")["memory_index"]["global"]
    rendered = len(block["rows"]) + 1 + len(block.get("lazy", "")) + 1
    footprint = T.check_integrity(store)["bootstrap_footprint"]["global"]
    counted = footprint["memory_row_bytes"] + footprint["lazy_roster_bytes"]
    assert abs(rendered - counted) <= 2   


def test_stale_always_loaded_lists_quiet_project_memories_whatever_they_say(store):
    'test stale always loaded lists quiet project memories whatever they say.'
    T.upsert_memory(store, "quiet-worklog", "project", "ongoing migration work", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "quiet-done", "project", "Phase B COMPLETE, landed", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "fresh-worklog", "project", "ongoing work", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "quiet-lazy", "project", "ongoing work", "b", project=None,
                    load_behavior="lazy")
    T.upsert_memory(store, "quiet-reference", "reference", "a trap", "b", project=None,
                    load_behavior="always")
    for slug in ("quiet-worklog", "quiet-done", "quiet-lazy", "quiet-reference"):
        store.get("memory", slug, None)["updated_at"] = "2020-01-01T00:00:00Z"
    f = T.check_integrity(store)
    rows = {r["slug"]: r for r in f["stale_always_loaded"]}
    assert set(rows) == {"quiet-worklog", "quiet-done"}      
    assert rows["quiet-worklog"]["description"] == "ongoing migration work"
    assert "load_behavior='lazy'" in rows["quiet-worklog"]["fix"]
    assert f["summary"]["stale_always_loaded"] == 2
    
    assert {r["slug"] for r in f["prune_candidates"]} <= set(rows)


def test_prune_candidates_veto_unfinished_work(store):
    '#313: `pending deploy`/`not deployed`/`undeployed` used to sit inside\n    _STABILIZED_RE itself, so a description saying work is NOT done matched the same\n    pattern as one saying it IS done. The veto must win regardless of what\n    stabilized-sounding word rides along in the same description.'
    unfinished = {
        "not-done-pending": "STATUS: landed dev (17fde1fb), PENDING full dev-test-prod deploy",
        "not-done-not-deployed": "Fix merged and complete on dev but not deployed to prod yet",
        "not-done-undeployed": "Migration landed on dev; undeployed everywhere else",
        "not-done-in-progress": "Rollout landed in dev, resolved locally, but still in progress fleet-wide",
        "not-done-wip": "WIP: shipped a partial fix, complete rewrite still pending",
    }
    for slug, desc in unfinished.items():
        T.upsert_memory(store, slug, "project", desc, "b", project=None)
        store.get("memory", slug, None)["updated_at"] = "2020-01-01T00:00:00Z"
    f = T.check_integrity(store)
    slugs = {r["slug"] for r in f["prune_candidates"]}
    assert slugs.isdisjoint(unfinished)


def test_prune_candidates_flags_genuinely_stabilized_old_work(store):
    T.upsert_memory(store, "finished-old", "project",
                    "Migration landed, shipped, and resolved", "b", project=None,
                    load_behavior="always")
    store.get("memory", "finished-old", None)["updated_at"] = "2020-01-01T00:00:00Z"
    f = T.check_integrity(store)
    assert "finished-old" in {r["slug"] for r in f["prune_candidates"]}


def test_prune_candidates_skips_stabilized_but_recent_work(store):
    'The 30-day cutoff still applies once the veto is in place.'
    T.upsert_memory(store, "finished-recent", "project",
                    "Migration landed, shipped, and resolved", "b", project=None)
    f = T.check_integrity(store)
    assert "finished-recent" not in {r["slug"] for r in f["prune_candidates"]}


def test_stale_and_prune_fix_strings_never_propose_delete_entity(store):
    "#313: age (and a description regex) is not evidence a memory is safe to delete —\n    only cold_always_loaded's read/hit telemetry is. Both fix strings must cap their\n    suggestion at the reversible set_memory_load_behavior(slug, 'lazy') tier change."
    T.upsert_memory(store, "quiet-worklog", "project", "ongoing migration work", "b", project=None,
                    load_behavior="always")
    T.upsert_memory(store, "finished-old", "project",
                    "Migration landed, shipped, and resolved", "b", project=None,
                    load_behavior="always")
    for slug in ("quiet-worklog", "finished-old"):
        store.get("memory", slug, None)["updated_at"] = "2020-01-01T00:00:00Z"
    f = T.check_integrity(store)
    stale_fix = next(r["fix"] for r in f["stale_always_loaded"] if r["slug"] == "quiet-worklog")
    assert "delete_entity" not in stale_fix
    health = T.get_session_context(store, "/no/project")["index_health"]
    assert "finished-old" in health["prune_candidates"]
    assert "delete_entity" not in health["fix"]


def _delete_file_behind_the_index(store, typ, key, project=None):
    "Remove an entity's FILE without telling the store, the way another machine's\n    delete arrives here: git writes the deletion to disk and nothing reloads until\n    the next sync cycle."
    import os
    scope = store._scope_for(project)
    uid = store.by_key[(typ, scope, key)]
    path = store.entities[uid]["_path"]
    os.remove(path)
    return path


def test_a_deleted_file_stops_being_served_without_a_full_reload(store):
    '`_fresh` must EVICT on a vanished file, not keep answering from the index.'
    T.upsert_memory(store, "doomed", "reference", "d", "body", project=None)
    assert T.get_memory(store, "doomed") is not None

    _delete_file_behind_the_index(store, "memory", "doomed")

    assert T.get_memory(store, "doomed") is None
    assert ("memory", "global", "doomed") not in store.by_key


def test_integrity_never_suggests_a_target_whose_file_is_gone(store):
    'The scan walks `store.entities` directly, so `_fresh` never runs for it.'
    T.upsert_doc(store, "inbox/keeper.md", "body", title="Keeper")
    T.upsert_doc(store, "archive/inbox/keeper.md", "body", title="Archived keeper")
    T.upsert_memory(store, "src", "reference", "d",
                    'see get_doc("inbox/keeper.md")', project=None)

    
    _delete_file_behind_the_index(store, "doc", "inbox/keeper.md")
    _delete_file_behind_the_index(store, "doc", "archive/inbox/keeper.md")

    f = T.check_integrity(store)
    broken = [b for b in f["broken_pointers"] if b["target"] == "inbox/keeper.md"]
    assert broken, "a pointer to a deleted doc must still be REPORTED"
    assert "suggest" not in broken[0], (
        f"suggested {broken[0].get('suggest')!r}, whose file no longer exists")


def test_sweep_vanished_reports_what_it_evicted(store):
    T.upsert_memory(store, "ghost", "reference", "d", "body", project=None)
    T.upsert_memory(store, "alive", "reference", "d", "body", project=None)
    _delete_file_behind_the_index(store, "memory", "ghost")

    assert store.sweep_vanished() == 1
    assert store.sweep_vanished() == 0          
    assert T.get_memory(store, "alive") is not None


def test_an_unreadable_directory_never_evicts(store, monkeypatch):
    'A failed stat is not proof of deletion, and a false positive is the costly one.\n\n    Evicting on one bad stat makes a live document unreachable to every get_* and\n    every write until the next full reload. Waiting one cycle costs nothing.'
    import os
    T.upsert_memory(store, "survivor", "reference", "d", "body", project=None)
    uid = store.by_key[("memory", "global", "survivor")]
    path = store.entities[uid]["_path"]

    real_exists = os.path.exists
    
    calls = {"n": 0}
    def flaky(p):
        if p == path:
            calls["n"] += 1
            if calls["n"] == 1:
                return False
        return real_exists(p)
    monkeypatch.setattr(os.path, "exists", flaky)

    assert store.sweep_vanished() == 0, "a single failed stat must not evict"
    monkeypatch.undo()
    assert T.get_memory(store, "survivor") is not None


def test_eviction_still_happens_when_the_file_is_truly_gone(store):
    'The hardening must not defeat the fix it guards.'
    T.upsert_memory(store, "really-doomed", "reference", "d", "body", project=None)
    _delete_file_behind_the_index(store, "memory", "really-doomed")
    assert store.sweep_vanished() == 1
    assert T.get_memory(store, "really-doomed") is None












def _long_body(marker):
    'Over _DUP_MIN_BYTES (600), so the duplicate-body check actually considers it.'
    return (f"{marker}\n" + "a settled paragraph that is long enough to be compared. " * 20)


def test_archived_doc_pointer_is_not_reported(store):
    T.upsert_doc(store, "archive/inbox/old-handoff.md",
                 'shipped; see get_doc("api/guides/gone.md")', title="Old handoff")
    f = T.check_integrity(store)
    assert not [b for b in f["broken_pointers"]
                if b["source"] == "archive/inbox/old-handoff.md"], \
        "an archived record's pointer names the past on purpose"


def test_live_doc_pointer_is_still_reported(store):
    'The control. Without this, an exclusion that disabled the check would pass.'
    T.upsert_doc(store, "features/live-thing.md",
                 'see get_doc("api/guides/gone.md")', title="Live thing")
    f = T.check_integrity(store)
    assert [b for b in f["broken_pointers"] if b["source"] == "features/live-thing.md"], \
        "a live doc's broken pointer is a real hazard and must still be reported"


def test_a_dash_archive_directory_is_also_archived(store):
    'test a dash archive directory is also archived.'
    T.upsert_doc(store, "parity-archive/2026-09-04-snapshot.md",
                 'see get_doc("api/guides/gone.md")', title="Snapshot")
    f = T.check_integrity(store)
    assert not [b for b in f["broken_pointers"]
                if b["source"].startswith("parity-archive/")]


def test_archive_in_a_filename_does_not_exempt_a_live_doc(store):
    'The exclusion is on path SEGMENTS, not a substring anywhere in the path.\n\n    `features/archive-retention.md` is live knowledge that happens to be ABOUT archiving,\n    and a substring match would have stopped checking it.'
    T.upsert_doc(store, "features/archive-retention.md",
                 'see get_doc("api/guides/gone.md")', title="Archive retention")
    f = T.check_integrity(store)
    assert [b for b in f["broken_pointers"]
            if b["source"] == "features/archive-retention.md"], \
        "a live doc whose NAME mentions archiving is still live"


def test_a_snapshot_is_not_a_duplicate_body(store):
    body = _long_body("rules")
    T.upsert_doc(store, "ship/ref/rules.md", body, title="Rules")
    T.upsert_doc(store, "parity-archive/2026-09-04-ship-ref-rules.md", body,
                 title="Rules — snapshot")
    f = T.check_integrity(store)
    for dup in f["duplicate_bodies"]:
        names = [c["name"] for c in dup["copies"]]
        assert "parity-archive/2026-09-04-ship-ref-rules.md" not in names, \
            "a snapshot is a copy of its original by design"


def test_two_live_copies_are_still_a_duplicate_body(store):
    'The control for the duplicate half — this is the divergence hazard the check exists\n    for, and it must survive the archive exclusion.'
    body = _long_body("shared")
    T.upsert_doc(store, "features/one.md", body, title="One")
    T.upsert_doc(store, "features/two.md", body, title="Two")
    f = T.check_integrity(store)
    assert any({"features/one.md", "features/two.md"} <= {c["name"] for c in dup["copies"]}
               for dup in f["duplicate_bodies"]), \
        "two live copies of one body is exactly what this check is for"
