"""OAuth helper utilities (PKCE + state generation)."""

from __future__ import annotations

import base64
import hashlib
import secrets


def generate_code_verifier() -> str:
    """Generate a high-entropy PKCE code verifier."""

    return secrets.token_urlsafe(64)


def build_code_challenge(code_verifier: str) -> str:
    """Create the S256 PKCE code challenge from the verifier."""

    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")


def generate_state_token() -> str:
    """Generate an OAuth state token for CSRF protection."""

    return secrets.token_urlsafe(32)
