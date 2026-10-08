
'Project resolution: cwd → project_id.'

import re
import subprocess
from pathlib import Path


def _git(argv: list[str]) -> subprocess.CompletedProcess:
    'Run a git read. stdin is the null device: the relay reads its own stdin on another thread,\n    and on Windows a child that inherits a pipe handle with a read pending on it hangs at startup.'
    return subprocess.run(argv, capture_output=True, text=True, timeout=5,
                          stdin=subprocess.DEVNULL)


def normalize_remote(url: str) -> str:
    'Normalize a git remote URL to canonical form.'
    url = url.strip()
    url = re.sub(r"\.git$", "", url)

    
    m = re.match(r"ssh://(?:[^@]+@)?([^/:]+)(?::\d+)?/(.+)", url)
    if m:
        return f"{m.group(1)}:{m.group(2).lstrip('/')}"

    
    m = None if "://" in url else re.match(r"(?:git@)?([^:]+):(.+)", url)
    if m and "/" in m.group(2):
        return f"{m.group(1)}:{m.group(2)}"

    
    m = re.match(r"https?://([^/]+)/(.+)", url)
    if m:
        return f"{m.group(1)}:{m.group(2)}"

    return url


def get_git_remote(path: str | Path) -> str | None:
    'Get the primary git remote URL for a directory.'
    path = Path(path)
    if not path.is_dir():
        return None

    for remote_name in ("origin", "remote"):
        try:
            result = _git(["git", "-C", str(path), "remote", "get-url", remote_name])
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    
    try:
        result = _git(["git", "-C", str(path), "remote"])
        if result.returncode == 0 and result.stdout.strip():
            first = result.stdout.strip().split("\n")[0]
            result2 = _git(["git", "-C", str(path), "remote", "get-url", first])
            if result2.returncode == 0 and result2.stdout.strip():
                return result2.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def get_git_remotes(path: str | Path) -> list[str]:
    "Every distinct remote URL configured at `path` — fetch AND push alike.\n\n    A mirrored repo pushes to several hosts under one remote name, and only the\n    fetch URL is reachable via `git remote get-url`. When the store's\n    canonical_remote happens to be one of the pushurl-only legs, matching on the\n    fetch URL alone resolves nothing (the store may record the mirror's push leg while the checkout fetches from another host). Ordered fetch-first so the\n    primary URL still wins a tie."
    path = Path(path)
    if not path.is_dir():
        return []
    try:
        result = _git(["git", "-C", str(path), "remote", "-v"])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []

    fetch, push = [], []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _, url, kind = parts[0], parts[1], parts[2]
        bucket = fetch if kind == "(fetch)" else push
        if url not in bucket:
            bucket.append(url)
    
    return list(dict.fromkeys(fetch + push))


def get_repo_root(path: str | Path) -> str | None:
    'Absolute worktree root for `path`, or None if not inside a (non-bare) git\n    worktree. Uses `--show-toplevel`, so a linked worktree resolves to its OWN\n    root and a bare/uninitialized repo yields None (guards marker writes).'
    try:
        result = _git(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_git_branch(path: str | Path) -> str | None:
    'Get the current branch name.'
    try:
        result = _git(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def resolve_project_from_cwd(conn, cwd: str, machine_id: int | None = None) -> dict | None:
    'Resolve a cwd to a project record using longest-prefix match on project_path.\n\n    Only paths belonging to ``machine_id`` (or machine-unscoped legacy rows, where\n    machine_id IS NULL) are considered, so the same DB resolves correctly on every\n    machine. When machine_id is None the filter is skipped (any path matches).'
    cwd = str(Path(cwd).resolve())
    mfilter = "" if machine_id is None else " AND (pp.machine_id = ? OR pp.machine_id IS NULL)"
    mparam = [] if machine_id is None else [machine_id]

    
    row = conn.execute(
        "SELECT p.* FROM project p "
        "JOIN project_path pp ON pp.project_id = p.id "
        "WHERE pp.local_path = ? AND p.expired_at IS NULL" + mfilter,
        (cwd, *mparam),
    ).fetchone()
    if row:
        return dict(row)

    
    rows = conn.execute(
        "SELECT p.*, pp.local_path FROM project p "
        "JOIN project_path pp ON pp.project_id = p.id "
        "WHERE ? LIKE pp.local_path || '%' AND p.expired_at IS NULL" + mfilter +
        " ORDER BY LENGTH(pp.local_path) DESC LIMIT 1",
        (cwd, *mparam),
    ).fetchone()
    if rows:
        return dict(rows)

    return None


def decode_project_dir_name(encoded: str) -> str:
    'Decode a ~/.claude/projects/ directory name back to a path.'
    return "/" + encoded.lstrip("-").replace("-", "/")
