'delete_entity records every file it removes in the write ledger (policy).\n\nDeleting store-autocommit, store-autopush and store-commit removed each body and its\n.meta.toml sidecar, but only the body went into the ledger, so the three sidecar removals\nwere reported as outside edits and never committed.'
import os

from agent_context import entities, generic


def test_a_script_delete_records_its_sidecar(store):
    entities.upsert_script(store, "gone", script_body="print(1)\n", language="py")
    body = os.path.join(store.root, "global", "scripts", "gone.py")
    assert os.path.exists(body + ".meta.toml")
    store.ledger.discard(store.ledger.snapshot())   
    generic.delete_entity(store, "script", "gone")
    held = store.ledger.snapshot()
    assert "global/scripts/gone.py" in held
    assert "global/scripts/gone.py.meta.toml" in held
    assert not os.path.exists(body + ".meta.toml")
