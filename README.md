# ProxyLoader

A macOS menu app for managing SOCKS5/SOCKS4/HTTP proxies and routing traffic through them — either system-wide or per-app.

## What it does

ProxyLoader runs a local SOCKS5 server on your machine (default `127.0.0.1:1080`). Anything that connects to that local server gets its traffic relayed through one or more upstream proxies you've configured. It supports:

- **Multiple proxy protocols upstream**: SOCKS5, SOCKS4, and HTTP (CONNECT) proxies
- **Proxy chaining**: route a connection through a sequence of proxies, hop by hop
- **Per-app or system-wide routing**: point the whole OS at the local proxy via `networksetup`, or launch a specific app with proxy env vars set (`ALL_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY`)
- **Firefox support**: since Firefox ignores proxy env vars, ProxyLoader spins up a throwaway Firefox profile with the proxy baked into its prefs
- **Health checks**: periodically probes each configured proxy (real protocol handshake, not just a TCP connect) and reports latency/status
- **UDP associate**: SOCKS5 UDP relay support for a single upstream hop
- **Persistence**: proxy list, active selection, app targets, and settings are saved to disk between runs

## Requirements

- macOS (the UI is built on PyObjC/AppKit — no Windows/Linux UI exists yet, `main.py` exits early on other platforms)
- Python 3.10+
- Dependencies in `requirements.txt` (`pyobjc-core`, `pyobjc-framework-Cocoa`)

## Installation

```bash
pip install -r requirements.txt
python main.py
```

To build a standalone `.app` bundle:

```bash
python setup.py py2app
```

## Project layout

```
main.py                  # Entry point — dispatches to Mac/app.py on Darwin
core/
  proxy_entry.py          # ProxyEntry dataclass + parsing (URL, CSV, host:port:user:pass, etc.)
  manager.py               # ProxyManager — owns entries, active/chain selection, server lifecycle, health loop
  server.py                 # ProxyLoaderServer — the local SOCKS5 listener + relay/chaining logic
  socks5.py / socks4.py / http_proxy.py   # Upstream proxy protocol handshakes
  health_check.py          # Async probes (real handshake) for proxy status/latency
  persistence.py            # Save/load state.json (entries, app targets, mode, settings)
  secrets_store.py          # Keychain-backed encryption key + MCP token; encrypts proxy passwords
  controller.py             # App-level operations shared by the window and the MCP server
  app_target.py             # AppTarget dataclass (an app + its optional pinned proxy)
  launcher.py               # Launches an app subprocess with proxy env vars set
  firefox_profile.py        # Throwaway Firefox profile generation + stale-profile cleanup
mcp_server/
  server.py                 # MCP tool/resource definitions + CLI (`python -m mcp_server`)
  http_host.py              # Authenticated HTTP transport, run in a background thread by the app
Mac/
  app.py                    # AppKit application bootstrap
  glass_window.py           # Main window UI controller
  system_proxy.py           # Toggles the macOS system-wide SOCKS proxy via networksetup
  app_bundle.py              # Helpers for the packaged .app
```

## How proxy chaining works

`ProxyManager.active_chain()` returns either a single active proxy or a multi-hop chain (set via `set_chain`). When a client connects to the local SOCKS5 server, `ProxyLoaderServer` opens a connection to the first hop, then issues a `CONNECT` (or protocol equivalent) through each subsequent hop, with the final hop connecting to the client's actual target. Once the chain is established, bytes are pumped bidirectionally between the client and the last hop.

## Proxy list format

Proxies can be imported from a file or pasted as text. Supported formats per line:

- URL form: `socks5://user:pass@host:port`
- `host:port:user:pass` or `host|port|user|pass` or whitespace-separated
- `user:pass@host:port`
- CSV: `scheme,host,port,user,password`

Lines starting with `#` or `//` are treated as comments.

## State storage

State is saved to `~/Library/Application Support/proxyloader/state.json` by default (overridable via `PROXYLOADER_STATE_DIR`). It includes the proxy list, active selection/chain, bind port, app targets, health-check settings, and LLM-access settings. The file is only readable by your user.

Proxy passwords are encrypted in `state.json` (`"password_enc": "enc:v1:..."`, Fernet/AES). The encryption key and the MCP access token live in the macOS Keychain (service `proxyloader`), or your OS keyring on Linux. If no keyring is available, they fall back to `secrets.json` next to the state file (mode `600`); set `PROXYLOADER_NO_KEYRING=1` to force that. State files from older versions with plain-text passwords are encrypted automatically the first time they're loaded.

## License

See [LICENSE](LICENSE) — personal/non-commercial use only; commercial use requires permission from the copyright holder.

## LLM access (MCP server)

ProxyLoader has a built-in [Model Context Protocol](https://modelcontextprotocol.io) server, so an LLM client (Claude Desktop, Claude Code, Cursor, etc.) can manage your proxies.

### From the app

Click **LLM access: Off** at the bottom of the window. ProxyLoader starts an MCP server at `http://127.0.0.1:8765/mcp` inside the app itself, so the LLM and the window share the same live state (changes the LLM makes show up in the window right away). A ready-to-paste client config, access token included, is copied to your clipboard:

```json
{
  "mcpServers": {
    "proxyloader": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Click the button again to re-copy the config, turn access off, or generate a new token (revokes the old one). The setting is remembered between launches. With Claude Code you can also run `claude mcp add --transport http proxyloader http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"`.

### Headless (no window, works on Linux)

```bash
python -m mcp_server                                     # stdio, for clients that spawn the server
python -m mcp_server --transport streamable-http          # HTTP on 127.0.0.1:8765, token required
python -m mcp_server --print-config                       # print the HTTP client config with token
python -m mcp_server --rotate-token                       # revoke the old token, make a new one
python -m mcp_server --state /path/to/state.json          # use a separate state file
```

Stdio client config: `{"mcpServers": {"proxyloader": {"command": "python", "args": ["-m", "mcp_server"], "cwd": "/path/to/ProxyLoader"}}}`. Don't run the headless server against the same state file while the app is open; use the app's built-in server instead.

### HTTP security

- Every request needs `Authorization: Bearer <token>`; anything else gets a 401.
- It only listens on loopback. Binding elsewhere needs an explicit `--allow-remote` (headless only).
- Host/Origin headers are checked against the bind address to block DNS-rebinding attacks from web pages.

### Tools

| Area | Tools |
| --- | --- |
| Status | `get_status` |
| Proxy list | `list_proxies`, `import_proxies`, `import_proxy_file`, `remove_proxies`, `clear_proxies`, `sort_proxies_by_latency` |
| Routing | `set_active_proxy`, `set_proxy_chain`, `clear_proxy_chain` |
| Server | `start_proxy_server`, `stop_proxy_server`, `set_system_proxy` |
| Health | `check_proxy_health`, `configure_health_checks` |
| Per-app | `list_apps`, `add_app`, `remove_app`, `pin_app_to_proxy`, `launch_apps`, `stop_app_proxies`, `cleanup_firefox_profiles` |

Read-only resources: `proxyloader://status` and `proxyloader://proxies`. Proxy passwords are never returned to the LLM (only `has_password`).

## Open questions

See `open_questions.md` for known caveats (e.g. Firefox profile cleanup edge cases).
