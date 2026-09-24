import os
import subprocess
from typing import List, Optional

from .app_target import AppTarget


def build_proxy_env(host: str, port: int) -> dict:
    env = dict(os.environ)
    proxy_url = f"socks5://{host}:{port}"
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        env[key] = proxy_url
    return env


def launch_app_target(
    target: AppTarget,
    host: str,
    port: int,
    extra_args: Optional[List[str]] = None,
) -> subprocess.Popen:
    env = build_proxy_env(host, port)
    args = [target.executable_path] + list(extra_args or [])
    return subprocess.Popen(args, env=env)
