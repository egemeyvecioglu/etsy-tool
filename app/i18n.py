"""Internationalization helpers for UI templates and localized labels.

This module provides a lightweight YAML-backed translation system with:
- Configurable default/supported locales
- Session + query-param locale selection
- Fallback to default locale for missing keys
- Small helper methods for enum/status localization
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import Request

from app.config import get_settings


_catalog_signature_cache: tuple[tuple[str, float], ...] | None = None


def _deep_get(mapping: dict[str, Any], dotted_key: str) -> str | None:
    """Return nested translation value by dotted key path.

    Args:
        mapping: Locale translation dictionary.
        dotted_key: Translation key (e.g. `pages.index.title`).

    Returns:
        The translated string if found, otherwise `None`.
    """

    current: Any = mapping
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]

    if isinstance(current, str):
        return current
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load one YAML file and validate the top-level mapping shape."""

    if not path.exists():
        return {}

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Translation file must contain a mapping: {path}")
    return payload


def _split_supported_locales(raw: str) -> list[str]:
    """Split locale list from config string into normalized codes."""

    values = [token.strip().lower() for token in re.split(r"[\s,]+", raw) if token.strip()]
    return values or ["en"]


def _catalog_signature(path: Path, locales: tuple[str, ...]) -> tuple[tuple[str, float], ...]:
    """Build lightweight signature for locale files based on mtimes."""

    signature: list[tuple[str, float]] = []
    for locale in locales:
        file_path = path / f"{locale}.yaml"
        if file_path.exists():
            signature.append((locale, file_path.stat().st_mtime))
        else:
            signature.append((locale, -1.0))
    return tuple(signature)


def _accept_language_candidates(header_value: str | None) -> list[str]:
    """Extract locale candidates from Accept-Language header."""

    if not header_value:
        return []

    candidates: list[str] = []
    for item in header_value.split(","):
        code = item.split(";")[0].strip().lower()
        if not code:
            continue
        candidates.append(code)
        if "-" in code:
            candidates.append(code.split("-", maxsplit=1)[0])
    return candidates


@dataclass(slots=True)
class I18nService:
    """Translation service loaded from locale YAML files."""

    catalogs: dict[str, dict[str, Any]]
    supported_locales: tuple[str, ...]
    default_locale: str

    def normalize_locale(self, raw_locale: str | None) -> str | None:
        """Normalize user locale input to one of supported locale codes."""

        if not raw_locale:
            return None

        lowered = raw_locale.strip().lower()
        if lowered in self.supported_locales:
            return lowered

        if "-" in lowered:
            short = lowered.split("-", maxsplit=1)[0]
            if short in self.supported_locales:
                return short

        return None

    def resolve_locale(self, request: Request) -> str:
        """Resolve locale from query/session/headers with default fallback.

        Precedence:
        1. `?lang=<code>` query parameter (also persisted to session)
        2. Session value
        3. `Accept-Language` header
        4. Configured default locale
        """

        requested = self.normalize_locale(request.query_params.get("lang"))
        if requested:
            request.session["locale"] = requested
            return requested

        session_locale = self.normalize_locale(request.session.get("locale"))
        if session_locale:
            return session_locale

        for candidate in _accept_language_candidates(request.headers.get("accept-language")):
            normalized = self.normalize_locale(candidate)
            if normalized:
                return normalized

        return self.default_locale

    def translate(self, locale: str, key: str, **kwargs: Any) -> str:
        """Translate key using locale catalog with default-locale fallback."""

        catalog = self.catalogs.get(locale, {})
        fallback_catalog = self.catalogs.get(self.default_locale, {})

        message = _deep_get(catalog, key)
        if message is None:
            message = _deep_get(fallback_catalog, key)
        if message is None:
            return key

        try:
            return message.format(**kwargs) if kwargs else message
        except Exception:
            return message

    def translator(self, locale: str):
        """Return a template-friendly translation function for one locale."""

        def _t(key: str, **kwargs: Any) -> str:
            return self.translate(locale, key, **kwargs)

        return _t

    def enum_label(self, locale: str, enum_group: str, raw_value: str) -> str:
        """Translate enum-style values using `<group>.<lower_value>` keys."""

        key = f"{enum_group}.{str(raw_value).lower()}"
        translated = self.translate(locale, key)
        return translated if translated != key else str(raw_value)


@lru_cache(maxsize=1)
def get_i18n_service() -> I18nService:
    """Load and cache i18n catalogs from configured path."""

    settings = get_settings()
    supported = tuple(_split_supported_locales(settings.supported_locales))

    default_locale = settings.default_locale.strip().lower() or "en"
    if default_locale not in supported:
        default_locale = supported[0]

    base_path = Path(settings.i18n_path)
    catalogs: dict[str, dict[str, Any]] = {}
    for locale in supported:
        catalogs[locale] = _load_yaml(base_path / f"{locale}.yaml")

    return I18nService(
        catalogs=catalogs,
        supported_locales=supported,
        default_locale=default_locale,
    )


def get_runtime_i18n_service() -> I18nService:
    """Return i18n service, reloading locale catalogs on changes in debug mode."""

    global _catalog_signature_cache

    settings = get_settings()
    if not settings.debug:
        return get_i18n_service()

    supported = tuple(_split_supported_locales(settings.supported_locales))
    signature = _catalog_signature(Path(settings.i18n_path), supported)
    if signature != _catalog_signature_cache:
        get_i18n_service.cache_clear()
        _catalog_signature_cache = signature

    return get_i18n_service()
