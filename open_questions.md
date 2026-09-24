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
