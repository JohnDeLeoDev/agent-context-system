"""Shared hook selection for every harness projection."""

import json
import os
from pathlib import Path


def enabled(command: str) -> bool:
    root = Path(
        os.environ.get("AGENT_CONTEXT_STORE") or Path(__file__).resolve().parents[2]
    )
    try:
        config = json.loads((root / "global/starter-config.json").read_text())
    except FileNotFoundError:
        config = {}
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Invalid starter-config.json; refusing to rewrite hook configuration."
        ) from error
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("disabled_hooks", []), list)
        or any(not isinstance(item, str) for item in config.get("disabled_hooks", []))
    ):
        raise RuntimeError(
            "Invalid starter-config.json; disabled_hooks must be a list of names."
        )
    return Path(command.split()[0]).stem not in config.get("disabled_hooks", [])


def manifest(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("hooks", {}), dict)
        or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("script", name), str)
            for name, entry in value.get("hooks", {}).items()
        )
    ):
        raise RuntimeError(
            "Invalid hooks-manifest.json; refusing to rewrite hook configuration."
        )
    return {
        **value,
        "hooks": {
            name: entry
            for name, entry in value.get("hooks", {}).items()
            if enabled(entry.get("script", name))
        },
    }
