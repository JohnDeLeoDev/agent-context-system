'test sync unsigned hold.'
import subprocess

import pytest

from agent_context.store import ContextStore


def _git(root, *args, **kw):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True, **kw)


@pytest.fixture
def signed_repo(tmp_path):
    'A store with an `origin` bare remote and working ssh commit signing.'
    key = tmp_path / "sign_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t",
                    "-f", str(key)], check=True)
    
    
    key.chmod(0o600)

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    root = tmp_path / "ctx"
    (root / "global").mkdir(parents=True)
    (root / "server" / "src").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("gpg.format", "ssh"), ("user.signingkey", str(key.with_suffix(".pub"))),
                 ("commit.gpgsign", "true")):
        _git(root, "config", k, v)
    (root / "global" / "base.md").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")
    return root, origin, ContextStore(root=str(root))


def _commit(root, name, body, *, signed):
    (root / "global" / name).write_text(body)
    _git(root, "add", "-A")
    _git(root, "-c", f"commit.gpgsign={'true' if signed else 'false'}",
         "commit", "-q", "-m", name)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _origin_tip(origin):
    return _git(origin, "rev-parse", "main").stdout.strip()


def test_unsigned_commit_below_the_tip_holds_the_whole_push(signed_repo):
    root, origin, st = signed_repo
    before = _origin_tip(origin)
    bad = _commit(root, "a.md", "unsigned\n", signed=False)
    _commit(root, "b.md", "signed on top\n", signed=True)

    res = st.sync(push=True)

    assert res["push"] is False
    assert res["unsigned_hold"] == bad
    
    assert _origin_tip(origin) == before


def test_unsigned_tip_is_re_signed_and_then_published(signed_repo):
    root, origin, st = signed_repo
    _commit(root, "a.md", "unsigned tip\n", signed=False)

    res = st.sync(push=True)

    assert "unsigned_hold" not in res
    assert res["push"] is True
    assert st._has_sig("HEAD")
    assert _origin_tip(origin) == _git(root, "rev-parse", "HEAD").stdout.strip()


def test_fully_signed_range_publishes_untouched(signed_repo):
    root, origin, st = signed_repo
    head = _commit(root, "a.md", "signed\n", signed=True)

    res = st.sync(push=True)

    assert "unsigned_hold" not in res
    assert res["push"] is True
    assert _origin_tip(origin) == head   
