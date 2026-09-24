import asyncio
import socket
import struct

SOCKS4_VERSION = 0x04
CMD_CONNECT = 0x01
REPLY_GRANTED = 0x5A


class Socks4Error(Exception):
    pass


async def socks4_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    username: str = "",
) -> None:
    try:
        packed_ip = socket.inet_aton(target_host)
        request = (
            bytes([SOCKS4_VERSION, CMD_CONNECT])
            + struct.pack(">H", target_port)
            + packed_ip
            + username.encode()
            + b"\x00"
        )
    except OSError:
        request = (
            bytes([SOCKS4_VERSION, CMD_CONNECT])
            + struct.pack(">H", target_port)
            + b"\x00\x00\x00\x01"
            + username.encode()
            + b"\x00"
            + target_host.encode()
            + b"\x00"
        )

    writer.write(request)
    await writer.drain()

    reply = await reader.readexactly(8)
    if reply[0] not in (0x00, SOCKS4_VERSION):
        raise Socks4Error("unexpected reply version")
    if reply[1] != REPLY_GRANTED:
        raise Socks4Error(f"proxy refused request, reply code {reply[1]}")
