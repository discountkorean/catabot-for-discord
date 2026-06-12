"""JSON-file persistence for bot configuration and per-guild state.

Layout under ``data/``::

    config.toml            # static config (version, defaults)         [read-only]
    stock_state.json       # last-seen variant map, keyed by store URL
    bot_state.json         # misc cross-restart flags (restart message, etc.)
    <guild_id>/state.json  # per-guild stores, channels, subscriptions

All writes are plain ``json.dump`` — callers offload them to a thread when on
the event loop. Corrupt state files are renamed aside and treated as empty so a
single bad write can never wedge startup.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from typing import Any

from .runtime import (
    BOT_STATE_FILE,
    CONFIG_FILE,
    DATA_DIR,
    STATE_FILE,
)

log = logging.getLogger(__name__)

_config_cache: dict[str, Any] | None = None


def load_config() -> dict[str, Any]:
    """Return ``config.toml`` parsed once and cached for the process lifetime."""
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_FILE, "rb") as f:
            _config_cache = tomllib.load(f)
    return _config_cache


def bot_footer() -> str:
    """Standard embed footer, e.g. ``cata.ai v2.0.0``."""
    version = load_config().get("bot", {}).get("version", "1.0.0")
    return f"cata.ai v{version}"


def _load_json(path: str, default: Any) -> Any:
    """Load JSON from ``path``; on corruption rename it aside and return default."""
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001 — any parse error means "treat as empty"
        log.error(f"Corrupted {os.path.basename(path)} — resetting ({e})")
        os.rename(path, path + ".corrupt")
        return default


def _save_json(path: str, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_state() -> dict[str, Any]:
    """Last-seen variant map, keyed by store URL."""
    return _load_json(STATE_FILE, {})


def save_state(state: dict[str, Any]) -> None:
    _save_json(STATE_FILE, state)


def load_bot_state() -> dict[str, Any]:
    """Cross-restart flags (restart confirmation message, etc.)."""
    return _load_json(BOT_STATE_FILE, {})


def save_bot_state(data: dict[str, Any]) -> None:
    _save_json(BOT_STATE_FILE, data)


def _guild_dir(guild_id: int | str) -> str:
    return os.path.join(DATA_DIR, str(guild_id))


def _guild_file(guild_id: int | str) -> str:
    return os.path.join(_guild_dir(guild_id), "state.json")


def load_guild_state(guild_id: int | str) -> dict[str, Any]:
    return _load_json(
        _guild_file(guild_id),
        {"alert_channel_id": None, "extra_stores": {}, "notifications": {}},
    )


def save_guild_state(guild_id: int | str, data: dict[str, Any]) -> None:
    os.makedirs(_guild_dir(guild_id), exist_ok=True)
    _save_json(_guild_file(guild_id), data)


def load_all_guilds() -> dict[str, dict[str, Any]]:
    """Load every ``data/<guild_id>/state.json`` into a ``{guild_id: state}`` map."""
    guilds: dict[str, dict[str, Any]] = {}
    for entry in os.scandir(DATA_DIR):
        if entry.is_dir() and entry.name.isdigit():
            guilds[entry.name] = load_guild_state(entry.name)
    return guilds
