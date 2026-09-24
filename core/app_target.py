from dataclasses import dataclass
from typing import Optional


@dataclass
class AppTarget:
    display_name: str
    executable_path: str
    proxy_index: Optional[int] = None
