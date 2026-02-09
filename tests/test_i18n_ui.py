"""Unit tests for i18n and schema-driven UI helpers."""

from app.i18n import get_i18n_service
from app.ui_schema import get_job_form_schema


def test_default_locale_is_english() -> None:
    """Configured default locale should be English unless overridden."""

    i18n = get_i18n_service()
    assert i18n.default_locale == "en"


def test_turkish_status_label_is_available() -> None:
    """Status translations should resolve for Turkish locale."""

    i18n = get_i18n_service()
    assert i18n.enum_label("tr", "statuses", "SUCCEEDED") == "Başarılı"


def test_job_form_schema_contains_expected_sections() -> None:
    """Schema loader should provide listing and adjustment sections."""

    schema = get_job_form_schema()
    section_ids = {section.id for section in schema.sections}
    assert {"listing_selection", "adjustment"}.issubset(section_ids)
    assert "options" not in section_ids


def test_job_form_schema_contains_visibility_rules() -> None:
    """Conditional field visibility rules should be available for dynamic UI behavior."""

    schema = get_job_form_schema()
    listing_section = next(section for section in schema.sections if section.id == "listing_selection")
    filter_field = next(field for field in listing_section.fields if field.name == "filter_keyword")
    ids_field = next(field for field in listing_section.fields if field.name == "listing_ids_csv")

    assert filter_field.visible_when == {"selection_mode": ["filter"]}
    assert ids_field.visible_when == {"selection_mode": ["listing_ids"]}
