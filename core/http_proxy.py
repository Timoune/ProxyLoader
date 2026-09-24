import asyncio
import base64
from typing import Optional


class HttpProxyError(Exception):
    pass


async def http_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    lines = [f"CONNECT {target_host}:{target_port} HTTP/1.1", f"Host: {target_host}:{target_port}"]

    if username:
        token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
        lines.append(f"Proxy-Authorization: Basic {token}")

    lines.append("Proxy-Connection: Keep-Alive")
    request = "\r\n".join(lines) + "\r\n\r\n"

    writer.write(request.encode())
    await writer.drain()

    status_line = await reader.readline()
    if not status_line:
        raise HttpProxyError("proxy closed connection")

    parts = status_line.decode(errors="replace").split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit() or not (200 <= int(parts[1]) < 300):
        raise HttpProxyError(f"proxy CONNECT failed: {status_line!r}")

    while True:
        header_line = await reader.readline()
        if header_line in (b"\r\n", b"\n", b""):
            break
