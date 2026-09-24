import glob
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Tuple

PROFILE_DIR_PREFIX = "proxyloader-firefox-"

# type 1 = manual proxy config. socks_remote_dns sends DNS lookups through the
# proxy too (otherwise hostnames leak via the system resolver even though the
# connection itself is proxied). no_proxies_on is cleared so Firefox's default
# localhost/127.0.0.1 bypass list doesn't accidentally exempt anything.
_PREFS_TEMPLATE = """\
user_pref("network.proxy.type", 1);
user_pref("network.proxy.socks", "{host}");
user_pref("network.proxy.socks_port", {port});
user_pref("network.proxy.socks_version", 5);
user_pref("network.proxy.socks_remote_dns", true);
user_pref("network.proxy.no_proxies_on", "");
"""


def create_proxied_profile(host: str, port: int) -> str:
    """Creates a throwaway Firefox profile directory whose user.js points
    every protocol at the given SOCKS5 proxy. Firefox has no env-var or
    CLI-flag way to set its proxy (unlike Chromium's --proxy-server) — the
    profile's prefs are the only lever. Returns the profile dir path.

    The caller (`Mac/glass_window.py`) is responsible for cleaning this up
    once the launched process is done with it — see `sweep_stale_profiles`
    below for the filesystem-wide fallback that catches anything a crashed
    or force-quit run left behind instead.
    """
    profile_dir = tempfile.mkdtemp(prefix=PROFILE_DIR_PREFIX)
    prefs_path = Path(profile_dir) / "user.js"
    prefs_path.write_text(
        _PREFS_TEMPLATE.format(host=host, port=port), encoding="utf-8"
    )
    return profile_dir


def launch_args_for_profile(profile_dir: str) -> List[str]:
    # -no-remote + -new-instance: without both, launching Firefox while
    # another Firefox process is already running just hands the request off
    # to that existing instance (which ignores -profile entirely) instead of
    # actually starting a new process on the throwaway profile.
    return ["-profile", profile_dir, "-no-remote", "-new-instance"]


def _profile_looks_in_use(profile_dir: str) -> bool:
    """Best-effort check for whether a Firefox process might still have this
    profile open. Firefox writes a `lock` file (a symlink on macOS/Linux)
    into a profile directory for as long as it's running, and removes it on
    a clean exit — so its presence is a reasonable (not perfect) signal.
    An *unclean* exit (force-quit, crash, `kill -9`) can leave that lock
    file behind on a profile nothing is using anymore, which is exactly the
    stale-leftover case `sweep_stale_profiles` exists to catch — so this is
    deliberately a "when in doubt, don't delete" check, not a guarantee the
    directory really is still in use.
    """
    lock_path = os.path.join(profile_dir, "lock")
    return os.path.islink(lock_path) or os.path.exists(lock_path)


def sweep_stale_profiles() -> Tuple[List[str], List[str]]:
    """Scans the OS temp dir for *every* proxyloader Firefox profile
    directory currently sitting there — not just ones this process itself
    launched and is tracking, but ones left over from a crash, a
    force-quit, a killed proxyloader process, or a previous version that
    didn't clean up at all — and removes whichever ones don't look like a
    running Firefox still has open.

    Returns (removed, skipped): `removed` is every directory actually
    deleted; `skipped` is every directory that matched the prefix but was
    left alone because `_profile_looks_in_use` found a lock file. Skipped
    profiles aren't a bug — they're the sweep declining to risk yanking a
    live Firefox session's profile out from under it (see the Firefox
    support section in open_questions.md for why that's a hard line, not
    just a nicety).
    """
    pattern = os.path.join(tempfile.gettempdir(), f"{PROFILE_DIR_PREFIX}*")
    removed: List[str] = []
    skipped: List[str] = []
    for path in glob.glob(pattern):
        if not os.path.isdir(path):
            continue
        if _profile_looks_in_use(path):
            skipped.append(path)
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed, skipped
