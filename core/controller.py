import platform
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import firefox_profile, launcher, persistence
from .app_target import AppTarget
from .manager import ProxyManager, ProxyManagerError
from .secrets_store import SecretStore, store_for

MIN_HEALTH_INTERVAL = 5.0


class ControllerError(Exception):
    pass


class ProxyLoaderController:
    def __init__(self, state_path: Optional[Path] = None, manager: Optional[ProxyManager] = None):
        self.state_path = Path(state_path) if state_path else persistence.default_state_path()
        self.manager = manager or ProxyManager()
        self._lock = threading.RLock()

        loaded = persistence.load_state(self.manager, self.state_path)
        self.app_targets: List[AppTarget] = loaded.app_targets
        self.mode: str = loaded.mode
        self.last_import_path: Optional[str] = loaded.last_import_path
        self.health_interval: float = loaded.health_interval
        self.health_auto_enabled: bool = loaded.health_auto_enabled
        self.mcp_enabled: bool = loaded.mcp_enabled
        self.mcp_port: int = loaded.mcp_port
        self.undecryptable_passwords: int = loaded.undecryptable_passwords
        self.system_proxy_enabled = False
        self.selected_mode_active = False

        self._firefox_launches: List[tuple] = []
        self._firefox_launches_lock = threading.Lock()
        self._dispatch: Optional[Callable[[Callable[[], Any]], Any]] = None
        self._on_change: Optional[Callable[[], None]] = None

        self.manager.set_health_interval(self.health_interval)
        if loaded.needs_resave:
            self.try_save()

    @property
    def secrets(self) -> SecretStore:
        return store_for(self.state_path.parent)

    def set_ui_hooks(
        self,
        dispatch: Optional[Callable[[Callable[[], Any]], Any]],
        on_change: Optional[Callable[[], None]],
    ) -> None:
        self._dispatch = dispatch
        self._on_change = on_change

    def invoke(self, fn: Callable, *args, mutates: bool = True, **kwargs):
        def _call():
            try:
                return fn(*args, **kwargs)
            finally:
                if mutates and self._on_change is not None:
                    self._on_change()

        if self._dispatch is None or threading.current_thread() is threading.main_thread():
            return _call()
        return self._dispatch(_call)

    def save(self) -> None:
        persistence.save_state(
            self.manager,
            self.app_targets,
            self.mode,
            self.last_import_path,
            self.health_interval,
            self.health_auto_enabled,
            path=self.state_path,
            mcp_enabled=self.mcp_enabled,
            mcp_port=self.mcp_port,
        )

    def try_save(self) -> None:
        try:
            self.save()
        except OSError:
            pass

    def _require_proxy_index(self, index: int) -> None:
        if not (0 <= index < len(self.manager.entries)):
            raise ControllerError(
                f"proxy index {index} is out of range (have {len(self.manager.entries)} proxies)"
            )

    def _require_app_index(self, index: int) -> None:
        if not (0 <= index < len(self.app_targets)):
            raise ControllerError(
                f"app index {index} is out of range (have {len(self.app_targets)} apps)"
            )

    def _proxy_dict(self, index: int) -> Dict[str, Any]:
        entry = self.manager.entries[index]
        health = self.manager.health.get(index)
        chain = self.manager.chain
        return {
            "index": index,
            "label": entry.label(),
            "scheme": entry.scheme,
            "host": entry.host,
            "port": entry.port,
            "username": entry.username,
            "has_password": bool(entry.password),
            "active": (index in chain) if chain else (index == self.manager.active_index),
            "chain_position": chain.index(index) if index in chain else None,
            "health": {
                "status": health.status,
                "latency_ms": round(health.latency_ms, 1) if health.latency_ms is not None else None,
                "checked_at": health.checked_at,
                "error": health.error,
            } if health else None,
        }

    def _app_dict(self, index: int) -> Dict[str, Any]:
        target = self.app_targets[index]
        pinned = target.proxy_index
        return {
            "index": index,
            "display_name": target.display_name,
            "executable_path": target.executable_path,
            "proxy_index": pinned,
            "proxy_label": self.manager.entries[pinned].label()
            if pinned is not None and 0 <= pinned < len(self.manager.entries)
            else None,
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            chain = self.manager.active_chain()
            return {
                "server_running": self.manager.running,
                "bind_host": self.manager.bind_host,
                "bind_port": self.manager.bind_port,
                "mode": self.mode,
                "system_proxy_enabled": self.system_proxy_enabled,
                "proxy_count": len(self.manager.entries),
                "active_index": self.manager.active_index,
                "chain": list(self.manager.chain),
                "route": " -> ".join(entry.label() for entry in chain) or None,
                "health_checks_running": self.manager.health_checks_running,
                "health_interval": self.health_interval,
                "health_auto_enabled": self.health_auto_enabled,
                "app_target_count": len(self.app_targets),
                "app_proxies_running": self.selected_mode_active,
                "undecryptable_passwords": self.undecryptable_passwords,
                "last_error": self.manager.last_error,
                "state_path": str(self.state_path),
                "platform": platform.system(),
            }

    def list_proxies(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._proxy_dict(i) for i in range(len(self.manager.entries))]

    def import_text(self, text: str, replace: bool = False) -> Dict[str, Any]:
        with self._lock:
            result = self.manager.import_text(text, replace=replace)
            if replace:
                self._unpin_all_apps()
            self.save()
            return {"added": result.added, "duplicates": result.duplicates, "total": len(self.manager.entries)}

    def import_file(self, path: str, replace: bool = False) -> Dict[str, Any]:
        with self._lock:
            resolved = str(Path(path).expanduser())
            try:
                result = self.manager.import_file(resolved, replace=replace)
            except (OSError, UnicodeDecodeError) as exc:
                raise ControllerError(f"couldn't read {resolved}: {exc}") from exc
            if replace:
                self._unpin_all_apps()
            self.last_import_path = resolved
            self.save()
            return {"added": result.added, "duplicates": result.duplicates, "total": len(self.manager.entries)}

    def _unpin_all_apps(self) -> None:
        for target in self.app_targets:
            target.proxy_index = None

    def remove_proxies(self, indices: List[int]) -> Dict[str, Any]:
        with self._lock:
            for index in indices:
                self._require_proxy_index(index)
            removed = 0
            for index in sorted(set(indices), reverse=True):
                if not self.manager.remove_at(index):
                    continue
                removed += 1
                for target in self.app_targets:
                    if target.proxy_index is None:
                        continue
                    if target.proxy_index == index:
                        target.proxy_index = None
                    elif target.proxy_index > index:
                        target.proxy_index -= 1
            self.save()
            return {"removed": removed, "total": len(self.manager.entries)}

    def clear_proxies(self) -> Dict[str, Any]:
        with self._lock:
            self.manager.clear()
            self._unpin_all_apps()
            self.save()
            return {"total": 0}

    def set_active(self, index: int) -> Dict[str, Any]:
        with self._lock:
            self._require_proxy_index(index)
            self.manager.set_active(index)
            self.save()
            return self.status()

    def set_chain(self, indices: List[int]) -> Dict[str, Any]:
        with self._lock:
            if not indices:
                raise ControllerError("a chain needs at least one proxy index")
            for index in indices:
                self._require_proxy_index(index)
            self.manager.set_chain(indices)
            self.save()
            return self.status()

    def clear_chain(self) -> Dict[str, Any]:
        with self._lock:
            self.manager.clear_chain()
            self.save()
            return self.status()

    def sort_by_latency(self) -> List[Dict[str, Any]]:
        with self._lock:
            mapping = self.manager.sort_by_latency()
            for target in self.app_targets:
                if target.proxy_index is not None:
                    target.proxy_index = mapping.get(target.proxy_index)
            self.save()
            return self.list_proxies()

    def start_server(self, port: Optional[int] = None, system_proxy: bool = False) -> Dict[str, Any]:
        with self._lock:
            if port is not None and not (0 < port < 65536):
                raise ControllerError(f"port {port} is out of range")
            if self.selected_mode_active:
                raise ControllerError("per-app proxies are running; call stop_app_proxies first")
            if not self.manager.active_chain():
                raise ControllerError("no active proxy; import proxies and pick one first")
            try:
                self.manager.start(bind_port=port)
            except ProxyManagerError as exc:
                raise ControllerError(f"couldn't start the proxy server: {exc}") from exc
            self.mode = "all"
            self.save()
            if system_proxy:
                self._set_system_proxy(True)
            return self.status()

    def stop_server(self) -> Dict[str, Any]:
        with self._lock:
            self.manager.stop()
            if self.system_proxy_enabled:
                self._set_system_proxy(False)
            return self.status()

    def set_system_proxy(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            if enabled and not self.manager.running:
                raise ControllerError("start the proxy server before enabling the system proxy")
            self._set_system_proxy(enabled)
            return self.status()

    def _set_system_proxy(self, enabled: bool) -> None:
        if platform.system() != "Darwin":
            raise ControllerError("system-wide proxy toggling is only supported on macOS")
        from Mac import system_proxy

        try:
            if enabled:
                system_proxy.enable_system_socks_proxy(self.manager.bind_host, self.manager.bind_port)
            else:
                system_proxy.disable_system_socks_proxy()
        except Exception as exc:
            raise ControllerError(f"couldn't change the system proxy setting: {exc}") from exc
        self.system_proxy_enabled = enabled

    def check_health(self, timeout: float = 20.0) -> List[Dict[str, Any]]:
        if not self.manager.entries:
            return []
        try:
            self.manager.probe_and_wait(timeout=timeout)
        except ProxyManagerError as exc:
            raise ControllerError(str(exc)) from exc
        return self.invoke(self.list_proxies, mutates=False)

    def configure_health_checks(self, enabled: bool, interval: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            if interval is not None:
                if interval < MIN_HEALTH_INTERVAL:
                    raise ControllerError(f"interval must be at least {MIN_HEALTH_INTERVAL:.0f} seconds")
                self.health_interval = float(interval)
                self.manager.set_health_interval(self.health_interval)
            self.health_auto_enabled = enabled
            if enabled:
                self.manager.start_health_checks(self.health_interval)
            else:
                self.manager.stop_health_checks()
            self.save()
            return self.status()

    def list_apps(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._app_dict(i) for i in range(len(self.app_targets))]

    def add_app(self, path: str, display_name: Optional[str] = None, proxy_index: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            if proxy_index is not None:
                self._require_proxy_index(proxy_index)
            resolved = Path(path).expanduser()
            target = None
            if resolved.suffix == ".app":
                from Mac import app_bundle

                target = app_bundle.resolve_app_bundle(str(resolved))
                if target is None:
                    raise ControllerError(f"{resolved} doesn't look like a valid .app bundle")
            elif resolved.is_file():
                target = AppTarget(resolved.name, str(resolved))
            else:
                raise ControllerError(f"{resolved} isn't an executable file or .app bundle")
            if display_name:
                target.display_name = display_name
            target.proxy_index = proxy_index
            self.app_targets.append(target)
            self.save()
            return self._app_dict(len(self.app_targets) - 1)

    def remove_app(self, index: int) -> Dict[str, Any]:
        with self._lock:
            self._require_app_index(index)
            removed = self.app_targets.pop(index)
            self.save()
            return {"removed": removed.display_name, "remaining": len(self.app_targets)}

    def pin_app(self, index: int, proxy_index: Optional[int]) -> Dict[str, Any]:
        with self._lock:
            self._require_app_index(index)
            if proxy_index is not None:
                self._require_proxy_index(proxy_index)
            self.app_targets[index].proxy_index = proxy_index
            self.save()
            return self._app_dict(index)

    def launch_apps(self, indices: Optional[List[int]] = None) -> Dict[str, Any]:
        with self._lock:
            if self.manager.running:
                raise ControllerError("the all-processes proxy server is running; call stop_proxy_server first")
            chosen = list(range(len(self.app_targets))) if indices is None else indices
            for index in chosen:
                self._require_app_index(index)

            launched: List[Dict[str, Any]] = []
            failures: List[str] = []
            for app_index in chosen:
                target = self.app_targets[app_index]
                proxy_index = target.proxy_index if target.proxy_index is not None else self.manager.active_index
                if proxy_index is None:
                    failures.append(f"{target.display_name}: no pinned or active proxy")
                    continue
                entry = self.manager.entries[proxy_index]
                port = self.manager.start_pinned_server(f"proxy-{proxy_index}", [entry])
                if port is None:
                    failures.append(f"{target.display_name}: {self.manager.last_error or 'local server failed'}")
                    continue
                extra_args, profile_dir = self._extra_args(target, port)
                try:
                    process = launcher.launch_app_target(target, self.manager.bind_host, port, extra_args)
                except OSError as exc:
                    failures.append(f"{target.display_name}: {exc}")
                    if profile_dir:
                        shutil.rmtree(profile_dir, ignore_errors=True)
                    continue
                if profile_dir:
                    self._watch_profile(process, profile_dir)
                launched.append({
                    "app": target.display_name,
                    "pid": process.pid,
                    "proxy_index": proxy_index,
                    "local_port": port,
                })

            if launched:
                self.mode = "selected"
                self.selected_mode_active = True
                self.save()
            return {"launched": launched, "failures": failures}

    def stop_app_proxies(self) -> Dict[str, Any]:
        with self._lock:
            self.manager.stop_all_pinned_servers()
            self.selected_mode_active = False
            self.cleanup_finished_firefox_profiles()
            return self.status()

    def _extra_args(self, target: AppTarget, port: int):
        if platform.system() == "Darwin":
            from Mac import app_bundle

            return app_bundle.known_extra_args(target, self.manager.bind_host, port)
        return [], None

    def _watch_profile(self, process, profile_dir: str) -> None:
        with self._firefox_launches_lock:
            self._firefox_launches.append((process, profile_dir))

        def _wait_then_clean():
            process.wait()
            shutil.rmtree(profile_dir, ignore_errors=True)
            with self._firefox_launches_lock:
                if (process, profile_dir) in self._firefox_launches:
                    self._firefox_launches.remove((process, profile_dir))

        threading.Thread(target=_wait_then_clean, daemon=True).start()

    def cleanup_finished_firefox_profiles(self) -> List[str]:
        with self._firefox_launches_lock:
            still_running = []
            finished = []
            for process, profile_dir in self._firefox_launches:
                (finished if process.poll() is not None else still_running).append(
                    (process, profile_dir)
                )
            self._firefox_launches = still_running
        removed = []
        for _process, profile_dir in finished:
            shutil.rmtree(profile_dir, ignore_errors=True)
            removed.append(profile_dir)
        return removed

    def cleanup_firefox_profiles(self) -> Dict[str, Any]:
        removed = self.cleanup_finished_firefox_profiles()
        swept, skipped = firefox_profile.sweep_stale_profiles()
        removed.extend(path for path in swept if path not in removed)
        return {"removed": removed, "skipped_in_use": list(skipped)}

    def shutdown(self) -> None:
        with self._lock:
            try:
                self.manager.stop_health_checks()
                self.manager.stop_all_pinned_servers()
                self.manager.stop()
                if self.system_proxy_enabled:
                    self._set_system_proxy(False)
            except (ControllerError, ProxyManagerError):
                pass
            self.cleanup_finished_firefox_profiles()
            self.try_save()
