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
  app_target.py             # AppTarget dataclass (an app + its optional pinned proxy)
  launcher.py               # Launches an app subprocess with proxy env vars set
  firefox_profile.py        # Throwaway Firefox profile generation + stale-profile cleanup
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

State is saved to `~/Library/Application Support/proxyloader/state.json` by default (overridable via `PROXYLOADER_STATE_DIR`). It includes the proxy list, active selection/chain, bind port, app targets, and health-check settings.

## License

See [LICENSE](LICENSE) — personal/non-commercial use only; commercial use requires permission from the copyright holder.

## Open questions

See `open_questions.md` for known caveats (e.g. Firefox profile cleanup edge cases).
