"""Install the starter at ~/.agent-context and render installed harnesses."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        help="Use an existing Python with the server dependencies instead of installing a venv.",
    )
    parser.add_argument(
        "--harness",
        choices=("claude", "codex", "pi", "opencode", "copilot"),
        action="append",
        help="Create this harness's config directory; existing harnesses are also rendered.",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Render config without installing dependencies or creating credentials.",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if root != Path.home() / ".agent-context":
        parser.error(
            "Extract or clone the starter into ~/.agent-context before installation."
        )
    if not args.render_only and not args.python:
        subprocess.run(
            ["uv", "sync", "--frozen", "--project", str(root / "server")], check=True
        )
    python = args.python or str(
        root
        / "server/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if args.render_only and not Path(python).exists():
        python = sys.executable
    env = {
        **os.environ,
        "AGENT_CONTEXT_STORE": str(root),
        "PYTHONPATH": str(root / "server/src"),
        "AGENT_CONTEXT_NO_SYNC": "1",
        "AGENT_CONTEXT_SELF_DEPLOY": "0",
        "AGENT_CONTEXT_COMMIT_DEBOUNCE_SECS": "0",
        "AGENT_CONTEXT_OFFLINE_RENDER": "1",
    }
    harnesses = args.harness or []
    folders = {
        "claude": ".claude",
        "codex": ".codex",
        "pi": ".pi/agent",
        "opencode": ".config/opencode",
        "copilot": ".copilot",
    }
    for harness in harnesses:
        (Path.home() / folders[harness]).mkdir(parents=True, exist_ok=True)
    if not any((Path.home() / folder).is_dir() for folder in folders.values()):
        parser.error("Choose at least one --harness, or install a harness first.")
    config = Path.home() / ".config/agent-context"
    env_file = config / "env"
    if not args.render_only and not env_file.exists():
        config.mkdir(parents=True, exist_ok=True)
        # The table stores a hash; only the owner's local env file holds the secret.
        code = 'from agent_context.token_table import issue; import sys; print(issue(sys.argv[1], id="owner", scopes=["read","entity-write","protected-write"], allowed_ips=["127.0.0.1","::1"]))'
        token = subprocess.run(
            [python, "-c", code, str(config / "tokens.json")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        port = env.get("AGENT_CONTEXT_PORT", "8765")
        descriptor = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(
                f"AGENT_CONTEXT_HOST=127.0.0.1\nAGENT_CONTEXT_PORT={port}\nAGENT_CONTEXT_TOKEN={token}\n"
            )
    manifest = root / "global/mcp-servers.json"
    data = json.loads(manifest.read_text())
    data["servers"]["agent-context"].update(
        command=python, args=[str(root / "start.py"), "--shared"]
    )
    manifest.write_text(json.dumps(data, indent=2) + "\n")
    scripts = root / "global/scripts"
    if (Path.home() / ".claude").is_dir():
        subprocess.run(
            [python, str(scripts / "home-settings-sync.py")], env=env, check=True
        )
    subprocess.run(
        [python, str(scripts / "harness-materialize.py")], env=env, check=True
    )
    print("Installed hook wiring and context configuration for detected harnesses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
