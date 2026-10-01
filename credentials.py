"""Desktop credentials: explicit session values and Windows Credential Manager."""
import json
import os
import sys
from dataclasses import dataclass, field

SERVICE = "Standgrox Maker Bot"
ACCOUNT = "desktop-session"


@dataclass(frozen=True)
class Credentials:
    token: str = field(repr=False)
    sign_key_hex: str = field(repr=False)

    @classmethod
    def parse(cls, token, key):
        token, key = token.strip(), key.strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError("token")
        if len(key) != 64:
            raise ValueError("key")
        try:
            raw = bytes.fromhex(key)
        except ValueError:
            raise ValueError("key") from None
        if len(raw) != 32:
            raise ValueError("key")
        return cls(token, key.lower())

    @classmethod
    def from_environment(cls):
        return cls.parse(os.getenv("STANDX_TOKEN", ""),
                         os.getenv("STANDX_SIGN_KEY_HEX", ""))


class CredentialStore:
    """Never fall back to a plaintext or user-configured keyring backend."""
    def __init__(self, backend=None):
        self.backend = backend
        if backend is None and sys.platform == "win32":
            try:
                from keyring.backends.Windows import WinVaultKeyring
                self.backend = WinVaultKeyring()
            except (ImportError, RuntimeError):
                pass

    @property
    def available(self):
        return self.backend is not None

    def load(self):
        if not self.available:
            return None
        value = self.backend.get_password(SERVICE, ACCOUNT)
        if value is None:
            return None
        data = json.loads(value)
        return Credentials.parse(data["token"], data["sign_key_hex"])

    def save(self, credentials):
        if not self.available:
            raise RuntimeError("secure_storage_unavailable")
        self.backend.set_password(SERVICE, ACCOUNT, json.dumps({
            "token": credentials.token, "sign_key_hex": credentials.sign_key_hex}))

    def delete(self):
        if self.available and self.backend.get_password(SERVICE, ACCOUNT) is not None:
            self.backend.delete_password(SERVICE, ACCOUNT)


def validate_connection(credentials, client_factory):
    """Read-only validation. Never places an order or changes account settings."""
    client = client_factory(token=credentials.token, sign_key_hex=credentials.sign_key_hex)
    data = client._get("/api/query_balance", auth=True)
    if isinstance(data, dict) and data.get("code", 0) != 0:
        raise ValueError("account")
    balance = data.get("result", data) if isinstance(data, dict) else None
    if not isinstance(balance, dict) or "equity" not in balance:
        raise ValueError("account")
    # This authenticates the token; signed writes are intentionally not exercised.
    return client
