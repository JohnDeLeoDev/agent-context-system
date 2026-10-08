"C9b: GET /relay-source serves the relay's own code as a tarball, so a machine with no clone can\ninstall the relay. Bearer-authenticated like every other custom route; the tarball holds only\nwhat `uv tool install` needs and is byte-stable for an unchanged tree."
import importlib
import io
import os
import sys
import tarfile
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

TOKEN = "s3cret-token-for-tests"
REAL_STORE_ROOT = Path(__file__).resolve().parents[2]
MakeClient = Callable[..., TestClient]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_store(root: Path) -> Path:
    'A store root whose server/ holds source plus everything the tarball must leave out.'
    server = root / "server"
    _write(server / "pyproject.toml", '[project]\nname = "agent-context"\nversion = "0.1.0"\n')
    _write(server / "src" / "agent_context" / "__init__.py", "")
    _write(server / "src" / "agent_context" / "server.py", "def main() -> int:\n    return 0\n")
    _write(server / "src" / "agent_context" / "__pycache__" / "server.cpython-314.pyc", "junk")
    _write(server / "src" / "agent_context" / "stray.pyc", "junk")
    _write(server / "tests" / "test_x.py", "def test_x() -> None: ...\n")
    _write(server / ".venv" / "bin" / "python", "junk")
    _write(server / ".pytest_cache" / "README.md", "junk")
    _write(server / ".git" / "config", "junk")
    return server


def _load_server(monkeypatch: pytest.MonkeyPatch, root: Path, *, token: str | None) -> ModuleType:
    if token is None:
        monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AGENT_CONTEXT_TOKEN", token)
    monkeypatch.delenv("AGENT_CONTEXT_ALLOWED_HOSTS", raising=False)
    from agent_context import server as srv

    srv = importlib.reload(srv)
    monkeypatch.setattr(srv, "_get_conn", lambda: SimpleNamespace(root=root))
    return srv


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[MakeClient]:
    stack: list[TestClient] = []

    def make(*, token: str | None = TOKEN, root: Path | None = None) -> TestClient:
        srv = _load_server(monkeypatch, root if root is not None else tmp_path, token=token)
        client = TestClient(srv.mcp.streamable_http_app(), base_url="http://127.0.0.1:8765")
        client.__enter__()
        stack.append(client)
        return client

    yield make
    for client in stack:
        client.__exit__(None, None, None)
    monkeypatch.delenv("AGENT_CONTEXT_TOKEN", raising=False)
    from agent_context import server as srv

    importlib.reload(srv)


def _names(body: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return tar.getnames()







def test_relay_source_without_token_is_401(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    assert make_client().get("/relay-source").status_code == 401


def test_relay_source_with_wrong_token_is_401(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    assert make_client().get("/relay-source", headers=_bearer("nope")).status_code == 401


def test_relay_source_with_correct_token_is_a_gzip_tar(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    response = make_client().get("/relay-source", headers=_bearer(TOKEN))
    assert response.status_code == 200
    assert "pyproject.toml" in _names(response.content)


def test_relay_source_is_open_when_no_token_configured(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    assert make_client(token=None).get("/relay-source").status_code == 200







def test_tarball_holds_the_package_source(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    names = _names(make_client().get("/relay-source", headers=_bearer(TOKEN)).content)
    assert "pyproject.toml" in names
    assert "src/agent_context/__init__.py" in names
    assert "src/agent_context/server.py" in names


@pytest.mark.parametrize(
    "excluded", ["tests", ".venv", ".git", ".pytest_cache", "__pycache__", ".pyc"]
)
def test_tarball_leaves_out_tests_venv_and_caches(
    make_client: MakeClient, tmp_path: Path, excluded: str
) -> None:
    _fake_store(tmp_path)
    names = _names(make_client().get("/relay-source", headers=_bearer(TOKEN)).content)
    assert not [n for n in names if excluded in n], names


def test_tarball_members_have_no_absolute_or_parent_paths(
    make_client: MakeClient, tmp_path: Path
) -> None:
    _fake_store(tmp_path)
    names = _names(make_client().get("/relay-source", headers=_bearer(TOKEN)).content)
    assert names
    assert all(not n.startswith("/") and ".." not in Path(n).parts for n in names)


def test_real_server_tree_yields_an_installable_package(make_client: MakeClient, tmp_path: Path) -> None:
    body = make_client(root=REAL_STORE_ROOT).get("/relay-source", headers=_bearer(TOKEN)).content
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        tar.extractall(tmp_path / "out", filter="data")
    out = tmp_path / "out"
    project = tomllib.loads((out / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["agent-context"] == "agent_context.server:main"
    assert "build-system" in project
    assert (out / "src" / "agent_context" / "server.py").is_file()
    assert (out / "src" / "agent_context" / "relay_materialize.py").is_file()
    assert not (out / "tests").exists()
    assert len(body) < 5_000_000







def test_same_tree_gives_identical_bytes_and_an_etag(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    client = make_client()
    first = client.get("/relay-source", headers=_bearer(TOKEN))
    second = client.get("/relay-source", headers=_bearer(TOKEN))
    assert first.content == second.content
    assert first.headers["etag"] == second.headers["etag"]
    assert first.headers["etag"].startswith('"')


def test_touching_a_file_without_changing_it_keeps_the_bytes(
    make_client: MakeClient, tmp_path: Path
) -> None:
    server = _fake_store(tmp_path)
    client = make_client()
    before = client.get("/relay-source", headers=_bearer(TOKEN))
    target = server / "src" / "agent_context" / "server.py"
    os.utime(target, (1_000_000_000, 1_000_000_000))
    after = client.get("/relay-source", headers=_bearer(TOKEN))
    assert before.status_code == after.status_code == 200
    assert before.content == after.content


def test_a_content_change_after_the_snapshot_does_not_change_the_release(
        make_client: MakeClient, tmp_path: Path) -> None:
    "policy/policy: the daemon serves the code it booted with. A tree that changed since (a\n    landing the gate has not passed yet) reaches relays only once the daemon exec's onto it."
    server = _fake_store(tmp_path)
    client = make_client()
    before = client.get("/relay-source", headers=_bearer(TOKEN)).headers["etag"]
    _write(server / "src" / "agent_context" / "server.py", "def main() -> int:\n    return 1\n")
    after = client.get("/relay-source", headers=_bearer(TOKEN)).headers["etag"]
    assert before == after


def test_matching_if_none_match_is_304_with_no_body(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    client = make_client()
    etag = client.get("/relay-source", headers=_bearer(TOKEN)).headers["etag"]
    response = client.get("/relay-source", headers={**_bearer(TOKEN), "If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""


def test_stale_if_none_match_returns_the_full_body(make_client: MakeClient, tmp_path: Path) -> None:
    _fake_store(tmp_path)
    response = make_client().get(
        "/relay-source", headers={**_bearer(TOKEN), "If-None-Match": '"stale"'}
    )
    assert response.status_code == 200
    assert response.content







def test_missing_server_dir_is_404_not_a_crash(make_client: MakeClient, tmp_path: Path) -> None:
    response = make_client().get("/relay-source", headers=_bearer(TOKEN))
    assert response.status_code == 404
    assert "error" in response.json()
