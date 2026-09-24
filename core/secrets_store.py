import json
import os
import platform
import secrets
import threading
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

SERVICE_NAME = "proxyloader"
STATE_KEY_NAME = "state-key"
MCP_TOKEN_NAME = "mcp-token"
FALLBACK_FILENAME = "secrets.json"
ENCRYPTED_PREFIX = "enc:v1:"


class SecretStoreError(Exception):
    pass


def _load_keyring():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        if platform.system() == "Darwin":
            from keyring.backends import macOS

            backend = macOS.Keyring()
        else:
            backend = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(backend, fail.Keyring):
        return None
    if getattr(backend, "priority", 1) <= 0:
        return None
    return backend


class SecretStore:
    def __init__(self, state_dir: Path, use_keyring: bool = True):
        self.state_dir = Path(state_dir)
        self._backend = _load_keyring() if use_keyring else None
        self._lock = threading.Lock()
        self._fernet: Optional[Fernet] = None

    @property
    def backend_name(self) -> str:
        if self._backend is None:
            return f"file ({self._fallback_path()})"
        return type(self._backend).__module__ + "." + type(self._backend).__name__

    def _fallback_path(self) -> Path:
        return self.state_dir / FALLBACK_FILENAME

    def _read_fallback(self) -> dict:
        path = self._fallback_path()
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"couldn't read {path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def _write_fallback(self, data: dict) -> None:
        path = self._fallback_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)

    def get(self, name: str) -> Optional[str]:
        if self._backend is not None:
            try:
                return self._backend.get_password(SERVICE_NAME, name)
            except Exception as exc:
                raise SecretStoreError(f"keychain read failed: {exc}") from exc
        value = self._read_fallback().get(name)
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        if self._backend is not None:
            try:
                self._backend.set_password(SERVICE_NAME, name, value)
                return
            except Exception as exc:
                raise SecretStoreError(f"keychain write failed: {exc}") from exc
        data = self._read_fallback()
        data[name] = value
        self._write_fallback(data)

    def get_or_create(self, name: str, factory) -> str:
        with self._lock:
            value = self.get(name)
            if value:
                return value
            value = factory()
            self.set(name, value)
            return value

    def _cipher(self) -> Fernet:
        if self._fernet is None:
            key = self.get_or_create(STATE_KEY_NAME, lambda: Fernet.generate_key().decode("ascii"))
            try:
                self._fernet = Fernet(key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise SecretStoreError("stored encryption key is corrupt") from exc
        return self._fernet

    def encrypt(self, plaintext: str) -> str:
        token = self._cipher().encrypt(plaintext.encode("utf-8"))
        return ENCRYPTED_PREFIX + token.decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value.startswith(ENCRYPTED_PREFIX):
            raise SecretStoreError("value isn't in the encrypted format")
        try:
            return self._cipher().decrypt(value[len(ENCRYPTED_PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretStoreError("couldn't decrypt; the encryption key doesn't match") from exc

    def mcp_token(self) -> str:
        return self.get_or_create(MCP_TOKEN_NAME, lambda: secrets.token_urlsafe(32))

    def rotate_mcp_token(self) -> str:
        with self._lock:
            value = secrets.token_urlsafe(32)
            self.set(MCP_TOKEN_NAME, value)
            return value


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


_default_stores: dict = {}
_default_lock = threading.Lock()


def store_for(state_dir: Path) -> SecretStore:
    key = str(Path(state_dir).resolve())
    with _default_lock:
        store = _default_stores.get(key)
        if store is None:
            use_keyring = os.environ.get("PROXYLOADER_NO_KEYRING") != "1"
            store = SecretStore(Path(state_dir), use_keyring=use_keyring)
            _default_stores[key] = store
        return store
