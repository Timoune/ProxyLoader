import asyncio
import struct
from typing import List, Optional

from . import socks5, socks4, http_proxy
from .proxy_entry import ProxyEntry

LOCAL_SOCKS_VERSION = 0x05
LOCAL_AUTH_NONE = 0x00
LOCAL_CMD_CONNECT = 0x01
LOCAL_CMD_UDP_ASSOCIATE = 0x03
LOCAL_REPLY_OK = 0x00
LOCAL_REPLY_COMMAND_NOT_SUPPORTED = 0x07
LOCAL_REPLY_GENERAL_FAILURE = 0x01


class ProxyServerError(Exception):
    pass


class ProxyLoaderServer:
    def __init__(self, get_chain, bind_host: str = "127.0.0.1", bind_port: int = 1080):
        self._get_chain = get_chain
        self.bind_host = bind_host
        self.bind_port = bind_port
        self._server: Optional[asyncio.base_events.Server] = None
        self._udp_transports = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.bind_host, self.bind_port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for transport in self._udp_transports:
            transport.close()
        self._udp_transports.clear()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> Optional[int]:
        if self._server is None:
            return None
        sockets = self._server.sockets
        if not sockets:
            return None
        return sockets[0].getsockname()[1]

    async def _handle_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            await self._negotiate_local_auth(client_reader, client_writer)
            command, target_host, target_port = await self._read_local_request(client_reader)

            chain = self._get_chain()
            if not chain:
                await self._reply_local(client_writer, LOCAL_REPLY_GENERAL_FAILURE)
                return

            if command == LOCAL_CMD_CONNECT:
                await self._relay_connect(client_reader, client_writer, chain, target_host, target_port)
            elif command == LOCAL_CMD_UDP_ASSOCIATE:
                await self._relay_udp_associate(client_reader, client_writer, chain)
            else:
                await self._reply_local(client_writer, LOCAL_REPLY_COMMAND_NOT_SUPPORTED)
        except (asyncio.IncompleteReadError, ConnectionError, socks5.Socks5Error,
                socks4.Socks4Error, http_proxy.HttpProxyError, ProxyServerError):
            client_writer.close()
        finally:
            if not client_writer.is_closing():
                client_writer.close()

    async def _negotiate_local_auth(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readexactly(2)
        if header[0] != LOCAL_SOCKS_VERSION:
            raise ProxyServerError("client is not speaking SOCKS5")
        methods = await reader.readexactly(header[1])
        if LOCAL_AUTH_NONE not in methods:
            writer.write(bytes([LOCAL_SOCKS_VERSION, 0xFF]))
            await writer.drain()
            raise ProxyServerError("client offered no acceptable auth method")
        writer.write(bytes([LOCAL_SOCKS_VERSION, LOCAL_AUTH_NONE]))
        await writer.drain()

    async def _read_local_request(self, reader: asyncio.StreamReader):
        header = await reader.readexactly(3)
        command = header[1]
        host, port = await socks5.read_address(reader)
        return command, host, port

    async def _reply_local(self, writer: asyncio.StreamWriter, reply_code: int,
                            bound_host: str = "0.0.0.0", bound_port: int = 0) -> None:
        payload = bytes([LOCAL_SOCKS_VERSION, reply_code, 0x00]) + socks5.encode_address(bound_host, bound_port)
        writer.write(payload)
        await writer.drain()

    async def _open_upstream(self, entry: ProxyEntry):
        return await asyncio.open_connection(entry.host, entry.port)

    async def _hop_connect(self, upstream_reader, upstream_writer, entry: ProxyEntry,
                            next_host: str, next_port: int) -> None:
        if entry.scheme == "socks5":
            await socks5.socks5_connect(upstream_reader, upstream_writer, next_host, next_port,
                                         entry.username, entry.password)
        elif entry.scheme == "socks4":
            await socks4.socks4_connect(upstream_reader, upstream_writer, next_host, next_port,
                                         entry.username or "")
        elif entry.scheme == "http":
            await http_proxy.http_connect(upstream_reader, upstream_writer, next_host, next_port,
                                           entry.username, entry.password)
        else:
            raise ProxyServerError(f"unsupported scheme: {entry.scheme}")

    async def _relay_connect(self, client_reader, client_writer, chain: List[ProxyEntry],
                              target_host: str, target_port: int) -> None:
        try:
            upstream_reader, upstream_writer = await self._open_upstream(chain[0])
        except OSError:
            await self._reply_local(client_writer, LOCAL_REPLY_GENERAL_FAILURE)
            return

        try:
            for hop_index, entry in enumerate(chain):
                if hop_index < len(chain) - 1:
                    next_host, next_port = chain[hop_index + 1].host, chain[hop_index + 1].port
                else:
                    next_host, next_port = target_host, target_port
                await self._hop_connect(upstream_reader, upstream_writer, entry, next_host, next_port)
        except (socks5.Socks5Error, socks4.Socks4Error, http_proxy.HttpProxyError, OSError):
            upstream_writer.close()
            await self._reply_local(client_writer, LOCAL_REPLY_GENERAL_FAILURE)
            return

        await self._reply_local(client_writer, LOCAL_REPLY_OK)
        await self._pump_bidirectional(client_reader, client_writer, upstream_reader, upstream_writer)

    async def _pump_bidirectional(self, a_reader, a_writer, b_reader, b_writer) -> None:
        async def pump(src_reader: asyncio.StreamReader, dst_writer: asyncio.StreamWriter):
            try:
                while True:
                    chunk = await src_reader.read(65536)
                    if not chunk:
                        break
                    dst_writer.write(chunk)
                    await dst_writer.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                dst_writer.close()

        await asyncio.gather(
            pump(a_reader, b_writer),
            pump(b_reader, a_writer),
            return_exceptions=True,
        )

    async def _relay_udp_associate(self, client_reader, client_writer, chain: List[ProxyEntry]) -> None:
        if len(chain) != 1 or chain[0].scheme != "socks5":
            await self._reply_local(client_writer, LOCAL_REPLY_COMMAND_NOT_SUPPORTED)
            return
        entry = chain[0]

        try:
            upstream_reader, upstream_writer = await self._open_upstream(entry)
            relay_host, relay_port = await socks5.socks5_udp_associate(
                upstream_reader, upstream_writer, entry.username, entry.password
            )
        except (OSError, socks5.Socks5Error):
            await self._reply_local(client_writer, LOCAL_REPLY_GENERAL_FAILURE)
            return

        client_peer = client_writer.get_extra_info("peername")
        expected_client_host = client_peer[0] if client_peer else None

        loop = asyncio.get_running_loop()
        relay_protocol = _UdpRelayProtocol(relay_host, relay_port, expected_client_host)
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: relay_protocol, local_addr=("127.0.0.1", 0)
        )
        self._udp_transports.append(transport)

        local_sock_host, local_sock_port = transport.get_extra_info("sockname")
        await self._reply_local(client_writer, LOCAL_REPLY_OK, local_sock_host, local_sock_port)

        try:
            await client_reader.read()
        finally:
            transport.close()
            upstream_writer.close()


class _UdpRelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay_host: str, relay_port: int, expected_client_host: Optional[str] = None):
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.expected_client_host = expected_client_host
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.client_addr = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if addr[0] == self.relay_host:
            try:
                target_host, target_port, payload = socks5.unwrap_udp_datagram(data)
                if self.client_addr:
                    self.transport.sendto(payload, self.client_addr)
            except socks5.Socks5Error:
                pass
            return

        if self.expected_client_host is not None and addr[0] != self.expected_client_host:
            return

        self.client_addr = addr
        wrapped = socks5.wrap_udp_datagram(self.relay_host, self.relay_port, data)
        self.transport.sendto(wrapped, (self.relay_host, self.relay_port))
