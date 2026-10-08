'Context graph T3, review finding: a path-derived note colliding with a real entity.\n\nRename a note in Obsidian, then create a new note under the old name, and two files\nclaim one natural key: the renamed file by its frontmatter, the new note by its path.\nThe frontmatter entity must win in either walk order, and the new note must be\nreported under its own path, naming the file that holds the key. Before the fix the\noutcome depended on `os.walk` order: the note was dropped with a misleading "no uuid"\nerror, or stayed in the index where no read could reach it.'
from pathlib import Path

import pytest

from agent_context import fstools as T


def _setup(store, kind, how):
    'Returns (key, real_path, note_path) after building the collision on disk.'
    root = Path(store.root)
    if kind == "doc":
        T.upsert_doc(store, "a.md", body="original")
        a, b = root / "global/docs/a.md", root / "global/docs/b.md"
        old_key, new_key, key_line = "a.md", "b.md", 'path: "{}"'
    else:
        T.upsert_memory(store, "alpha", "reference", "d", "original")
        a, b = root / "global/memory/alpha.md", root / "global/memory/beta.md"
        old_key, new_key, key_line = "alpha", "beta", 'slug: "{}"'
    if how == "renamed":
        
        a.rename(b)
        a.write_text("a new note\n")
        return old_key, b, a
    
    text = a.read_text()
    a.write_text(text.replace(key_line.format(old_key), key_line.format(new_key), 1))
    b.write_text("a new note\n")
    return new_key, a, b


@pytest.mark.parametrize("real_first", [True, False], ids=["real-first", "note-first"])
@pytest.mark.parametrize("how", ["renamed", "rekeyed"])
@pytest.mark.parametrize("kind", ["doc", "memory"])
def test_a_new_note_never_displaces_the_entity_holding_its_key(store, kind, how, real_first):
    root = Path(store.root)
    key, real, note = _setup(store, kind, how)
    store.entities, store.by_key, store.load_errors = {}, {}, []
    for p in ([real, note] if real_first else [note, real]):
        store._load_file(str(p), str(p.relative_to(root)), p.name)

    e = store.get(kind, key)
    assert e is not None
    assert e["body"] == "original"
    assert e["_path"] == str(real)
    assert not [x for x in store.entities.values() if x.get("_path") == str(note)]
    note_rel, real_rel = str(note.relative_to(root)), str(real.relative_to(root))
    errors = [x["error"] for x in store.load_errors if x["path"] == note_rel]
    assert len(errors) == 1, store.load_errors
    assert real_rel in errors[0]
    assert "uuid" not in errors[0].split(real_rel)[0].lower(), errors[0]
    assert T.check_integrity(store)["path_derived_identity"] == []
