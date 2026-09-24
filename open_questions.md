# Open Questions / Known Caveats

## Firefox profile cleanup

`core/firefox_profile.py` creates a throwaway profile directory per Firefox launch (since Firefox has no env-var/CLI way to set a proxy, unlike Chromium's `--proxy-server`). The caller is responsible for cleaning it up once the launched process exits.

`sweep_stale_profiles()` exists as a fallback: it scans the OS temp dir for *any* `proxyloader-firefox-*` directory (not just ones this process launched) and removes any that don't look like a running Firefox still has open. It checks for Firefox's `lock` file (a symlink) as the "in use" signal — this is best-effort, not a guarantee, so the sweep deliberately errs toward *not* deleting a profile that might still be live rather than risk yanking one out from under a running session.

## Platform support

Only macOS has a UI (`Mac/` is built on PyObjC/AppKit). `main.py` exits immediately with a message on any other platform. `core/` itself is platform-agnostic (asyncio-based), so a Linux/Windows UI could reuse it — just needs its own windowing layer plus an equivalent to `Mac/system_proxy.py` for system-wide proxy toggling.

## UDP associate scope

`ProxyLoaderServer._relay_udp_associate` only supports UDP relay when the active chain is exactly one SOCKS5 hop (`core/server.py`). Multi-hop UDP relaying isn't implemented — chained UDP would need each hop to tunnel the other's UDP relay endpoint, which none of the upstream protocol modules currently do.

## Health check probe target

`core/health_check.py` probes proxies by handshaking a CONNECT to `example.com:443` through each one. This confirms the proxy accepts connections and completes a real handshake, but doesn't verify the proxy can actually reach the open internet from wherever it's hosted, or measure anything beyond a single TLS-port connect's latency.

## MCP server

- **Running alongside the GUI.** `python -m mcp_server` owns its own `ProxyManager`; it doesn't talk to a running ProxyLoader window. Both read/write the same `state.json`, and whichever saves last wins (the GUI saves on close and after most actions). They also can't both bind the same local SOCKS port. Should the GUI host the MCP server in-process instead (e.g. streamable-http on localhost) so the LLM and the window share live state? Until then, point the MCP server at a separate file with `--state` if you want to use both at once.
- **Passwords.** Tool output hides passwords (`has_password` only), but `state.json` still stores them in plain text like it always has. Moving credentials to the macOS Keychain would be the real fix.
- **System proxy.** `set_system_proxy` / `start_proxy_server(system_proxy=true)` triggers the macOS admin password prompt via `osascript`, so a human still has to approve it. On other platforms these tools return an error.
- **No auth on HTTP transport.** `--transport streamable-http` binds to `127.0.0.1` by default with no authentication. Binding it to a public interface would let anyone on the network reconfigure your proxies.
- **`ProxyManager.probe_and_wait`.** Added so `check_proxy_health` can return fresh results. If a background health probe is already in flight it returns immediately and the tool reports whatever results exist at that moment (often `checking`).
- **App launches.** Launched apps are not killed on `stop_app_proxies` or MCP server exit, same as the GUI. Firefox throwaway profiles are deleted when the launched Firefox exits, as long as the MCP server is still running; otherwise use `cleanup_firefox_profiles`.
- **Dependency.** `mcp` lives in `requirements-mcp.txt` (not `requirements.txt`) so the py2app bundle doesn't grow; it requires the 2.x SDK (`MCPServer`, formerly `FastMCP`).
