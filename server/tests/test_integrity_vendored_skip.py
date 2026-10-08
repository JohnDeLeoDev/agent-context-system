'check_integrity: emphatic_capitals exempts vendored (upstream-authored) content.'
from agent_context import fstools as T


def test_capitals_skip_vendored_skill(store):
    T.upsert_skill(store, "vendored-sk", description="d", body="ALWAYS run the suite.",
                   upstream="Google LLC, Android Studio agent skills")
    keys = {r["key"] for r in T.check_integrity(store)["emphatic_capitals"]}
    assert "vendored-sk" not in keys


def test_capitals_still_flagged_without_upstream(store):
    T.upsert_skill(store, "plain-sk", description="d", body="ALWAYS run the suite.")
    keys = {r["key"] for r in T.check_integrity(store)["emphatic_capitals"]}
    assert "plain-sk" in keys
