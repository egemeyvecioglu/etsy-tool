"""Price transformation logic for Etsy variation offerings."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any

from app.schemas import AdjustmentConfig, AdjustmentDirection, AdjustmentType, JobOptions, RoundingMode


@dataclass(slots=True)
class PriceChange:
    """Represents one offering-level price change result."""

    old_price: Decimal
    new_price: Decimal
    changed: bool


def _quantize(value: Decimal, rounding_mode: RoundingMode) -> Decimal:
    """Round a decimal to two digits according to selected mode."""

    rounding = ROUND_HALF_EVEN if rounding_mode == RoundingMode.BANKERS else ROUND_HALF_UP
    return value.quantize(Decimal("0.01"), rounding=rounding)


def adjust_price(old_price: Decimal, adjustment: AdjustmentConfig, options: JobOptions) -> PriceChange:
    """Apply absolute/percent adjustment to a single price.

    Args:
        old_price: Existing offering price as Decimal.
        adjustment: Adjustment method and value.
        options: Job options containing rounding/floor settings.

    Returns:
        PriceChange with old/new value and changed flag.
    """

    sign = Decimal("1") if adjustment.direction == AdjustmentDirection.INCREASE else Decimal("-1")
    effective_value = adjustment.value * sign

    if adjustment.type == AdjustmentType.ABSOLUTE:
        proposed = old_price + effective_value
    else:
        proposed = old_price * (Decimal("1") + (effective_value / Decimal("100")))

    bounded = max(proposed, options.min_price)
    rounded = _quantize(bounded, options.rounding)
    changed = rounded != _quantize(old_price, options.rounding)
    return PriceChange(old_price=old_price, new_price=rounded, changed=changed)


def _parse_price_value(price_value: Any) -> Decimal:
    """Normalize Etsy price payload variants into Decimal dollars.

    Etsy payloads may represent price as:
    - decimal-like value ("12.34", 12.34)
    - object with amount/divisor ({"amount": 1234, "divisor": 100})
    """

    if isinstance(price_value, dict):
        amount = Decimal(str(price_value.get("amount", "0")))
        divisor = Decimal(str(price_value.get("divisor", "100")))
        if divisor == 0:
            raise ValueError("Price divisor cannot be zero")
        return amount / divisor
    return Decimal(str(price_value))


def _serialize_price(original_price: Any, new_price: Decimal) -> Any:
    """Convert a Decimal back into the original Etsy-compatible structure."""

    if isinstance(original_price, dict):
        divisor = int(original_price.get("divisor", 100)) or 100
        amount = int((new_price * Decimal(divisor)).to_integral_value(rounding=ROUND_HALF_UP))
        serialized = dict(original_price)
        serialized["amount"] = amount
        serialized["divisor"] = divisor
        return serialized
    return format(new_price, "f")


def apply_adjustment_to_inventory(
    inventory_payload: dict[str, Any],
    adjustment: AdjustmentConfig,
    options: JobOptions,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a job adjustment to each eligible offering in inventory payload.

    The output payload preserves all non-price fields to avoid unintended
    changes in variation properties, SKUs, quantities, and flags.

    Args:
        inventory_payload: Raw listing inventory JSON from Etsy.
        adjustment: Price adjustment instruction.
        options: Job options including dry-run/rounding/floors.

    Returns:
        Tuple of (updated_inventory_payload, summary_dict).
    """

    updated = deepcopy(inventory_payload)
    products = updated.get("products", [])

    total_offerings = 0
    changed_offerings = 0
    skipped_offerings = 0
    examples: list[dict[str, str]] = []

    for product in products:
        offerings = product.get("offerings", [])
        for offering in offerings:
            total_offerings += 1

            # Disabled offerings are left untouched to avoid accidental re-enables.
            if offering.get("is_enabled") is False:
                skipped_offerings += 1
                continue

            old_decimal = _parse_price_value(offering.get("price", "0"))
            change = adjust_price(old_decimal, adjustment, options)

            if not change.changed:
                skipped_offerings += 1
                continue

            if options.max_change_percent is not None and old_decimal > 0:
                pct_delta = ((change.new_price - old_decimal) / old_decimal) * Decimal("100")
                if abs(pct_delta) > abs(options.max_change_percent):
                    raise ValueError(
                        f"Calculated change {pct_delta:.2f}% exceeds max_change_percent "
                        f"{options.max_change_percent}%"
                    )

            offering["price"] = _serialize_price(offering.get("price", "0"), change.new_price)
            changed_offerings += 1

            # Keep a small sample for previews/reporting.
            if len(examples) < 5:
                examples.append(
                    {
                        "from": format(change.old_price, "f"),
                        "to": format(change.new_price, "f"),
                    }
                )

    summary = {
        "total_offerings": total_offerings,
        "changed_offerings": changed_offerings,
        "skipped_offerings": skipped_offerings,
        "examples": examples,
    }
    return updated, summary
