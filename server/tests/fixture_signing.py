"The daemon's self-redeploy gate runs this suite under the service's environment, where\nno signing agent is reachable (see conftest's `_hermetic_git`). A key FILE needs no\nagent: git hands it to `ssh-keygen -Y sign` directly. Keep the key outside the repo it\nsigns for, or `git add -A` in a fixture would commit it."
import subprocess
from pathlib import Path


def throwaway_key(directory) -> Path:
    key = Path(directory) / "fixture-signing-key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "fixture",
                        "-f", str(key)], check=True, capture_output=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    
    
    
    
    key.chmod(0o600)
    return key


def signing_config(key_dir) -> tuple[tuple[str, str], ...]:
    '`git config` (key, value) pairs that sign every commit with a throwaway key kept\n    in `key_dir`.'
    return (("gpg.format", "ssh"), ("gpg.ssh.program", "ssh-keygen"),
            ("user.signingkey", str(throwaway_key(key_dir))), ("commit.gpgsign", "true"))


def signing_config_beside(repo) -> tuple[tuple[str, str], ...]:
    'signing_config with the key in the directory that holds `repo`.'
    return signing_config(Path(repo).resolve().parent)
