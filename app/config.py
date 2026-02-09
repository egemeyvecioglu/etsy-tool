"""Configuration loading for the Etsy tool.

The settings loader supports two configuration layers:
1. Environment variables (recommended for sensitive values).
2. YAML defaults file for non-sensitive baseline values.

Environment variables use the `ETSY_TOOL_` prefix.
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from environment variables.

    Fields include sensible local defaults; production deployments should
    override security-sensitive values through environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="ETSY_TOOL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Etsy Bulk Variation Price Tool"
    env: str = "development"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite:///./etsy_tool.db"

    session_secret_key: str = "local-dev-session-secret"
    token_encryption_key: str = "local-dev-token-secret"

    # Preferred env names:
    # - ETSY_TOOL_OAUTH_CLIENT_ID
    # - ETSY_TOOL_OAUTH_CLIENT_SECRET
    # - ETSY_TOOL_OAUTH_REDIRECT_URI
    # Backward-compatible names are also accepted.
    etsy_client_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ETSY_TOOL_OAUTH_CLIENT_ID",
            "ETSY_TOOL_ETSY_CLIENT_ID",
        ),
    )
    etsy_client_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ETSY_TOOL_OAUTH_CLIENT_SECRET",
            "ETSY_TOOL_ETSY_CLIENT_SECRET",
        ),
    )
    etsy_redirect_uri: str = Field(
        default="http://localhost:8000/oauth/etsy/callback",
        validation_alias=AliasChoices(
            "ETSY_TOOL_OAUTH_REDIRECT_URI",
            "ETSY_TOOL_ETSY_REDIRECT_URI",
        ),
    )
    etsy_scopes: str = "listings_r listings_w"

    etsy_authorize_url: str = "https://www.etsy.com/oauth/connect"
    etsy_token_url: str = "https://api.etsy.com/v3/public/oauth/token"
    etsy_base_url: str = "https://openapi.etsy.com/v3/application"

    request_timeout_seconds: float = 20.0
    max_retries: int = 4
    per_user_min_interval_seconds: float = 0.2
    job_poll_interval_seconds: float = 1.5
    token_expiry_buffer_seconds: int = 120

    max_percentage_change: float = 50.0
    enable_worker: bool = True
    allow_admin_bypass_login: bool = False
    listing_cache_max_age_hours: int = 6
    support_email: str = "support@example.com"
    legal_terms_version: str = "2026-02-09"
    legal_privacy_version: str = "2026-02-09"
    default_locale: str = "en"
    supported_locales: str = "en,tr"
    i18n_path: str = "config/i18n"
    job_form_schema_path: str = "config/ui/job_form.yaml"

    config_path: str = Field(default="config/defaults.yaml")


def _load_yaml_defaults(path: str) -> dict[str, Any]:
    """Load YAML defaults from disk.

    Args:
        path: Relative or absolute path to a YAML config file.

    Returns:
        A dictionary of settings values from YAML.
    """

    config_file = Path(path)
    if not config_file.exists():
        return {}
    raw = config_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"YAML config at {path} must contain a mapping")
    return parsed


def _overlay_yaml_defaults(settings: Settings, yaml_values: dict[str, Any]) -> Settings:
    """Apply YAML values only where environment did not override defaults.

    Environment values are loaded by `BaseSettings` and should always win.
    This function only fills fields that still match class defaults.
    """

    data = settings.model_dump()
    for key, value in yaml_values.items():
        if key not in Settings.model_fields:
            continue
        default_value = Settings.model_fields[key].default
        if data.get(key) == default_value:
            data[key] = value
    return Settings.model_validate(data)


def to_fernet_key(raw_value: str) -> str:
    """Convert a user-provided key/secret string into a valid Fernet key.

    Users can provide either:
    - A full Fernet key string
    - Any secret text, which is deterministically hashed into a valid key

    Args:
        raw_value: Raw token encryption input from configuration.

    Returns:
        A URL-safe base64 encoded Fernet key string.
    """

    candidate = raw_value.strip().encode("utf-8")
    try:
        # If this decode succeeds to 32 bytes, the user already provided
        # a proper Fernet key.
        decoded = base64.urlsafe_b64decode(candidate)
        if len(decoded) == 32:
            return raw_value.strip()
    except Exception:
        pass

    digest = hashlib.sha256(raw_value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


def _validate_runtime_settings(settings: Settings) -> None:
    """Validate safety/compliance-sensitive settings before app startup."""

    if settings.listing_cache_max_age_hours <= 0:
        raise ValueError("listing_cache_max_age_hours must be greater than 0")

    support_email = settings.support_email.strip()
    if not support_email or "@" not in support_email:
        raise ValueError("support_email must be a valid monitored email address")

    env_lower = settings.env.strip().lower()
    is_production = env_lower in {"production", "prod"}
    if not is_production:
        return

    redirect = urlparse(settings.etsy_redirect_uri.strip())
    if redirect.scheme.lower() != "https":
        raise ValueError("Production OAuth redirect URI must use HTTPS")

    if settings.allow_admin_bypass_login:
        raise ValueError("allow_admin_bypass_login must be false in production")

    if support_email == "support@example.com":
        raise ValueError("Set a real monitored support_email in production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Create and cache runtime settings.

    Configuration source order:
    1. Class defaults
    2. YAML defaults file
    3. Environment variables / `.env`
    """

    # First pass loads env values and discovers optional config path override.
    env_settings = Settings()
    config_path = os.getenv("ETSY_TOOL_CONFIG_PATH", env_settings.config_path)
    yaml_defaults = _load_yaml_defaults(config_path)
    merged = _overlay_yaml_defaults(env_settings, yaml_defaults)

    # Normalize token encryption input to a valid Fernet key format.
    merged_data = merged.model_dump()
    merged_data["token_encryption_key"] = to_fernet_key(merged.token_encryption_key)
    validated = Settings.model_validate(merged_data)
    _validate_runtime_settings(validated)
    return validated
