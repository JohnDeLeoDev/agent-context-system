'check_integrity authoring rules: breaks found by the adversarial review.'
import json
import os
from datetime import UTC, datetime, timedelta

from agent_context import fstools as T


def test_unclosed_code_fence_counts_as_code_to_the_end(store):
    T.upsert_instruction(store, "R3", "Text before.\n```\nNEVER inside open fence\nMUST also\n")
    keys = {r["key"] for r in T.check_integrity(store)["emphatic_capitals"]}
    assert "R3" not in keys


def test_prose_before_an_unclosed_fence_is_still_scanned(store):
    T.upsert_instruction(store, "R4", "NEVER out here.\n```\ncode\n")
    rows = T.check_integrity(store)["emphatic_capitals"]
    assert next(r for r in rows if r["key"] == "R4")["words"] == {"NEVER": 1}


def test_observation_id_zero_sorts_by_its_id(store):
    d = os.path.join(store.root, "global", "audit-observations")
    os.makedirs(d, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for oid in (0, 2):
        with open(os.path.join(d, f"{oid:04d}.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": oid, "status": "resolved", "observation": "x",
                       "created_at": old, "resolved_date": old}, fh)
    ids = [r["id"] for r in T.check_integrity(store)["spent_records_in_live_dirs"]
           if r["kind"] == "observation"]
    assert ids == [0, 2]
