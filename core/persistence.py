import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .app_target import AppTarget
from .health_check import DEFAULT_INTERVAL
from .manager import ProxyManager
from .proxy_entry import ProxyEntry

STATE_FILENAME = "state.json"
STATE_VERSION = 1


def default_state_dir() -> Path:
    override = os.environ.get("PROXYLOADER_STATE_DIR")
    if override:
        return Path(override)
    home = Path.home()
    app_support = home / "Library" / "Application Support"
    if app_support.is_dir():
        return app_support / "proxyloader"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "proxyloader"
    return home / ".proxyloader"


def default_state_path() -> Path:
    return default_state_dir() / STATE_FILENAME


def _entry_from_dict(data: dict) -> Optional[ProxyEntry]:
    if not isinstance(data, dict):
        return None
    try:
        entry = ProxyEntry(
            scheme=str(data["scheme"]),
            host=str(data["host"]),
            port=int(data["port"]),
            username=data.get("username"),
            password=data.get("password"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return entry if entry.validate() else None


def _target_from_dict(data: dict) -> Optional[AppTarget]:
    if not isinstance(data, dict):
        return None
    display_name = data.get("display_name")
    executable_path = data.get("executable_path")
    if not display_name or not executable_path:
        return None
    proxy_index = data.get("proxy_index")
    if not isinstance(proxy_index, int):
        proxy_index = None
    return AppTarget(str(display_name), str(executable_path), proxy_index)


def build_state_dict(
    manager: ProxyManager,
    app_targets: List[AppTarget],
    mode: str,
    last_import_path: Optional[str],
    health_interval: float,
    health_auto_enabled: bool,
) -> dict:
    return {
        "version": STATE_VERSION,
        "entries": [asdict(entry) for entry in manager.entries],
        "last_import_path": last_import_path,
        "active_index": manager.active_index,
        "chain": list(manager.chain),
        "mode": mode,
        "bind_port": manager.bind_port,
        "app_targets": [asdict(target) for target in app_targets],
        "health_interval": health_interval,
        "health_auto_enabled": health_auto_enabled,
    }


def save_state(
    manager: ProxyManager,
    app_targets: List[AppTarget],
    mode: str,
    last_import_path: Optional[str],
    health_interval: float,
    health_auto_enabled: bool,
    path: Optional[Path] = None,
) -> None:
    path = Path(path) if path else default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_state_dict(
        manager, app_targets, mode, last_import_path, health_interval, health_auto_enabled
    )
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp_path, path)


class LoadedState:
    def __init__(
        self,
        app_targets: List[AppTarget],
        mode: str,
        last_import_path: Optional[str],
        health_interval: float = DEFAULT_INTERVAL,
        health_auto_enabled: bool = True,
    ):
        self.app_targets = app_targets
        self.mode = mode
        self.last_import_path = last_import_path
        self.health_interval = health_interval
        self.health_auto_enabled = health_auto_enabled


def load_state(manager: ProxyManager, path: Optional[Path] = None) -> LoadedState:
    """Populate `manager` (entries/active_index/chain/bind_port) from the saved
    state file and return the pieces the manager doesn't own (app targets,
    mode, last import path). No-ops manager-side (leaves it at its __init__
    defaults) if there's no state file or it can't be parsed.
    """
    path = Path(path) if path else default_state_path()
    if not path.exists():
        return LoadedState([], "all", None)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return LoadedState([], "all", None)

    if not isinstance(data, dict):
        return LoadedState([], "all", None)

    raw_entries = data.get("entries")
    entries = [
        entry
        for entry in (_entry_from_dict(item) for item in (raw_entries or []))
        if entry is not None
    ]
    manager.entries = entries

    bind_port = data.get("bind_port")
    if isinstance(bind_port, int) and 0 < bind_port < 65536:
        manager.bind_port = bind_port

    chain = data.get("chain") or []
    valid_chain = (
        isinstance(chain, list)
        and bool(chain)
        and all(isinstance(i, int) and 0 <= i < len(entries) for i in chain)
    )
    if valid_chain:
        manager.chain = list(chain)
        manager.active_index = manager.chain[0]
    else:
        manager.chain = []
        active_index = data.get("active_index")
        if isinstance(active_index, int) and 0 <= active_index < len(entries):
            manager.active_index = active_index
        elif entries:
            manager.active_index = 0
        else:
            manager.active_index = None

    raw_targets = data.get("app_targets")
    app_targets = [
        target
        for target in (_target_from_dict(item) for item in (raw_targets or []))
        if target is not None
    ]
    for target in app_targets:
        if target.proxy_index is not None and not (0 <= target.proxy_index < len(entries)):
            target.proxy_index = None

    mode = data.get("mode")
    if mode not in ("all", "selected"):
        mode = "all"

    last_import_path = data.get("last_import_path")
    if not isinstance(last_import_path, str):
        last_import_path = None

    health_interval = data.get("health_interval")
    if not (isinstance(health_interval, (int, float)) and health_interval >= 5):
        health_interval = DEFAULT_INTERVAL

    health_auto_enabled = data.get("health_auto_enabled")
    if not isinstance(health_auto_enabled, bool):
        health_auto_enabled = True

    return LoadedState(
        app_targets, mode, last_import_path, health_interval, health_auto_enabled
    )
