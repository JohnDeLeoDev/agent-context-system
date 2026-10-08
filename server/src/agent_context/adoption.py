'Adoption — has this machine actually PUT INTO USE the code it has synced?\n\nThe common shape is not "sync is broken". It is that **delivery is not adoption**,\nand every check the fleet had measured delivery. A file arriving on disk says nothing\nabout whether the process that needs it has read it, or whether the step that copies\nit into place has run since.\n\nWHAT THIS MODULE DOES, AND WHAT IT DELIBERATELY DOES NOT. It reads cheap local\nevidence that some other component already publishes about itself, and returns it for\n`fleet.publish` to carry into the store. It computes no verdicts — `fleet.problems`\nowns those, so every "is this worth telling a human" decision stays in one place with\none set of grace windows. It never raises: this is diagnostics riding inside the sync\nloop, and diagnostics must not be able to break the sync that carries them.\n\nIt also does not invent a second source of truth. Daemon staleness is read from\n`lspd.py --status`, which asks each daemon what build it is running — the same answer\nan operator gets, rather than a sidecar file that could disagree with it.'

import contextlib
import json
import shutil
import subprocess
import time
from pathlib import Path

from . import paths



STAMP = paths.health_dir() / "materialize-stamp.json"





LSPD_TIMEOUT_SECS = 20.0



LSPD = Path.home() / ".agent-context" / "global" / "scripts" / "lspd.py"


def projection(stamp_path=STAMP, now=None) -> dict:
    'When this machine last projected the store into its harnesses, and from what.\n\n    `age_secs` is resolved here rather than published raw so that a machine with a\n    skewed clock reports an age its own clock believes, which is the number its own\n    logs will agree with. `commit` names WHICH content was projected: a machine that\n    projected an hour ago from a commit fifty behind is a different fault from one\n    that has not projected in a week, and a timestamp alone cannot separate them.\n\n    A missing stamp is not an error and not a zero. It means "this machine has not\n    projected since the stamp was introduced", which on a rarely-used host is\n    indistinguishable from a very old projection and is reported as unknown.'
    t = time.time() if now is None else now
    with contextlib.suppress(OSError, ValueError):
        s = json.loads(Path(stamp_path).read_text())
        at = s.get("at")
        if isinstance(at, (int, float)):
            return {"at": int(at), "commit": s.get("commit"),
                    "age_secs": int(t - at), "findings": s.get("findings")}
    return {"at": None, "commit": None, "age_secs": None, "findings": None}


def stale_daemons(lspd_path=LSPD, timeout=LSPD_TIMEOUT_SECS) -> list[str] | None:
    'Keys of long-lived lspd daemons running superseded code, or None if unknown.\n\n    NONE AND [] MEAN DIFFERENT THINGS AND MUST NOT BE COLLAPSED. `[]` is "asked, and\n    every daemon is current"; `None` is "could not ask" — lspd is not installed here,\n    or the probe timed out. Reporting an unknown as "all clear" is the failure mode\n    this whole module exists to end, so the distinction is carried all the way to the\n    reader.\n\n    Parsed from `--status` rather than from a sidecar the daemons write, so there is\n    exactly one answer to "which build is that daemon on" and it is the one an\n    operator sees. The format it keys off is the `code=STALE` marker lspd prints on\n    its own second line for a stale daemon, and the `--upgrade --key <k>` remedy in\n    that same line — so the key is taken from lspd\'s own output rather than\n    re-derived from a socket filename here.'
    p = Path(lspd_path)
    if not p.exists():
        return None
    exe = shutil.which("python3") or "python3"
    try:
        r = subprocess.run([exe, str(p), "--status"], capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    keys = []
    for line in (r.stdout or "").splitlines():
        if "code=STALE" not in line:
            continue
        
        marker = "--upgrade --key "
        i = line.find(marker)
        if i == -1:
            continue
        key = line[i + len(marker):].strip().strip("`").split()[0]
        if key and key not in keys:
            keys.append(key)
    return keys


def probe(now=None) -> dict:
    'Everything this machine can cheaply say about what it has actually adopted.\n\n    Never raises. A probe that can throw would take down the sync-loop publish that\n    carries it, trading a diagnostic for the thing being diagnosed.'
    out = {"projection": {"at": None, "commit": None, "age_secs": None,
                          "findings": None},
           "stale_daemons": None}
    with contextlib.suppress(Exception):
        out["projection"] = projection(now=now)
    with contextlib.suppress(Exception):
        out["stale_daemons"] = stale_daemons()
    return out
