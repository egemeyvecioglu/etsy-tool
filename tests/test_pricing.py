"""Unit tests for pricing transformation behavior."""

from decimal import Decimal

from app.pricing import adjust_price, apply_adjustment_to_inventory
from app.schemas import AdjustmentConfig, AdjustmentDirection, AdjustmentType, JobOptions, RoundingMode


def test_adjust_price_absolute_with_floor() -> None:
    """Absolute decrease should honor minimum price floor."""

    change = adjust_price(
        old_price=Decimal("1.00"),
        adjustment=AdjustmentConfig(
            type=AdjustmentType.ABSOLUTE,
            direction=AdjustmentDirection.DECREASE,
            value=Decimal("2.00"),
        ),
        options=JobOptions(min_price=Decimal("0.25")),
    )

    assert change.new_price == Decimal("0.25")
    assert change.changed is True


def test_adjust_price_percent_standard_rounding() -> None:
    """Percent mode should round with half-up strategy by default."""

    change = adjust_price(
        old_price=Decimal("10.00"),
        adjustment=AdjustmentConfig(
            type=AdjustmentType.PERCENT,
            direction=AdjustmentDirection.INCREASE,
            value=Decimal("5"),
        ),
        options=JobOptions(rounding=RoundingMode.STANDARD),
    )

    assert change.new_price == Decimal("10.50")


def test_apply_adjustment_to_inventory_keeps_disabled_offering() -> None:
    """Disabled offerings should be skipped and remain unchanged."""

    inventory = {
        "products": [
            {
                "offerings": [
                    {"price": {"amount": 1000, "divisor": 100}, "is_enabled": True},
                    {"price": {"amount": 2000, "divisor": 100}, "is_enabled": False},
                ]
            }
        ]
    }

    updated, summary = apply_adjustment_to_inventory(
        inventory,
        AdjustmentConfig(
            type=AdjustmentType.ABSOLUTE,
            direction=AdjustmentDirection.INCREASE,
            value=Decimal("2.00"),
        ),
        JobOptions(),
    )

    assert summary["total_offerings"] == 2
    assert summary["changed_offerings"] == 1
    assert summary["skipped_offerings"] == 1
    assert updated["products"][0]["offerings"][0]["price"]["amount"] == 1200
    assert updated["products"][0]["offerings"][1]["price"]["amount"] == 2000
