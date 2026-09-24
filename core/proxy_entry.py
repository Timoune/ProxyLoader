from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse
import csv
import re


VALID_SCHEMES = ("socks5", "socks4", "http")
_SCHEME_ALIASES = {"socks5h": "socks5", "socks4a": "socks4", "https": "http"}
_DELIMITED_FORMS = (
    lambda line: line.split(":", 3),
    lambda line: line.split("|", 3),
    lambda line: re.split(r"\s+", line, maxsplit=3),
)


@dataclass
class ProxyEntry:
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

    def label(self) -> str:
        auth = f"{self.username}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def validate(self) -> bool:
        return self.scheme in VALID_SCHEMES and bool(self.host) and 0 < self.port < 65536

    def dedupe_key(self) -> Tuple[str, str, int, Optional[str], Optional[str]]:
        return (self.scheme, self.host, self.port, self.username, self.password)


def _try_int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _from_url_form(line: str) -> Optional[ProxyEntry]:
    parsed = urlparse(line)
    scheme = _SCHEME_ALIASES.get(parsed.scheme.lower(), parsed.scheme.lower())
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return None
    entry = ProxyEntry(scheme, host, port, parsed.username, parsed.password)
    return entry if entry.validate() else None


def _from_auth_at_host_form(line: str) -> Optional[ProxyEntry]:
    creds, sep, hostport = line.rpartition("@")
    if not sep:
        return None
    host, _, port_str = hostport.partition(":")
    port = _try_int(port_str)
    if not host or port is None:
        return None
    username, _, password = creds.partition(":")
    entry = ProxyEntry("socks5", host, port, username or None, password or None)
    return entry if entry.validate() else None


def _from_comma_form(line: str) -> Optional[ProxyEntry]:
    try:
        row = [cell.strip() for cell in next(csv.reader([line]))]
    except StopIteration:
        return None
    entry = parse_csv_row(row)
    if entry:
        return entry
    if len(row) >= 2:
        port = _try_int(row[1])
        if row[0] and port is not None:
            username = row[2] if len(row) > 2 and row[2] else None
            password = row[3] if len(row) > 3 and row[3] else None
            entry = ProxyEntry("socks5", row[0], port, username, password)
            return entry if entry.validate() else None
    return None


def _from_delimited_form(line: str) -> Optional[ProxyEntry]:
    for split in _DELIMITED_FORMS:
        parts = [part.strip() for part in split(line)]
        if len(parts) < 2:
            continue
        host, port = parts[0], _try_int(parts[1])
        if not host or port is None:
            continue
        username = parts[2] if len(parts) > 2 and parts[2] else None
        password = parts[3] if len(parts) > 3 and parts[3] else None
        entry = ProxyEntry("socks5", host, port, username, password)
        if entry.validate():
            return entry
    return None


def parse_line(raw_line: str) -> Optional[ProxyEntry]:
    line = raw_line.strip().rstrip(",;")
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    if "://" in line:
        return _from_url_form(line)

    if "@" in line:
        entry = _from_auth_at_host_form(line)
        if entry:
            return entry

    if "," in line:
        entry = _from_comma_form(line)
        if entry:
            return entry

    return _from_delimited_form(line)


def parse_csv_row(row: List[str]) -> Optional[ProxyEntry]:
    if len(row) < 3:
        return None
    scheme = row[0].strip().lower()
    host = row[1].strip()
    try:
        port = int(row[2].strip())
    except ValueError:
        return None
    username = row[3].strip() if len(row) > 3 and row[3].strip() else None
    password = row[4].strip() if len(row) > 4 and row[4].strip() else None
    entry = ProxyEntry(scheme, host, port, username, password)
    return entry if entry.validate() else None


def load_proxy_file(path: str) -> List[ProxyEntry]:
    entries: List[ProxyEntry] = []
    suffix = Path(path).suffix.lower()
    with open(path, "r", encoding="utf-8") as handle:
        if suffix == ".csv":
            for row in csv.reader(handle):
                entry = parse_csv_row(row)
                if entry:
                    entries.append(entry)
        else:
            for raw_line in handle:
                entry = parse_line(raw_line)
                if entry:
                    entries.append(entry)
    return entries


def _iter_candidate_lines(text: str) -> Iterator[str]:
    for raw_line in text.replace(";", "\n").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and not line.startswith("//"):
            yield line


def parse_block(text: str) -> List[ProxyEntry]:
    entries: List[ProxyEntry] = []
    for line in _iter_candidate_lines(text):
        entry = parse_line(line)
        if entry:
            entries.append(entry)
    return entries


def count_candidate_lines(text: str) -> int:
    return sum(1 for _ in _iter_candidate_lines(text))
