"""The server's master secret and what is derived from it.

One random master key lives in `<data dir>/secret.key` (readable by the server's user only), never
in the database. Purpose keys are derived from it with HMAC, so a stolen database alone does not
reveal stored API keys or let anyone mint gateway tokens.
"""

import base64
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

KEY_FILE = "secret.key"


class VaultError(Exception):
    """A stored secret could not be decrypted (wrong or rotated master key)."""


def load_master_key(data_dir: Path) -> bytes:
    """The master key, created on first use."""
    path = data_dir / KEY_FILE
    if path.is_file():
        return bytes.fromhex(path.read_text(encoding="ascii").split()[0])
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(key.hex() + "\n")
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    return key


class Vault:
    """Derived keys and encryption of stored secrets."""

    def __init__(self, master: bytes, previous: list[bytes] | None = None) -> None:
        self.master = master
        keys = [master, *(previous or [])]
        self._fernet = MultiFernet([Fernet(self._fernet_key(k)) for k in keys])

    @staticmethod
    def _fernet_key(master: bytes) -> bytes:
        return base64.urlsafe_b64encode(hmac.new(master, b"fernet", hashlib.sha256).digest())

    def derive(self, purpose: str) -> bytes:
        """A key for one purpose (gateway tokens, sessions, …)."""
        return hmac.new(self.master, purpose.encode("utf-8"), hashlib.sha256).digest()

    def encrypt(self, secret: str) -> str:
        """Encrypt a secret for storage."""
        return self._fernet.encrypt(secret.encode("utf-8")).decode("ascii")

    def decrypt(self, stored: str) -> str:
        """Decrypt a stored secret."""
        try:
            return self._fernet.decrypt(stored.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise VaultError("a stored secret cannot be decrypted with this master key") from None


def sign(key: bytes, message: str) -> str:
    """A short URL-safe HMAC signature."""
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:43]
