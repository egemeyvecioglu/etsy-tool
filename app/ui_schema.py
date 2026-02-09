"""YAML-backed UI schema for rendering flexible job forms.

The job creation page reads this schema at runtime so new form fields or
sections can be introduced through configuration updates with minimal template
changes, as long as field type is supported.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app.config import get_settings


class FormOption(BaseModel):
    """One selectable option for radio/select input fields."""

    value: str
    label_key: str


class FormField(BaseModel):
    """Generic UI field definition for schema-driven rendering."""

    type: Literal["radio_group", "text", "textarea", "select", "number", "checkbox"]
    name: str
    label_key: str | None = None
    placeholder_key: str | None = None
    help_key: str | None = None
    default: str | bool | None = None
    required: bool = False
    options: list[FormOption] = Field(default_factory=list)
    visible_when: dict[str, list[str]] = Field(default_factory=dict)
    rows: int | None = None
    min: str | None = None
    max: str | None = None
    step: str | None = None
    checked_value: str = "true"


class FormSection(BaseModel):
    """A logical section in the UI form."""

    id: str
    title_key: str
    fields: list[FormField] = Field(default_factory=list)


class JobFormSchema(BaseModel):
    """Root schema for the bulk price update form."""

    sections: list[FormSection] = Field(default_factory=list)


_DEFAULT_SCHEMA = {
    "sections": [
        {
            "id": "listing_selection",
            "title_key": "pages.create_job.sections.listing_selection",
            "fields": [
                {
                    "type": "radio_group",
                    "name": "selection_mode",
                    "default": "all_active",
                    "options": [
                        {
                            "value": "all_active",
                            "label_key": "pages.create_job.fields.selection_mode.all_active",
                        },
                        {
                            "value": "filter",
                            "label_key": "pages.create_job.fields.selection_mode.filter",
                        },
                        {
                            "value": "listing_ids",
                            "label_key": "pages.create_job.fields.selection_mode.listing_ids",
                        },
                    ],
                },
                {
                    "type": "text",
                    "name": "filter_keyword",
                    "label_key": "pages.create_job.fields.filter_keyword.label",
                    "placeholder_key": "pages.create_job.fields.filter_keyword.placeholder",
                    "visible_when": {"selection_mode": ["filter"]},
                },
                {
                    "type": "textarea",
                    "name": "listing_ids_csv",
                    "label_key": "pages.create_job.fields.listing_ids_csv.label",
                    "placeholder_key": "pages.create_job.fields.listing_ids_csv.placeholder",
                    "rows": 4,
                    "visible_when": {"selection_mode": ["listing_ids"]},
                },
            ],
        },
        {
            "id": "adjustment",
            "title_key": "pages.create_job.sections.adjustment",
            "fields": [
                {
                    "type": "radio_group",
                    "name": "adjustment_direction",
                    "label_key": "pages.create_job.fields.adjustment_direction.label",
                    "default": "increase",
                    "options": [
                        {
                            "value": "increase",
                            "label_key": "pages.create_job.fields.adjustment_direction.increase",
                        },
                        {
                            "value": "decrease",
                            "label_key": "pages.create_job.fields.adjustment_direction.decrease",
                        },
                    ],
                },
                {
                    "type": "radio_group",
                    "name": "adjustment_basis",
                    "label_key": "pages.create_job.fields.adjustment_basis.label",
                    "default": "amount",
                    "options": [
                        {
                            "value": "amount",
                            "label_key": "pages.create_job.fields.adjustment_basis.amount",
                        },
                        {
                            "value": "percentage",
                            "label_key": "pages.create_job.fields.adjustment_basis.percentage",
                        },
                    ],
                },
                {
                    "type": "number",
                    "name": "adjustment_value",
                    "label_key": "pages.create_job.fields.adjustment_value.label",
                    "step": "0.01",
                    "min": "0",
                    "required": True,
                },
                {
                    "type": "checkbox",
                    "name": "dry_run",
                    "label_key": "pages.create_job.fields.dry_run.label",
                    "checked_value": "true",
                },
            ],
        },
    ]
}


def _load_yaml_schema(path: Path) -> dict:
    """Load YAML schema from disk and validate top-level type."""

    if not path.exists():
        return {}

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Job form schema must be a mapping: {path}")
    return payload


@lru_cache(maxsize=1)
def get_job_form_schema() -> JobFormSchema:
    """Load and cache form schema from configured YAML path."""

    settings = get_settings()
    schema_path = Path(settings.job_form_schema_path)
    payload = _load_yaml_schema(schema_path) or _DEFAULT_SCHEMA
    return JobFormSchema.model_validate(payload)
