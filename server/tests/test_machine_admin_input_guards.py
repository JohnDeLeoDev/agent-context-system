'machine_admin rejects a blank label and a non-bool sleeps flag before any write.\n\nFound in review: display_name="  " would have written a blank label onto a machine,\nand sleeps="false" reached bool("false"), which is True and records that the machine\nsleeps. Over MCP the schema stops the second; a direct caller had no guard.'
from agent_context import fstools as T


def _labels(store):
    return [m["display_name"] for m in T.list_machines(store)]


def test_blank_display_name_is_refused_and_the_label_is_kept(store):
    T.get_session_context(store, "/nowhere")
    T.machine_admin(store, "set_display_name", display_name="Keep")
    for blank in ("", "   ", "\t\n"):
        r = T.machine_admin(store, "set_display_name", display_name=blank)
        assert "error" in r and "display_name" in r["error"], (blank, r)
    assert _labels(store) == ["Keep"]


def test_non_bool_sleeps_is_refused(store):
    T.get_session_context(store, "/nowhere")
    for bad in ("false", "true", 0, 1, None):
        r = T.machine_admin(store, "set_sleeps", sleeps=bad)  
        assert "error" in r and "sleeps" in r["error"], (bad, r)
