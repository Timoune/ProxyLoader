import asyncio
import hmac
import ipaddress
import json
import threading
import time
from typing import Callable, Optional

MCP_PATH = "/mcp"


class HttpHostError(Exception):
    pass


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class BearerAuthMiddleware:
    def __init__(self, app, token_provider: Callable[[], str]):
        self.app = app
        self.token_provider = token_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = f"Bearer {self.token_provider()}".encode("utf-8")
        provided = b""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                provided = value
                break
        if not hmac.compare_digest(provided, expected):
            body = json.dumps({"error": "unauthorized", "detail": "missing or wrong bearer token"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="proxyloader"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_http_app(mcp_server, host: str, token_provider: Callable[[], str], allowed_hosts=None):
    from mcp.server.transport_security import TransportSecuritySettings

    if allowed_hosts is None:
        if is_loopback(host):
            allowed_hosts = [f"{name}:*" for name in ("127.0.0.1", "localhost", "[::1]")]
        else:
            allowed_hosts = [f"{host}:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        allowed_origins=[f"http://{entry}" for entry in allowed_hosts],
    )
    app = mcp_server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=security,
        host=host,
    )
    return BearerAuthMiddleware(app, token_provider)


def http_url(host: str, port: int) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return f"http://{bracketed}:{port}{MCP_PATH}"


def client_config(url: str, token: str) -> dict:
    return {
        "mcpServers": {
            "proxyloader": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


class McpHttpHost:
    def __init__(self, mcp_server, token_provider: Callable[[], str], host: str = "127.0.0.1",
                 port: int = 8765, allow_remote: bool = False):
        if not is_loopback(host) and not allow_remote:
            raise HttpHostError(
                f"refusing to listen on non-loopback address {host}; pass --allow-remote to override"
            )
        self.mcp_server = mcp_server
        self.token_provider = token_provider
        self.host = host
        self.port = port
        self._uvicorn_server = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None

    @property
    def url(self) -> str:
        return http_url(self.host, self.port)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _make_server(self):
        import uvicorn

        app = build_http_app(self.mcp_server, self.host, self.token_provider)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning", lifespan="on")
        return uvicorn.Server(config)

    def serve_forever(self) -> None:
        self._uvicorn_server = self._make_server()
        self._uvicorn_server.run()

    def start(self, timeout: float = 10.0) -> None:
        if self.running:
            return
        self._error = None
        server = self._make_server()
        self._uvicorn_server = server

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(server.serve())
            except BaseException as exc:
                self._error = exc
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, name="proxyloader-mcp-http", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not server.started and self._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            self.stop()
            detail = f": {self._error}" if self._error and not isinstance(self._error, SystemExit) else " (is the port already in use?)"
            raise HttpHostError(f"couldn't start MCP HTTP server on {self.host}:{self.port}{detail}")

    def stop(self, timeout: float = 5.0) -> None:
        server = self._uvicorn_server
        if server is not None:
            server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._uvicorn_server = None
