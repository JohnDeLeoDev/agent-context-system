'Machine identity — a stable per-host UUID.\n\nLabels which machine a session runs on. The agent-context store is shared across\nmachines via git; `get_session_context` reports the current machine. Identity is\nderived from the OS hardware UUID, so it needs no stored state:\n\n  - macOS: IOPlatformUUID (ioreg)\n  - Linux: /etc/machine-id (or dbus fallback)\n  - override: AGENT_CONTEXT_MACHINE_ID env var\n  - last resort: host-<hostname>'
import os
import platform
import socket
import subprocess
from pathlib import Path

_CACHED_UUID: str | None = None


def get_machine_uuid() -> str:
    "Return this machine's stable identity string (cached per process)."
    global _CACHED_UUID
    if _CACHED_UUID:
        return _CACHED_UUID
    env = os.environ.get("AGENT_CONTEXT_MACHINE_ID")
    if env and env.strip():
        _CACHED_UUID = env.strip()
        return _CACHED_UUID
    
    
    _CACHED_UUID = _os_uuid() or f"host-{socket.gethostname()}"
    return _CACHED_UUID


def get_chezmoi_machine_id() -> str | None:
    'get chezmoi machine id.'
    import re
    cfg = Path.home() / ".config" / "chezmoi" / "chezmoi.toml"
    try:
        for line in cfg.read_text().splitlines():
            m = re.match(r'\s*machine_id\s*=\s*"([^"]+)"', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def _os_uuid() -> str | None:
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split('"')[-2]
        elif sysname == "Linux":
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                fp = Path(p)
                if fp.is_file():
                    val = fp.read_text().strip()
                    if val:
                        return val
        elif sysname == "Windows":
            
            
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                val = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
                if val:
                    return val
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None
