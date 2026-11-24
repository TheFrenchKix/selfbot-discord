"""Chiffrement des secrets au repos + store de tokens.

Backends:
  * ``dpapi``  : Windows DPAPI (lié à l'utilisateur Windows, aucune clé à gérer)
  * ``fernet`` : clé maître dans ``SELFBOT_MASTER_KEY`` (portable)

Les tokens ne sont jamais stockés en clair ni affichés: utiliser ``mask_token``.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .config import mask_token

_MAGIC_DPAPI = "dpapi:"
_MAGIC_FERNET = "fernet:"


class SecretsError(Exception):
    pass


# --------------------------------------------------------------------------- #
# DPAPI (Windows)
# --------------------------------------------------------------------------- #
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    if sys.platform != "win32":  # pragma: no cover
        raise SecretsError("DPAPI indisponible hors Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise SecretsError("Échec DPAPI (profil Windows différent ou données corrompues)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------- #
# API publique
# --------------------------------------------------------------------------- #
def _fernet(key: Optional[str] = None):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover
        raise SecretsError("Le paquet 'cryptography' est requis pour le backend fernet") from exc
    key = key or os.environ.get("SELFBOT_MASTER_KEY")
    if not key:
        raise SecretsError("SELFBOT_MASTER_KEY absente (générer avec: python -m selfbot.secrets genkey)")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise SecretsError("SELFBOT_MASTER_KEY invalide (clé Fernet base64 de 32 octets attendue)") from exc


def generate_master_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def default_backend() -> str:
    if os.environ.get("SELFBOT_MASTER_KEY"):
        return "fernet"
    return "dpapi" if sys.platform == "win32" else "fernet"


def encrypt(plaintext: str, backend: Optional[str] = None, key: Optional[str] = None) -> str:
    backend = backend or default_backend()
    raw = plaintext.encode("utf-8")
    if backend == "dpapi":
        return _MAGIC_DPAPI + base64.b64encode(_dpapi(raw, True)).decode()
    if backend == "fernet":
        return _MAGIC_FERNET + _fernet(key).encrypt(raw).decode()
    raise SecretsError(f"Backend inconnu: {backend}")


def decrypt(token: str, key: Optional[str] = None) -> str:
    if token.startswith(_MAGIC_DPAPI):
        return _dpapi(base64.b64decode(token[len(_MAGIC_DPAPI):]), False).decode("utf-8")
    if token.startswith(_MAGIC_FERNET):
        try:
            return _fernet(key).decrypt(token[len(_MAGIC_FERNET):].encode()).decode("utf-8")
        except SecretsError:
            raise
        except Exception as exc:
            raise SecretsError("Déchiffrement impossible (mauvaise clé ?)") from exc
    raise SecretsError("Format de secret inconnu")


class TokenStore:
    """Store JSON de tokens chiffrés: ``{instance_id: secret_chiffré}``."""

    def __init__(self, path: str | os.PathLike, backend: Optional[str] = None, key: Optional[str] = None):
        self.path = Path(path)
        self.backend = backend
        self.key = key
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SecretsError(f"Store corrompu: {self.path}") from exc

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover
            pass
        os.replace(tmp, self.path)

    def set(self, instance_id: str, token: str) -> None:
        with self._lock:
            data = self._load()
            data[instance_id] = encrypt(token, self.backend, self.key)
            self._save(data)

    def get(self, instance_id: str) -> Optional[str]:
        with self._lock:
            blob = self._load().get(instance_id)
        return decrypt(blob, self.key) if blob else None

    def delete(self, instance_id: str) -> bool:
        with self._lock:
            data = self._load()
            if instance_id not in data:
                return False
            del data[instance_id]
            self._save(data)
            return True

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._load())

    def find_duplicate(self, token: str) -> Optional[str]:
        """Retourne l'id de l'instance qui utilise déjà ce token (ou None)."""
        for iid in self.ids():
            try:
                if self.get(iid) == token:
                    return iid
            except SecretsError:
                continue
        return None


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        print(generate_master_key())
    else:
        print("usage: python -m selfbot.secrets genkey")

__all__ = ["SecretsError", "TokenStore", "decrypt", "encrypt", "generate_master_key", "mask_token"]
