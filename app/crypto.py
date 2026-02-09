"""Encryption helpers for sensitive OAuth token fields."""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import get_settings


class TokenCipher:
    """Wrapper around Fernet token encryption/decryption operations."""

    def __init__(self, key: str) -> None:
        """Create a cipher instance.

        Args:
            key: Fernet key string in URL-safe base64 format.
        """

        self._fernet = Fernet(key.encode("utf-8"))

    def encrypt(self, raw_value: str) -> str:
        """Encrypt plaintext and return a safe text payload."""

        return self._fernet.encrypt(raw_value.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        """Decrypt a token field back to plaintext."""

        return self._fernet.decrypt(encrypted_value.encode("utf-8")).decode("utf-8")


def get_token_cipher() -> TokenCipher:
    """Build a token cipher using the current application settings."""

    settings = get_settings()
    return TokenCipher(settings.token_encryption_key)
