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

- **Untested on macOS.** The GUI changes (in-app MCP host, "LLM access" button, Keychain storage) were written without a Mac to run them on. The headless server, HTTP auth, encryption and migration were tested on Linux. Check the Keychain path on a real Mac before shipping.
- **py2app bundle.** `setup.py` now lists `mcp`, `uvicorn`, `starlette`, `keyring`, `cryptography` and friends under `packages`, but the bundle hasn't been built. If the built app says "Couldn't turn on LLM access: No module named …", add that module to the list.
- **Keychain prompts.** macOS ties Keychain access to the app's code signature. An unsigned or re-built `.app` (or switching between `python main.py` and the bundle) may trigger an "allow access" prompt the first time it reads the key. Clicking "Always Allow" fixes it.
- **Lost encryption key.** If the Keychain item (or `secrets.json` in fallback mode) is deleted, saved passwords can't be decrypted. They load as empty, `get_status` reports `undecryptable_passwords`, and a copy of the old file is kept as `state.json.undecryptable-backup` so the next save doesn't destroy them for good. There's no UI for this yet.
- **What encryption protects against.** With the Keychain, a copied or leaked `state.json` is useless on its own. In fallback mode the key sits in `secrets.json` in the same folder, so it only helps if `state.json` is shared by itself (backups, pasting it somewhere). Anything running as your user can still decrypt it either way.
- **Main-thread dispatch.** In the app, every MCP tool call runs on the AppKit main thread so it can't race the window. While a dialog is open, calls wait (default run-loop mode only) and give up after 30 seconds with "window is busy". A call that timed out still runs once the dialog closes. `check_proxy_health` waits on the probe off the main thread so the window doesn't freeze.
- **Admin prompts.** `set_system_proxy` / `start_proxy_server(system_proxy=true)` still show the macOS admin password prompt, so a human has to approve them.
- **Remote HTTP.** `--allow-remote` (headless only) is plain HTTP; the bearer token travels unencrypted. Put it behind TLS (reverse proxy / SSH tunnel) if you use it.
- **App launches.** Launched apps are not killed on `stop_app_proxies` or exit, same as the Start/Stop button. Firefox throwaway profiles are deleted when that Firefox exits while ProxyLoader is running; otherwise use `cleanup_firefox_profiles`.
