import plistlib
from pathlib import Path
from typing import List, Optional, Tuple

from core.app_target import AppTarget
from core import firefox_profile

_CHROMIUM_FAMILY_BUNDLES = {"Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge", "Vivaldi"}
_FIREFOX_FAMILY_BUNDLES = {"Firefox", "Firefox Developer Edition", "Firefox Nightly", "Firefox ESR"}


def resolve_app_bundle(app_path: str) -> Optional[AppTarget]:
    bundle = Path(app_path)
    plist_path = bundle / "Contents" / "Info.plist"
    if not plist_path.exists():
        return None

    with open(plist_path, "rb") as handle:
        info = plistlib.load(handle)

    executable_name = info.get("CFBundleExecutable")
    if not executable_name:
        return None

    display_name = info.get("CFBundleName") or bundle.stem
    executable_path = str(bundle / "Contents" / "MacOS" / executable_name)
    return AppTarget(display_name, executable_path)


def known_extra_args(target: AppTarget, host: str, port: int) -> Tuple[List[str], Optional[str]]:
    """Extra CLI args needed to actually get `target` onto the proxy, beyond
    the ALL_PROXY/HTTP_PROXY/HTTPS_PROXY env vars `core/launcher.py` always
    sets. Most apps read those env vars and need nothing more; a few browser
    families ignore them and need their own mechanism instead.

    Returns (extra_args, cleanup_path). cleanup_path is None except for the
    Firefox family, where it's the throwaway profile directory this call just
    created on disk (see `core/firefox_profile.py`) — the caller is
    responsible for deleting it later (once the launched process is done
    with it), `known_extra_args` only creates.
    """
    if target.display_name in _CHROMIUM_FAMILY_BUNDLES:
        return [f"--proxy-server=socks5://{host}:{port}"], None
    if target.display_name in _FIREFOX_FAMILY_BUNDLES:
        profile_dir = firefox_profile.create_proxied_profile(host, port)
        return firefox_profile.launch_args_for_profile(profile_dir), profile_dir
    return [], None


