"C9b: package the relay's own code so a machine with no clone can install it.\n\n`build_relay_source(server_dir)` returns a gzip tarball of the `server/` package tree (pyproject\nand src, no tests, venv, git data or caches). The bytes depend only on file names and contents,\nnever on mtimes, owners or the gzip clock, so an unchanged tree gives an unchanged ETag."
import gzip
import hashlib
import io
import tarfile
from pathlib import Path


_SKIP_DIRS = frozenset({
    "tests", ".venv", ".git", ".pytest_cache", "__pycache__", ".ruff_cache", ".mypy_cache",
})
_SKIP_SUFFIXES = (".pyc", ".pyo")

_ROOTS = ("pyproject.toml", "uv.lock", "src")


def _files(server_dir: Path) -> list[Path]:
    found: list[Path] = []
    for name in _ROOTS:
        root = server_dir / name
        if root.is_file():
            found.append(root)
        elif root.is_dir():
            found.extend(
                path for path in root.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and not path.name.endswith(_SKIP_SUFFIXES)
                and not _SKIP_DIRS.intersection(path.relative_to(server_dir).parts)
            )
    return sorted(found, key=lambda path: path.relative_to(server_dir).as_posix())


def build_relay_source(server_dir: Path) -> bytes:
    'Deterministic tar.gz of the installable package. Raises FileNotFoundError when\n    `server_dir` has no pyproject.toml.'
    if not (server_dir / "pyproject.toml").is_file():
        raise FileNotFoundError(f"no pyproject.toml under {server_dir}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in _files(server_dir):
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(server_dir).as_posix())
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def etag_of(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'





_RELEASES: dict[Path, tuple[str, bytes]] = {}

RELEASE_HEADER = "X-Agent-Context-Release"


def snapshot_release(server_dir: Path) -> tuple[str, bytes]:
    '(etag, tarball) of the release this process serves for `server_dir`, built on the first\n    call and never again. The daemon calls it at boot. Raises FileNotFoundError like\n    build_relay_source.'
    key = server_dir.resolve()
    if key not in _RELEASES:
        body = build_relay_source(server_dir)
        _RELEASES[key] = (etag_of(body), body)
    return _RELEASES[key]


def served_etag() -> str | None:
    'The ETag of the release this process serves (the latest snapshot; a daemon takes one), or\n    None before a snapshot exists.'
    return next(reversed(_RELEASES.values()))[0] if _RELEASES else None


_current_cache: dict[Path, tuple[int, str]] = {}


def current_etag(server_dir: Path) -> str:
    'The ETag `/relay-source` serves for `server_dir`: the snapshot once there is one, else the\n    tree as it is on disk, rebuilt only when the newest mtime in the shipped tree changes. Raises\n    FileNotFoundError like build_relay_source.'
    frozen = _RELEASES.get(server_dir.resolve())
    if frozen is not None:
        return frozen[0]
    newest = max((path.stat().st_mtime_ns for path in _files(server_dir)), default=0)
    cached = _current_cache.get(server_dir)
    if cached is not None and cached[0] == newest:
        return cached[1]
    etag = etag_of(build_relay_source(server_dir))
    _current_cache[server_dir] = (newest, etag)
    return etag
