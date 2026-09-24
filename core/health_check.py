import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from .http_proxy import HttpProxyError, http_connect
from .proxy_entry import ProxyEntry
from .socks4 import Socks4Error, socks4_connect
from .socks5 import Socks5Error, socks5_connect

STATUS_UNKNOWN = "unknown"
STATUS_CHECKING = "checking"
STATUS_UP = "up"
STATUS_DOWN = "down"

DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 30.0

PROBE_HOST = "example.com"
PROBE_PORT = 443


@dataclass
class HealthStatus:
    status: str = STATUS_UNKNOWN
    latency_ms: Optional[float] = None
    checked_at: Optional[float] = None
    error: Optional[str] = None

    def label(self) -> str:
        if self.status == STATUS_CHECKING:
            return "checking…"
        if self.status == STATUS_UP:
            return f"up · {self.latency_ms:.0f}ms" if self.latency_ms is not None else "up"
        if self.status == STATUS_DOWN:
            return f"down · {self.error}" if self.error else "down"
        return "—"


async def probe_entry(
    entry: ProxyEntry,
    timeout: float = DEFAULT_TIMEOUT,
    handshake: bool = True,
) -> HealthStatus:
    start = time.monotonic()
    reader = None
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(entry.host, entry.port), timeout=timeout
        )

        if handshake:
            remaining = max(timeout - (time.monotonic() - start), 0.1)
            if entry.scheme == "socks5":
                await asyncio.wait_for(
                    socks5_connect(reader, writer, PROBE_HOST, PROBE_PORT, entry.username, entry.password),
                    timeout=remaining,
                )
            elif entry.scheme == "socks4":
                await asyncio.wait_for(
                    socks4_connect(reader, writer, PROBE_HOST, PROBE_PORT, entry.username or ""),
                    timeout=remaining,
                )
            elif entry.scheme == "http":
                await asyncio.wait_for(
                    http_connect(reader, writer, PROBE_HOST, PROBE_PORT, entry.username, entry.password),
                    timeout=remaining,
                )

        latency_ms = (time.monotonic() - start) * 1000
        return HealthStatus(status=STATUS_UP, latency_ms=latency_ms, checked_at=time.time())
    except asyncio.TimeoutError:
        return HealthStatus(status=STATUS_DOWN, checked_at=time.time(), error="timed out")
    except (OSError, Socks5Error, Socks4Error, HttpProxyError) as exc:
        return HealthStatus(status=STATUS_DOWN, checked_at=time.time(), error=str(exc))
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def probe_entries(
    entries: list,
    timeout: float = DEFAULT_TIMEOUT,
    handshake: bool = True,
    concurrency: int = 20,
) -> dict:
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(index: int, entry: ProxyEntry):
        async with semaphore:
            status = await probe_entry(entry, timeout=timeout, handshake=handshake)
            return index, status

    results = await asyncio.gather(
        *(_bounded(i, entry) for i, entry in enumerate(entries))
    )
    return {index: status for index, status in results}
