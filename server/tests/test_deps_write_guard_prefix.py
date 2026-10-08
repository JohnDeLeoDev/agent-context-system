"global/deps/ is a protected store path: the fleet's declared dependency set changes only with\nthe protected-write scope, through a worktree, the deploy gate and a signed commit."
import pytest

from agent_context import write_guard


@pytest.mark.parametrize("rel", [
    "global/deps/manifest.toml", "global/deps/extra/roles.toml", "GLOBAL/Deps/manifest.toml",
])
def test_the_deps_tree_is_protected(rel):
    assert write_guard.classify_path(rel) is True


@pytest.mark.parametrize("rel", ["global/depsx/manifest.toml", "global/memory/deps.md"])
def test_a_near_miss_stays_free(rel):
    assert write_guard.classify_path(rel) is False


def test_the_deps_prefix_is_listed_beside_the_other_protected_trees():
    assert "global/deps/" in write_guard.PROTECTED_PREFIXES
    assert "global/node-tools/" in write_guard.PROTECTED_PREFIXES
