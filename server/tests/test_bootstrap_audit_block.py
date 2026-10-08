'The open-observation block is measured, beside the per-scope footprint.'
import json

from agent_context import fstools as T
from agent_context.session import _audit_bootstrap


def test_audit_block_bytes_is_reported_and_matches_the_rendered_block(store):
    f = T.check_integrity(store)
    assert f["audit_block_bytes"] == len(json.dumps(_audit_bootstrap(store, None), default=str))
    before = f["audit_block_bytes"]
    T.add_audit_observation(store, "a high finding " + "w" * 80, "universal", None, "e",
                            severity="high")
    after = T.check_integrity(store)["audit_block_bytes"]
    assert after > before


def test_the_per_scope_sum_is_untouched_by_the_block(store):
    'The equality test_integrity pins must keep holding: the block is reported\n    beside est_bootstrap_bytes, never folded into it.'
    T.add_audit_observation(store, "a high finding " + "w" * 80, "universal", None, "e",
                            severity="high")
    fp = T.check_integrity(store)["bootstrap_footprint"]
    for f in fp.values():
        assert f["est_bootstrap_bytes"] == (f["always_instruction_bytes"] + f["memory_row_bytes"]
                                            + f["lazy_roster_bytes"])
