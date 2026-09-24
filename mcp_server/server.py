import argparse
import atexit
import json
from pathlib import Path
from typing import Any, List, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .controller import ControllerError, ProxyLoaderController

INSTRUCTIONS = (
    "ProxyLoader routes traffic through upstream SOCKS5/SOCKS4/HTTP proxies via a local SOCKS5 "
    "server. Proxies and app targets are addressed by zero-based index; call list_proxies or "
    "list_apps first to see current indices, since removing or sorting proxies renumbers them. "
    "Passwords are never returned. All changes are saved to ProxyLoader's state file."
)


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2)


def build_server(controller: ProxyLoaderController) -> MCPServer:
    mcp = MCPServer(name="proxyloader", instructions=INSTRUCTIONS)

    def call(fn, *args, **kwargs) -> str:
        try:
            return _dump(fn(*args, **kwargs))
        except ControllerError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(description="Show server state: running or not, bind port, mode, active proxy or chain, health-check settings.")
    def get_status() -> str:
        return call(controller.status)

    @mcp.tool(description="List every configured upstream proxy with its index, active/chain position, and latest health result.")
    def list_proxies() -> str:
        return call(controller.list_proxies)

    @mcp.tool(description=(
        "Add proxies from pasted text, one per line. Accepts socks5://user:pass@host:port, "
        "host:port:user:pass, user:pass@host:port, or scheme,host,port,user,password. "
        "Set replace=true to wipe the current list first."
    ))
    def import_proxies(text: str, replace: bool = False) -> str:
        return call(controller.import_text, text, replace)

    @mcp.tool(description="Import proxies from a .txt or .csv file on disk. Set replace=true to wipe the current list first.")
    def import_proxy_file(path: str, replace: bool = False) -> str:
        return call(controller.import_file, path, replace)

    @mcp.tool(description="Remove proxies by index. Remaining proxies are renumbered afterwards.")
    def remove_proxies(indices: List[int]) -> str:
        return call(controller.remove_proxies, indices)

    @mcp.tool(description="Delete every configured proxy and clear the active selection and chain.")
    def clear_proxies() -> str:
        return call(controller.clear_proxies)

    @mcp.tool(description="Route through a single proxy by index. Clears any existing chain.")
    def set_active_proxy(index: int) -> str:
        return call(controller.set_active, index)

    @mcp.tool(description="Route through several proxies hop by hop, in the order given (first index is the first hop).")
    def set_proxy_chain(indices: List[int]) -> str:
        return call(controller.set_chain, indices)

    @mcp.tool(description="Drop the multi-hop chain and go back to routing through the single active proxy.")
    def clear_proxy_chain() -> str:
        return call(controller.clear_chain)

    @mcp.tool(description="Reorder proxies fastest-first using the latest health results. Indices change afterwards.")
    def sort_proxies_by_latency() -> str:
        return call(controller.sort_by_latency)

    @mcp.tool(description="Probe every proxy right now with a real handshake and return the updated list with latency and status.")
    def check_proxy_health(timeout_seconds: float = 20.0) -> str:
        return call(controller.check_health, timeout_seconds)

    @mcp.tool(description="Turn periodic background health checks on or off, optionally changing the interval (seconds, minimum 5).")
    def configure_health_checks(enabled: bool, interval_seconds: Optional[float] = None) -> str:
        return call(controller.configure_health_checks, enabled, interval_seconds)

    @mcp.tool(description=(
        "Start the local SOCKS5 server that forwards through the active proxy or chain. "
        "Optionally change the port. system_proxy=true also points macOS system-wide settings at it "
        "(macOS only, shows an admin password prompt)."
    ))
    def start_proxy_server(port: Optional[int] = None, system_proxy: bool = False) -> str:
        return call(controller.start_server, port, system_proxy)

    @mcp.tool(description="Stop the local SOCKS5 server, reverting the system-wide proxy setting if this server turned it on.")
    def stop_proxy_server() -> str:
        return call(controller.stop_server)

    @mcp.tool(description="Turn the macOS system-wide SOCKS proxy setting on or off (macOS only, shows an admin password prompt).")
    def set_system_proxy(enabled: bool) -> str:
        return call(controller.set_system_proxy, enabled)

    @mcp.tool(description="List app targets that can be launched with their traffic routed through a proxy.")
    def list_apps() -> str:
        return call(controller.list_apps)

    @mcp.tool(description=(
        "Register an app to launch through a proxy. path is a macOS .app bundle or an executable. "
        "proxy_index pins it to one proxy; leave it empty to use whatever proxy is active at launch."
    ))
    def add_app(path: str, display_name: Optional[str] = None, proxy_index: Optional[int] = None) -> str:
        return call(controller.add_app, path, display_name, proxy_index)

    @mcp.tool(description="Remove an app target by index.")
    def remove_app(index: int) -> str:
        return call(controller.remove_app, index)

    @mcp.tool(description="Pin an app target to a proxy index, or pass null to follow the active proxy.")
    def pin_app_to_proxy(index: int, proxy_index: Optional[int] = None) -> str:
        return call(controller.pin_app, index, proxy_index)

    @mcp.tool(description="Launch app targets (all of them, or just the given indices) with their traffic routed through their proxy.")
    def launch_apps(indices: Optional[List[int]] = None) -> str:
        return call(controller.launch_apps, indices)

    @mcp.tool(description="Stop the per-app local proxy servers. Apps that were already launched keep running.")
    def stop_app_proxies() -> str:
        return call(controller.stop_app_proxies)

    @mcp.tool(description="Delete leftover throwaway Firefox profile folders that no running Firefox is using.")
    def cleanup_firefox_profiles() -> str:
        return call(controller.cleanup_firefox_profiles)

    @mcp.resource("proxyloader://status", description="Current ProxyLoader status.", mime_type="application/json")
    def status_resource() -> str:
        return _dump(controller.status())

    @mcp.resource("proxyloader://proxies", description="Configured proxies with health.", mime_type="application/json")
    def proxies_resource() -> str:
        return _dump(controller.list_proxies())

    return mcp


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="proxyloader-mcp", description="ProxyLoader MCP server")
    parser.add_argument("--state", type=Path, default=None, help="path to state.json (defaults to the app's own)")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio for local MCP clients, streamable-http to serve over HTTP",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (streamable-http only)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port (streamable-http only)")
    args = parser.parse_args(argv)

    controller = ProxyLoaderController(state_path=args.state)
    atexit.register(controller.shutdown)
    if controller.health_auto_enabled:
        controller.manager.start_health_checks(controller.health_interval)

    server = build_server(controller)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
