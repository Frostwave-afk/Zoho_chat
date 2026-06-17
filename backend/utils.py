import hashlib
import base64
from cryptography.fernet import Fernet


def _make_fernet(secret_key: str) -> Fernet:
    """Derive a stable 32-byte Fernet key from SECRET_KEY."""
    raw = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_token(token: str, secret_key: str) -> str:
    return _make_fernet(secret_key).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str, secret_key: str) -> str:
    return _make_fernet(secret_key).decrypt(encrypted.encode()).decode()
