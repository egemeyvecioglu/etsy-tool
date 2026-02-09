"""Unit tests for request conversion and selection parsing helpers."""

from decimal import Decimal

import pytest

from app.schemas import AdjustmentDirection, AdjustmentType, SelectionMode
from app.services import convert_form_to_job_request, parse_listing_ids_csv


def test_parse_listing_ids_csv_handles_commas_and_newlines() -> None:
    """CSV parser should normalize mixed separators into cleaned IDs."""

    parsed = parse_listing_ids_csv("123, 456\n789\n,  ")
    assert parsed == ["123", "456", "789"]


def test_convert_form_to_job_request_listing_ids_mode() -> None:
    """Form conversion should parse visible inputs and apply option defaults."""

    req = convert_form_to_job_request(
        {
            "selection_mode": "listing_ids",
            "listing_ids_csv": "111,222",
            "adjustment_direction": "decrease",
            "adjustment_basis": "percentage",
            "adjustment_value": "5",
            "dry_run": "true",
        }
    )

    assert req.selection.mode == SelectionMode.LISTING_IDS
    assert req.selection.listing_ids == ["111", "222"]
    assert req.adjustment.type == AdjustmentType.PERCENT
    assert req.adjustment.direction == AdjustmentDirection.DECREASE
    assert req.adjustment.value == Decimal("5")
    assert req.options.min_price == Decimal("0.01")
    assert req.options.rounding.value == "standard"
    assert req.options.dry_run is True
    assert req.options.max_change_percent is None


def test_convert_form_to_job_request_rejects_negative_adjustment_value() -> None:
    """Negative values should be rejected; direction determines sign semantics."""

    with pytest.raises(ValueError):
        convert_form_to_job_request(
            {
                "selection_mode": "all_active",
                "adjustment_direction": "increase",
                "adjustment_basis": "amount",
                "adjustment_value": "-1",
            }
        )
