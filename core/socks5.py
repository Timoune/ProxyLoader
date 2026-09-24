import asyncio
import socket
import struct
from typing import Optional, Tuple

SOCKS5_VERSION = 0x05
AUTH_NONE = 0x00
AUTH_USERPASS = 0x02
AUTH_NO_ACCEPTABLE = 0xFF
CMD_CONNECT = 0x01
CMD_UDP_ASSOCIATE = 0x03
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04
REPLY_OK = 0x00


class Socks5Error(Exception):
    pass


def encode_address(host: str, port: int) -> bytes:
    try:
        packed = socket.inet_aton(host)
        return bytes([ATYP_IPV4]) + packed + struct.pack(">H", port)
    except OSError:
        pass
    try:
        packed = socket.inet_pton(socket.AF_INET6, host)
        return bytes([ATYP_IPV6]) + packed + struct.pack(">H", port)
    except OSError:
        pass
    host_bytes = host.encode()
    return bytes([ATYP_DOMAIN, len(host_bytes)]) + host_bytes + struct.pack(">H", port)


async def read_address(reader: asyncio.StreamReader) -> Tuple[str, int]:
    atyp = (await reader.readexactly(1))[0]
    if atyp == ATYP_IPV4:
        raw = await reader.readexactly(4)
        host = socket.inet_ntoa(raw)
    elif atyp == ATYP_IPV6:
        raw = await reader.readexactly(16)
        host = socket.inet_ntop(socket.AF_INET6, raw)
    elif atyp == ATYP_DOMAIN:
        length = (await reader.readexactly(1))[0]
        raw = await reader.readexactly(length)
        host = raw.decode()
    else:
        raise Socks5Error("unknown address type in reply")
    port = struct.unpack(">H", await reader.readexactly(2))[0]
    return host, port


async def _negotiate_auth(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    username: Optional[str],
    password: Optional[str],
) -> None:
    methods = bytes([AUTH_NONE]) if not username else bytes([AUTH_NONE, AUTH_USERPASS])
    writer.write(bytes([SOCKS5_VERSION, len(methods)]) + methods)
    await writer.drain()

    response = await reader.readexactly(2)
    if response[0] != SOCKS5_VERSION:
        raise Socks5Error("unexpected server version")
    chosen = response[1]

    if chosen == AUTH_NO_ACCEPTABLE:
        raise Socks5Error("server rejected all auth methods")

    if chosen == AUTH_USERPASS:
        if not username:
            raise Socks5Error("server requires username/password")
        user_bytes = username.encode()
        pass_bytes = (password or "").encode()
        payload = bytes([0x01, len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes
        writer.write(payload)
        await writer.drain()
        auth_reply = await reader.readexactly(2)
        if auth_reply[1] != 0x00:
            raise Socks5Error("username/password authentication failed")


async def _send_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    command: int,
    target_host: str,
    target_port: int,
) -> Tuple[str, int]:
    request = bytes([SOCKS5_VERSION, command, 0x00]) + encode_address(target_host, target_port)
    writer.write(request)
    await writer.drain()

    header = await reader.readexactly(3)
    if header[0] != SOCKS5_VERSION:
        raise Socks5Error("unexpected reply version")
    if header[1] != REPLY_OK:
        raise Socks5Error(f"proxy refused request, reply code {header[1]}")

    return await read_address(reader)


async def socks5_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    await _negotiate_auth(reader, writer, username, password)
    await _send_request(reader, writer, CMD_CONNECT, target_host, target_port)


async def socks5_udp_associate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Tuple[str, int]:
    await _negotiate_auth(reader, writer, username, password)
    return await _send_request(reader, writer, CMD_UDP_ASSOCIATE, "0.0.0.0", 0)


def wrap_udp_datagram(target_host: str, target_port: int, payload: bytes) -> bytes:
    return b"\x00\x00\x00" + encode_address(target_host, target_port) + payload


def unwrap_udp_datagram(data: bytes) -> Tuple[str, int, bytes]:
    if len(data) < 4 or data[2] != 0x00:
        raise Socks5Error("malformed UDP relay datagram")
    atyp = data[3]
    offset = 4
    if atyp == ATYP_IPV4:
        host = socket.inet_ntoa(data[offset:offset + 4])
        offset += 4
    elif atyp == ATYP_IPV6:
        host = socket.inet_ntop(socket.AF_INET6, data[offset:offset + 16])
        offset += 16
    elif atyp == ATYP_DOMAIN:
        length = data[offset]
        offset += 1
        host = data[offset:offset + length].decode()
        offset += length
    else:
        raise Socks5Error("unknown address type in UDP datagram")
    port = struct.unpack(">H", data[offset:offset + 2])[0]
    offset += 2
    return host, port, data[offset:]
