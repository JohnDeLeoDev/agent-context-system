'Context graph T2: breaks found by the adversarial review (criterion 8).\n\n`1.0 in (1, 2)` is True, so a float depth passed the range check and then crashed in\nrange() with a TypeError the MCP layer does not catch. The documented answer to a depth\noutside 1..2 is an error dict, and a depth that is not an integer is outside it.'
import pytest

from agent_context import fstools as T


@pytest.mark.parametrize("depth", [1.0, 2.0, "1", None])
def test_a_depth_that_is_not_an_integer_is_an_error(store, depth):
    T.upsert_memory(store, "b", "reference", "about b", "b\n")
    T.upsert_memory(store, "a", "reference", "about a", "[[b]]\n")
    explore = getattr(T, "explore", None)
    assert explore is not None, "fstools has no explore"

    out = explore(store, "memory", "a", depth=depth)

    assert isinstance(out, dict) and "depth" in out.get("error", ""), out
