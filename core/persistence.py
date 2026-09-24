import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .app_target import AppTarget
from .health_check import DEFAULT_INTERVAL
from .manager import ProxyManager
from .proxy_entry import ProxyEntry
from .secrets_store import SecretStore, SecretStoreError, is_encrypted, store_for

STATE_FILENAME = "state.json"
STATE_VERSION = 2
DEFAULT_MCP_PORT = 8765


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


def _store_for_path(path: Path) -> SecretStore:
    return store_for(Path(path).parent)


def _entry_to_dict(entry: ProxyEntry, store: SecretStore) -> dict:
    data = asdict(entry)
    password = data.pop("password", None)
    data["password_enc"] = store.encrypt(password) if password else None
    return data


class _EntryLoadStats:
    def __init__(self):
        self.plaintext_found = False
        self.undecryptable = 0


def _entry_from_dict(data: dict, store: SecretStore, stats: _EntryLoadStats) -> Optional[ProxyEntry]:
    if not isinstance(data, dict):
        return None
    password = None
    encrypted = data.get("password_enc")
    if isinstance(encrypted, str) and is_encrypted(encrypted):
        try:
            password = store.decrypt(encrypted)
        except SecretStoreError:
            stats.undecryptable += 1
    elif isinstance(data.get("password"), str) and data.get("password"):
        password = data["password"]
        stats.plaintext_found = True
    try:
        entry = ProxyEntry(
            scheme=str(data["scheme"]),
            host=str(data["host"]),
            port=int(data["port"]),
            username=data.get("username"),
            password=password,
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
    store: SecretStore,
    mcp_enabled: bool = False,
    mcp_port: int = DEFAULT_MCP_PORT,
) -> dict:
    return {
        "version": STATE_VERSION,
        "entries": [_entry_to_dict(entry, store) for entry in manager.entries],
        "last_import_path": last_import_path,
        "active_index": manager.active_index,
        "chain": list(manager.chain),
        "mode": mode,
        "bind_port": manager.bind_port,
        "app_targets": [asdict(target) for target in app_targets],
        "health_interval": health_interval,
        "health_auto_enabled": health_auto_enabled,
        "mcp_enabled": mcp_enabled,
        "mcp_port": mcp_port,
    }


def save_state(
    manager: ProxyManager,
    app_targets: List[AppTarget],
    mode: str,
    last_import_path: Optional[str],
    health_interval: float,
    health_auto_enabled: bool,
    path: Optional[Path] = None,
    mcp_enabled: bool = False,
    mcp_port: int = DEFAULT_MCP_PORT,
) -> None:
    path = Path(path) if path else default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = build_state_dict(
            manager,
            app_targets,
            mode,
            last_import_path,
            health_interval,
            health_auto_enabled,
            _store_for_path(path),
            mcp_enabled,
            mcp_port,
        )
    except SecretStoreError as exc:
        raise OSError(f"couldn't encrypt proxy passwords: {exc}") from exc
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)


class LoadedState:
    def __init__(
        self,
        app_targets: List[AppTarget],
        mode: str,
        last_import_path: Optional[str],
        health_interval: float = DEFAULT_INTERVAL,
        health_auto_enabled: bool = True,
        mcp_enabled: bool = False,
        mcp_port: int = DEFAULT_MCP_PORT,
        needs_resave: bool = False,
        undecryptable_passwords: int = 0,
    ):
        self.app_targets = app_targets
        self.mode = mode
        self.last_import_path = last_import_path
        self.health_interval = health_interval
        self.health_auto_enabled = health_auto_enabled
        self.mcp_enabled = mcp_enabled
        self.mcp_port = mcp_port
        self.needs_resave = needs_resave
        self.undecryptable_passwords = undecryptable_passwords


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
    stats = _EntryLoadStats()
    store = _store_for_path(path)
    entries = [
        entry
        for entry in (_entry_from_dict(item, store, stats) for item in (raw_entries or []))
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

    if stats.undecryptable:
        backup = path.with_name(path.name + ".undecryptable-backup")
        if not backup.exists():
            try:
                shutil.copy2(path, backup)
            except OSError:
                pass

    mcp_enabled = data.get("mcp_enabled")
    if not isinstance(mcp_enabled, bool):
        mcp_enabled = False

    mcp_port = data.get("mcp_port")
    if not (isinstance(mcp_port, int) and 0 < mcp_port < 65536):
        mcp_port = DEFAULT_MCP_PORT

    return LoadedState(
        app_targets,
        mode,
        last_import_path,
        health_interval,
        health_auto_enabled,
        mcp_enabled,
        mcp_port,
        needs_resave=stats.plaintext_found,
        undecryptable_passwords=stats.undecryptable,
    )
