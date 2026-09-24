import subprocess
from typing import List


def _enabled_network_services() -> List[str]:
    output = subprocess.run(
        ["networksetup", "-listallnetworkservices"],
        capture_output=True, text=True, check=True,
    ).stdout
    lines = output.splitlines()[1:]
    return [line for line in lines if line and not line.startswith("*")]


def _run_privileged(shell_command: str) -> None:
    escaped = shell_command.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e", f'do shell script "{escaped}" with administrator privileges'],
        check=True,
    )


def enable_system_socks_proxy(host: str, port: int) -> None:
    commands = []
    for service in _enabled_network_services():
        quoted = service.replace("'", "'\\''")
        commands.append(f"networksetup -setsocksfirewallproxy '{quoted}' {host} {port}")
        commands.append(f"networksetup -setsocksfirewallproxystate '{quoted}' on")
    if commands:
        _run_privileged(" && ".join(commands))


def disable_system_socks_proxy() -> None:
    commands = []
    for service in _enabled_network_services():
        quoted = service.replace("'", "'\\''")
        commands.append(f"networksetup -setsocksfirewallproxystate '{quoted}' off")
    if commands:
        _run_privileged(" && ".join(commands))
