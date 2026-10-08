"Issue, list, revoke and rotate the daemon's table tokens.\n\n    python -m agent_context.token_admin issue  --id ID --machine-id M --scopes a,b [--apply]\n    python -m agent_context.token_admin list   [--json]\n    python -m agent_context.token_admin revoke --id ID [--apply]\n    python -m agent_context.token_admin rotate --id ID [--overlap-hours 24] [--apply]\n\nEvery command that changes something is a dry run until `--apply` is given. The plaintext token\nexists in this process and in one place afterwards: a 1Password item written through the\n`~/.local/bin/op` gateway. It goes to `op` on stdin as a JSON template, so it is never in an\nargument list, the environment, a file or a log, and this tool never prints it or its hash.\nThe table keeps only the hash. Nothing the gateway prints is echoed, only its exit status.\n\nIf anything fails after the table entry was made, the new entry is revoked, so no token exists\nthat nobody holds. Items are created or updated and never deleted: revoking a token leaves its\nitem for a person to archive. A machine has one live token at a time: use `rotate` to replace\nit. A token table that cannot be read is never overwritten."
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from . import token_table
from .flock import LOCK_EX, flock

DEFAULT_VAULT = "example.invalid"
ITEM_FIELD = "credential"
MIN_OVERLAP_HOURS = 1.0
MAX_OVERLAP_HOURS = 168.0
DEFAULT_OVERLAP_HOURS = 24.0
MAX_EXPIRES_DAYS = 3650.0
LOOPBACK = ["127.0.0.1", "::1"]
_NOT_FOUND = "isn't an item"
_ROTATED = re.compile(r"-\d{8}(-\d+)?$")
_MACHINE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


class AdminError(Exception):
    'A refusal with a message that is safe to print. `code` is the exit status.'

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def gateway_path() -> str:
    return os.environ.get("TOKEN_ADMIN_OP") or str(Path.home() / ".local" / "bin" / "op")


def vault() -> str:
    return os.environ.get("TOKEN_ADMIN_VAULT") or DEFAULT_VAULT


def item_title(machine_id: str) -> str:
    return f"agent-context token - {machine_id}"


def _iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat(timespec="seconds")  
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _table(args: argparse.Namespace) -> Path:
    return Path(args.table) if args.table else token_table.table_path()


def _entry(table: Path, token_id: str) -> dict | None:
    return next((e for e in token_table.list_entries(table) if e.get("id") == token_id), None)


def _live(entry: dict) -> bool:
    try:
        expires = entry.get("expires")
        return (not entry.get("revoked")
                and not (expires is not None and float(expires) <= time.time()))
    except (ValueError, TypeError):
        return False


@contextlib.contextmanager
def _admin_lock(table: Path):
    'One changing command at a time per table, so two issues cannot both pass the checks.'
    table.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(table) + ".admin.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flock(fd, LOCK_EX)
        yield
    finally:
        os.close(fd)





def _run_op(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    kw: dict = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run([gateway_path(), *argv], capture_output=True, text=True, **kw)
    except OSError as exc:
        raise AdminError(f"could not run the 1Password gateway ({exc.strerror or 'error'})",
                         1) from None


def _credential_field(body: object) -> dict:
    "The item's credential field, made concealed, from an item body of the shape `op` prints.\n    Raises AdminError when the body is not an item this tool can edit."
    if not isinstance(body, dict):
        raise AdminError("1Password returned an item this tool cannot edit", 1)
    fields = body.setdefault("fields", [])
    if not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
        raise AdminError("1Password returned an item this tool cannot edit", 1)
    field = next((f for f in fields if f.get("label") == ITEM_FIELD
                  or f.get("id") == ITEM_FIELD), None)
    if field is None:
        field = {"id": ITEM_FIELD, "label": ITEM_FIELD}
        fields.append(field)
    field["type"] = "CONCEALED"
    return field


def _store_secret(machine_id: str, secret: str) -> None:
    "Put `secret` in the machine's item: update it when it exists, create it when it does\n    not. At most two gateway calls. Raises AdminError (code 1) on any failure, with a message\n    that holds no gateway output."
    title = item_title(machine_id)
    where = ["--vault", vault()]
    got = _run_op(["item", "get", title, *where, "--format", "json"], None)
    if got.returncode == 0:
        try:
            body = json.loads(got.stdout)
        except ValueError:
            raise AdminError("1Password returned an item this tool could not read", 1) from None
        _credential_field(body)["value"] = secret
        done = _run_op(["item", "edit", title, *where], json.dumps(body))
    elif _NOT_FOUND in got.stderr:
        body = {"title": title, "category": "API_CREDENTIAL", "fields": []}
        _credential_field(body)["value"] = secret
        done = _run_op(["item", "create", *where], json.dumps(body))
    else:
        raise AdminError(f"1Password lookup failed (exit {got.returncode})", 1)
    if done.returncode != 0:
        raise AdminError(f"1Password write failed (exit {done.returncode})", 1)


def _issue_and_store(table: Path, machine_id: str, **kw) -> str:
    'Make the table entry, store its plaintext, and revoke the entry if anything fails\n    while storing. Returns the token id.'
    secret = token_table.issue(table, machine_id=machine_id, **kw)
    try:
        _store_secret(machine_id, secret)
    except BaseException:
        try:
            token_table.revoke(table, kw["id"])
        except Exception:
            print(f"token_admin: could not revoke {kw['id']!r}; revoke it now", file=sys.stderr)
        raise
    return kw["id"]





def _check_new_id(table: Path, token_id: str) -> None:
    if not token_table._ID.fullmatch(token_id):
        raise AdminError(f"token id {token_id!r} must be lowercase letters, digits and hyphens")
    if token_id in token_table.RESERVED_IDS and token_id != token_table.LS_LOCAL_ID:
        raise AdminError(f"token id {token_id!r} is reserved for the server's own callers")
    if _entry(table, token_id) is not None:
        raise AdminError(f"token id {token_id!r} already exists; revoke it and pick a new id")


def _check_machine(table: Path, machine_id: str) -> None:
    if not _MACHINE.fullmatch(machine_id):
        raise AdminError(f"machine id {machine_id!r} must be lowercase letters, digits and "
                         "hyphens (a fleet id such as laptop or m4)")
    for entry in token_table.list_entries(table):
        if entry.get("machine_id") == machine_id and _live(entry):
            raise AdminError(f"machine {machine_id!r} already has a live token "
                             f"({entry.get('id')!r}); use rotate to replace it")


def _addresses(raw: str | None, token_id: str) -> list[str] | None:
    found: list[str] | None = None
    if raw:
        found = []
        for part in raw.split(","):
            ip = token_table.normalize_ip(part, names=False)
            if ip is None or "/" in part:
                raise AdminError(f"allowed IP {part.strip()!r} is not a single IP address")
            found.append(ip)
        found = list(dict.fromkeys(found))
    if token_id == token_table.LS_LOCAL_ID:
        if found is not None and sorted(found) != sorted(LOOPBACK):
            raise AdminError("ls-local is bound to loopback (127.0.0.1 and ::1) and to nothing "
                             "else")
        return list(LOOPBACK)
    return found


def _scopes(raw: str) -> list[str]:
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    bad = [s for s in scopes if s not in token_table.SCOPES]
    if not scopes or bad:
        raise AdminError(f"scopes must come from: {', '.join(token_table.SCOPES)}"
                         + (f" (not {bad[0]!r})" if bad else ""))
    return scopes


def _expiry(days: float | None) -> float | None:
    if days is None:
        return None
    if not math.isfinite(days) or not 0 < days <= MAX_EXPIRES_DAYS:
        raise AdminError(f"--expires-days must be more than 0 and at most "
                         f"{MAX_EXPIRES_DAYS:g}")
    return time.time() + days * 86400


def _guard(args: argparse.Namespace, table: Path):
    return _admin_lock(table) if args.apply else contextlib.nullcontext()


def cmd_issue(args: argparse.Namespace) -> int:
    table = _table(args)
    with _guard(args, table):
        _check_new_id(table, args.id)
        _check_machine(table, args.machine_id)
        scopes = _scopes(args.scopes)
        ips = _addresses(args.allowed_ips, args.id)
        expires = _expiry(args.expires_days)
        plan = (f"issue token {args.id!r} for machine {args.machine_id!r}, scopes "
                f"{','.join(scopes)}, allowed_ips {ips or 'any'}, expires "
                f"{_iso(expires) or 'never'}; store it in 1Password vault {vault()!r}, item "
                f"{item_title(args.machine_id)!r}, field {ITEM_FIELD!r}")
        if not args.apply:
            print(f"dry run: would {plan}")
            return 0
        _issue_and_store(table, args.machine_id, id=args.id, machine_uuid=args.machine_uuid,
                         scopes=scopes, expires=expires, allowed_ips=ips,
                         allow_ls_local=args.id == token_table.LS_LOCAL_ID)
    print(f"issued token {args.id!r}; its plaintext is in item {item_title(args.machine_id)!r}")
    return 0


def _row(entry: dict) -> dict:
    def as_list(value: object) -> list:
        return list(value) if isinstance(value, list) else []

    return {"id": entry.get("id"), "machine_id": entry.get("machine_id"),
            "machine_uuid": entry.get("machine_uuid"), "scopes": as_list(entry.get("scopes")),
            "allowed_ips": as_list(entry.get("allowed_ips")),
            "created": _iso(entry.get("created")), "expires": _iso(entry.get("expires")),
            "revoked": bool(entry.get("revoked"))}


def cmd_list(args: argparse.Namespace) -> int:
    rows = [_row(e) for e in token_table.list_entries(_table(args))]
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print("\t".join([str(r["id"]), str(r["machine_id"] or "-"), ",".join(map(str, r["scopes"])),
                         ",".join(map(str, r["allowed_ips"])) or "any", r["created"] or "-",
                         r["expires"] or "never", "revoked" if r["revoked"] else "live"]))
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    table = _table(args)
    entry = _entry(table, args.id)
    if entry is None:
        raise AdminError(f"no token with id {args.id!r}")
    if entry.get("revoked"):
        print(f"token {args.id!r} is already revoked")
        return 0
    if not args.apply:
        print(f"dry run: would revoke token {args.id!r}; its 1Password item is left alone")
        return 0
    token_table.revoke(table, args.id)
    print(f"revoked token {args.id!r}; archive its 1Password item by hand")
    return 0


def _next_id(table: Path, token_id: str) -> str:
    base = _ROTATED.sub("", token_id)
    first = f"{base}-{time.strftime('%Y%m%d')}"
    candidate, counter = first, 1
    while _entry(table, candidate) is not None:
        counter += 1
        candidate = f"{first}-{counter}"
    return candidate


def cmd_rotate(args: argparse.Namespace) -> int:
    table = _table(args)
    hours = args.overlap_hours
    if not MIN_OVERLAP_HOURS <= hours <= MAX_OVERLAP_HOURS:
        raise AdminError(f"--overlap-hours must be between {MIN_OVERLAP_HOURS:g} and "
                         f"{MAX_OVERLAP_HOURS:g}")
    with _guard(args, table):
        old = _entry(table, args.id)
        if old is None or not _live(old):
            raise AdminError(f"no live token with id {args.id!r}")
        machine_id = old.get("machine_id")
        if not isinstance(machine_id, str) or not _MACHINE.fullmatch(machine_id):
            raise AdminError(f"token {args.id!r} names no usable machine, so it has no "
                             "1Password item")
        new_id = _next_id(table, args.id)
        if not args.apply:
            print(f"dry run: would issue {new_id!r} for machine {machine_id!r}, update item "
                  f"{item_title(machine_id)!r}, and let {args.id!r} keep working for "
                  f"{hours:g} hours")
            return 0
        ips = old.get("allowed_ips")
        _issue_and_store(table, machine_id, id=new_id, machine_uuid=old.get("machine_uuid"),
                         scopes=list(old.get("scopes") or []),
                         allowed_ips=list(ips) if isinstance(ips, list) and ips else None)
        try:
            ends = token_table.set_expiry(table, args.id, time.time() + hours * 3600)
        except Exception as exc:
            raise AdminError(
                f"rotation incomplete: {new_id!r} was issued and stored, but {args.id!r} was "
                f"not shortened ({exc}). Both work now: revoke {args.id!r} or set its expiry "
                "by hand", 1) from None
    print(f"rotated {args.id!r} to {new_id!r}; the old token works until {_iso(ends)}; "
          f"revoke it earlier with: revoke --id {args.id}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="token_admin", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, changes: bool = True) -> None:
        p.add_argument("--table", help="token table path (default: the daemon's table)")
        if changes:
            p.add_argument("--apply", action="store_true",
                           help="do it (the default is a dry run that changes nothing)")

    issue = sub.add_parser("issue", help="issue a token and store it in 1Password")
    issue.add_argument("--id", required=True)
    issue.add_argument("--machine-id", required=True)
    issue.add_argument("--machine-uuid")
    issue.add_argument("--scopes", required=True, help="comma separated")
    issue.add_argument("--allowed-ips", help="comma separated single IP addresses")
    issue.add_argument("--expires-days", type=float)
    common(issue)
    listing = sub.add_parser("list", help="show the table without secrets")
    listing.add_argument("--json", action="store_true")
    common(listing, changes=False)
    revoke = sub.add_parser("revoke", help="revoke a token now")
    revoke.add_argument("--id", required=True)
    common(revoke)
    rotate = sub.add_parser("rotate", help="issue a replacement and let the old token expire")
    rotate.add_argument("--id", required=True)
    rotate.add_argument("--overlap-hours", type=float, default=DEFAULT_OVERLAP_HOURS)
    common(rotate)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:                 
        return exc.code if isinstance(exc.code, int) else 2
    handler = {"issue": cmd_issue, "list": cmd_list, "revoke": cmd_revoke,
               "rotate": cmd_rotate}[args.command]
    try:
        return handler(args)
    except AdminError as exc:
        print(f"token_admin: {exc}", file=sys.stderr)
        return exc.code
    except ValueError as exc:
        print(f"token_admin: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
