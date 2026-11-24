"""AES-256-GCM credential vault backed by SSOT + OS keychain."""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from robber.config import (
    VAULT_ACCOUNT,
    VAULT_KEY_BYTES,
    VAULT_KEY_ENV,
    VAULT_NONCE_BYTES,
    VAULT_SERVICE,
)
from robber.jail import workspace_root
from robber.ssot_db import SsotDB, get_ssot_db

TARGET = f"{VAULT_SERVICE}/{VAULT_ACCOUNT}"


class CredentialVault:
    """Encrypt API keys and proxy tokens at rest. Never logs plaintext."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        db: SsotDB | None = None,
        master: bytes | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root else workspace_root()
        self.db = db or get_ssot_db(self.root)
        self.db.connect()
        self._master = master if master is not None else unlock_master()

    def put(self, provider: str, secret: str, *, kind: str = "api_key") -> dict[str, Any]:
        text = (secret or "").strip()
        if not text:
            raise ValueError("secret is empty")
        nonce = os.urandom(VAULT_NONCE_BYTES)
        aad = _aad(provider, kind)
        blob = AESGCM(self._master).encrypt(nonce, text.encode("utf-8"), aad)
        row = self.db.put_credential(
            provider=provider,
            kind=kind,
            nonce=_b64(nonce),
            ciphertext=_b64(blob),
            fingerprint=_fingerprint(text),
        )
        return _public(row)

    def get(self, provider: str, *, kind: str = "api_key") -> str | None:
        row = self.db.find_credential(provider, kind=kind)
        if row is None:
            return None
        nonce = _unb64(str(row.get("nonce") or ""))
        blob = _unb64(str(row.get("ciphertext") or ""))
        if not nonce or not blob:
            return None
        aad = _aad(provider, kind)
        try:
            return AESGCM(self._master).decrypt(nonce, blob, aad).decode("utf-8")
        except Exception:
            return None

    def delete(self, provider: str, *, kind: str = "api_key") -> int:
        return self.db.delete_credential(provider, kind=kind)

    def list_public(self) -> list[dict[str, Any]]:
        return [_public(row) for row in self.db.list_credentials()]

    def status(self) -> dict[str, bool]:
        return {str(row.get("provider") or ""): True for row in self.list_public() if row.get("provider")}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "provider": row.get("provider"),
        "kind": row.get("kind"),
        "fingerprint": row.get("fingerprint"),
        "updated_at": row.get("updated_at"),
    }


def _aad(provider: str, kind: str) -> bytes:
    return f"{(provider or '').strip().lower()}|{(kind or 'api_key').strip()}".encode("utf-8")


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"))
    except Exception:
        return b""


def unlock_master() -> bytes:
    env = (os.environ.get(VAULT_KEY_ENV) or "").strip()
    if env:
        return _parse_key(env)
    stored = read_os_master()
    if stored:
        return stored
    generated = os.urandom(VAULT_KEY_BYTES)
    write_os_master(generated)
    return generated


def _parse_key(raw: str) -> bytes:
    text = raw.strip()
    if len(text) == VAULT_KEY_BYTES * 2:
        try:
            key = bytes.fromhex(text)
            if len(key) == VAULT_KEY_BYTES:
                return key
        except ValueError:
            pass
    try:
        key = base64.b64decode(text)
        if len(key) == VAULT_KEY_BYTES:
            return key
    except Exception:
        pass
    raise ValueError(f"{VAULT_KEY_ENV} must be 32 bytes hex or base64")


def read_os_master() -> bytes | None:
    if sys.platform == "win32":
        blob = _win_cred_read(TARGET)
        if blob and len(blob) == VAULT_KEY_BYTES:
            return blob
    else:
        blob = _posix_secret_read(TARGET)
        if blob and len(blob) == VAULT_KEY_BYTES:
            return blob
    path = _file_key_path()
    if path.is_file():
        data = path.read_bytes()
        if len(data) == VAULT_KEY_BYTES:
            return data
    return None


def write_os_master(key: bytes) -> None:
    if len(key) != VAULT_KEY_BYTES:
        raise ValueError("master key must be 32 bytes")
    if sys.platform == "win32":
        if _win_cred_write(TARGET, key):
            return
    else:
        if _posix_secret_write(TARGET, key):
            return
    path = _file_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _file_key_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "AgentRobber" / "master.key"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "agent-robber" / "master.key"


def _win_cred_read(target: str) -> bytes | None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    ptr = ctypes.POINTER(Credential)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        size = int(cred.CredentialBlobSize)
        if size <= 0 or not cred.CredentialBlob:
            return None
        return bytes(ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * size)).contents)
    finally:
        advapi.CredFree(ptr)


def _win_cred_write(target: str, blob: bytes) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = Credential()
    cred.Flags = 0
    cred.Type = 1
    cred.TargetName = target
    cred.Comment = None
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = 2
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    cred.UserName = VAULT_ACCOUNT
    return bool(advapi.CredWriteW(ctypes.byref(cred), 0))


def _posix_secret_read(target: str) -> bytes | None:
    try:
        import keyring
    except Exception:
        return None
    try:
        value = keyring.get_password(VAULT_SERVICE, target)
    except Exception:
        return None
    if not value:
        return None
    try:
        return _parse_key(value)
    except ValueError:
        raw = value.encode("utf-8")
        return raw if len(raw) == VAULT_KEY_BYTES else None


def _posix_secret_write(target: str, key: bytes) -> bool:
    try:
        import keyring
    except Exception:
        return False
    try:
        keyring.set_password(VAULT_SERVICE, target, key.hex())
        return True
    except Exception:
        return False


_VAULT: CredentialVault | None = None


def iter_secured_keys(provider: str) -> list[str]:
    """Env keys first; SSOT vault only if already unlocked or ROBBER_VAULT_KEY is set."""
    from robber.vault import iter_keys

    found = iter_keys(provider)
    if found:
        return found
    env = (os.environ.get(VAULT_KEY_ENV) or "").strip()
    if _VAULT is None and not env:
        return []
    try:
        secret = get_vault().get(provider)
        if not secret:
            secret = get_vault().get(provider, kind="proxy")
        if not secret:
            secret = get_vault().get(provider, kind="proxy_token")
    except Exception:
        return []
    return [secret] if secret else []


def get_vault(root: Path | str | None = None) -> CredentialVault:
    global _VAULT
    if _VAULT is None:
        _VAULT = CredentialVault(root)
    return _VAULT


def bind_vault(vault: CredentialVault | None) -> None:
    global _VAULT
    _VAULT = vault
