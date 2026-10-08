'Context graph T5 item 7, review finding: a carried key must not reorder the file.'
from pathlib import Path

from agent_context import fstools as T


def _bytes(path):
    return Path(path).read_bytes()


def test_omitting_links_on_a_repeat_upsert_leaves_the_file_byte_identical(store):
    T.upsert_memory(store, "n2", "reference", "d", "b\n")
    T.upsert_memory(store, "n1", "reference", "d", "body\n", links={"part_of": ["n2"]})
    e = store.get("memory", "n1")
    assert e is not None
    before = _bytes(e["_path"])
    T.upsert_memory(store, "n1", "reference", "d", "body\n")
    assert _bytes(e["_path"]) == before


def test_a_hand_added_key_keeps_its_place_through_a_no_op_upsert(store):
    T.upsert_memory(store, "m", "reference", "d", "b\n")
    e = store.get("memory", "m")
    assert e is not None
    p = Path(e["_path"])
    lines = p.read_text(encoding="utf-8").split("\n")
    lines.insert(2, 'status: "draft"')
    p.write_text("\n".join(lines), encoding="utf-8")
    store.reload()
    before = _bytes(p)
    T.upsert_memory(store, "m", "reference", "d", "b\n")
    assert _bytes(p) == before


def test_a_hand_added_sidecar_key_keeps_its_place_through_a_no_op_upsert(store):
    T.upsert_script(store, "x", "exit 0\n", description="d")
    e = store.get("script", "x")
    assert e is not None
    sidecar = Path(e["_path"] + ".meta.toml")
    sidecar.write_text('status = "draft"\n' + sidecar.read_text(encoding="utf-8"),
                       encoding="utf-8")
    store.reload()
    before = _bytes(sidecar)
    T.upsert_script(store, "x", "exit 0\n", description="d")
    assert _bytes(sidecar) == before
