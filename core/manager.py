import asyncio
import concurrent.futures
import threading
from typing import Callable, Dict, List, NamedTuple, Optional

from .health_check import (
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    STATUS_CHECKING,
    STATUS_DOWN,
    STATUS_UP,
    HealthStatus,
    probe_entries,
)
from .proxy_entry import ProxyEntry, load_proxy_file, parse_block
from .server import ProxyLoaderServer


class ProxyManagerError(Exception):
    pass


class ImportResult(NamedTuple):
    added: int
    duplicates: int


class ProxyManager:
    def __init__(self, bind_host: str = "127.0.0.1", bind_port: int = 1080):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.entries: List[ProxyEntry] = []
        self.active_index: Optional[int] = None
        self.chain: List[int] = []
        self.last_error: Optional[str] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()

        self._global_server: Optional[ProxyLoaderServer] = None
        self._pinned_servers: Dict[str, ProxyLoaderServer] = {}
        self._on_status_change: Optional[Callable[[bool], None]] = None

        self.health: Dict[int, HealthStatus] = {}
        self._health_task: Optional[concurrent.futures.Future] = None
        self._health_interval: float = DEFAULT_INTERVAL
        self._on_health_change: Optional[Callable[[], None]] = None
        self._probe_in_flight: bool = False

    def set_status_callback(self, callback: Callable[[bool], None]) -> None:
        self._on_status_change = callback

    def set_health_callback(self, callback: Callable[[], None]) -> None:
        self._on_health_change = callback

    def import_file(self, path: str, replace: bool = False) -> ImportResult:
        new_entries = load_proxy_file(path)
        return self._apply_import(new_entries, replace)

    def import_text(self, text: str, replace: bool = False) -> ImportResult:
        new_entries = parse_block(text)
        return self._apply_import(new_entries, replace)

    def _apply_import(self, new_entries: List[ProxyEntry], replace: bool) -> ImportResult:
        if replace:
            self.clear()

        seen_keys = {entry.dedupe_key() for entry in self.entries}
        to_add: List[ProxyEntry] = []
        duplicates = 0
        for entry in new_entries:
            key = entry.dedupe_key()
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)
            to_add.append(entry)

        self.entries.extend(to_add)
        if self.active_index is None and self.entries:
            self.active_index = 0
        return ImportResult(added=len(to_add), duplicates=duplicates)

    def clear(self) -> None:
        self.entries = []
        self.active_index = None
        self.chain = []
        self.health = {}

    def set_active(self, index: int) -> None:
        if 0 <= index < len(self.entries):
            self.active_index = index
            self.chain = []

    def active_entry(self) -> Optional[ProxyEntry]:
        return self.entry_at(self.active_index)

    def entry_at(self, index: Optional[int]) -> Optional[ProxyEntry]:
        if index is None or not (0 <= index < len(self.entries)):
            return None
        return self.entries[index]

    def set_chain(self, indices: List[int]) -> bool:
        if not indices or any(not (0 <= i < len(self.entries)) for i in indices):
            return False
        self.chain = list(indices)
        self.active_index = self.chain[0]
        return True

    def clear_chain(self) -> None:
        self.chain = []

    def remove_at(self, index: int) -> bool:
        if not (0 <= index < len(self.entries)):
            return False
        del self.entries[index]

        def shift(i: int) -> Optional[int]:
            if i == index:
                return None
            return i - 1 if i > index else i

        self.chain = [ni for ni in (shift(i) for i in self.chain) if ni is not None]
        if self.active_index is not None:
            self.active_index = shift(self.active_index)
            if self.active_index is None:
                self.active_index = 0 if self.entries else None

        self.health = {
            ni: status
            for i, status in self.health.items()
            if (ni := shift(i)) is not None
        }
        return True

    def remove_many(self, indices: List[int]) -> int:
        removed = 0
        for index in sorted(set(indices), reverse=True):
            if self.remove_at(index):
                removed += 1
        return removed

    def sort_by_latency(self) -> Dict[int, int]:
        def sort_key(item):
            index, _ = item
            status = self.health.get(index)
            if status is None:
                return (2, 0.0)
            if status.status == STATUS_UP and status.latency_ms is not None:
                return (0, status.latency_ms)
            if status.status == STATUS_DOWN:
                return (1, 0.0)
            return (2, 0.0)

        indexed = sorted(enumerate(self.entries), key=sort_key)
        old_to_new = {old: new for new, (old, _) in enumerate(indexed)}

        self.entries = [entry for _, entry in indexed]
        if self.active_index is not None:
            self.active_index = old_to_new.get(self.active_index)
        self.chain = [old_to_new[i] for i in self.chain if i in old_to_new]
        self.health = {
            old_to_new[old]: status
            for old, status in self.health.items()
            if old in old_to_new
        }
        return old_to_new

    def active_chain(self) -> List[ProxyEntry]:
        if self.chain:
            chain_entries = [self.entry_at(i) for i in self.chain]
            return [entry for entry in chain_entries if entry is not None]
        entry = self.active_entry()
        return [entry] if entry is not None else []

    @property
    def loop_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _ensure_loop(self) -> None:
        if self.loop_running:
            return

        self._loop_ready.clear()

        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=run_loop, daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5)

    def _stop_loop_if_idle(self) -> None:
        if (
            self._global_server is None
            and not self._pinned_servers
            and self._health_task is None
            and self._loop is not None
        ):
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._thread = None
            self._loop = None

    def _run_blocking(self, coro, timeout: float = 5):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ProxyManagerError("timed out waiting for the proxy server to respond") from exc
        except OSError as exc:
            raise ProxyManagerError(str(exc)) from exc

    @property
    def running(self) -> bool:
        return self._global_server is not None and self._global_server.running

    def start(self, bind_port: Optional[int] = None) -> None:
        if self.running:
            return
        if bind_port is not None:
            self.bind_port = bind_port
        self._ensure_loop()

        async def _start():
            server = ProxyLoaderServer(self.active_chain, self.bind_host, self.bind_port)
            await server.start()
            return server

        try:
            self._global_server = self._run_blocking(_start())
        except ProxyManagerError as exc:
            self.last_error = str(exc)
            self._stop_loop_if_idle()
            raise
        self.last_error = None
        self._notify(True)

    def stop(self) -> None:
        if not self.running or self._loop is None:
            return
        server = self._global_server
        try:
            self._run_blocking(server.stop())
        except ProxyManagerError as exc:
            self.last_error = str(exc)
        self._global_server = None
        self._notify(False)
        self._stop_loop_if_idle()

    def start_pinned_server(self, key: str, chain: List[ProxyEntry], port: int = 0) -> Optional[int]:
        self._ensure_loop()
        if self._loop is None:
            self.last_error = "background event loop failed to start"
            return None

        existing = self._pinned_servers.get(key)
        if existing is not None:
            return existing.bound_port

        async def _start():
            server = ProxyLoaderServer(lambda: chain, self.bind_host, port)
            await server.start()
            return server

        try:
            server = self._run_blocking(_start())
        except ProxyManagerError as exc:
            self.last_error = str(exc)
            self._stop_loop_if_idle()
            return None
        self.last_error = None
        self._pinned_servers[key] = server
        return server.bound_port

    def stop_pinned_server(self, key: str) -> None:
        server = self._pinned_servers.pop(key, None)
        if server is None or self._loop is None:
            return
        try:
            self._run_blocking(server.stop())
        except ProxyManagerError as exc:
            self.last_error = str(exc)
        self._stop_loop_if_idle()

    def stop_all_pinned_servers(self) -> None:
        for key in list(self._pinned_servers.keys()):
            self.stop_pinned_server(key)

    def _notify(self, is_running: bool) -> None:
        if self._on_status_change:
            self._on_status_change(is_running)

    @property
    def health_checks_running(self) -> bool:
        return self._health_task is not None

    @property
    def health_interval(self) -> float:
        return self._health_interval

    def set_health_interval(self, interval: float) -> None:
        self._health_interval = interval

    def start_health_checks(self, interval: Optional[float] = None) -> None:
        if self.health_checks_running:
            return
        if interval is not None:
            self._health_interval = interval
        self._ensure_loop()
        self._health_task = asyncio.run_coroutine_threadsafe(
            self._health_loop(), self._loop
        )

    def stop_health_checks(self) -> None:
        task = self._health_task
        if task is None:
            return
        task.cancel()
        self._health_task = None
        self._stop_loop_if_idle()

    def probe_now(self) -> None:
        self._ensure_loop()
        asyncio.run_coroutine_threadsafe(self._probe_once(), self._loop)

    def probe_and_wait(self, timeout: float = DEFAULT_TIMEOUT * 3) -> None:
        self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._probe_once(), self._loop)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ProxyManagerError("timed out waiting for health checks to finish") from exc
        finally:
            self._stop_loop_if_idle()

    async def _health_loop(self) -> None:
        try:
            while True:
                await self._probe_once()
                await asyncio.sleep(self._health_interval)
        except asyncio.CancelledError:
            pass

    async def _probe_once(self) -> None:
        if self._probe_in_flight:
            return
        self._probe_in_flight = True
        try:
            entries = list(self.entries)
            if not entries:
                return
            for index in range(len(entries)):
                self.health[index] = HealthStatus(status=STATUS_CHECKING)
            self._notify_health()
            results = await probe_entries(entries, timeout=DEFAULT_TIMEOUT)
            self.health.update(results)
            self._notify_health()
        finally:
            self._probe_in_flight = False

    def _notify_health(self) -> None:
        if self._on_health_change:
            self._on_health_change()
