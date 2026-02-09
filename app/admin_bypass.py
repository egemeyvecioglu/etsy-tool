"""Admin bypass helpers for local testing without Etsy OAuth approval.

This module intentionally provides deterministic mock listing/inventory data so
UI and job workflows can be exercised end-to-end in development environments.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


ADMIN_BYPASS_USER_PREFIX = "admin_bypass_user_"
ADMIN_BYPASS_SHOP_PREFIX = "admin_bypass_shop_"


def is_admin_bypass_user(user: Any) -> bool:
    """Return `True` when a user is marked as an admin bypass test user."""

    etsy_user_id = getattr(user, "etsy_user_id", None)
    return isinstance(etsy_user_id, str) and etsy_user_id.startswith(ADMIN_BYPASS_USER_PREFIX)


def apply_admin_bypass_identity(user: Any) -> None:
    """Assign synthetic Etsy identifiers to a user for testing purposes."""

    user_id = getattr(user, "id", "unknown")
    user.etsy_user_id = f"{ADMIN_BYPASS_USER_PREFIX}{user_id}"
    user.etsy_shop_id = f"{ADMIN_BYPASS_SHOP_PREFIX}{user_id}"


def mock_listing_rows() -> list[dict[str, str]]:
    """Return deterministic fake listings for selection/listing APIs."""

    return [
        {"listing_id": "TEST-1001", "title": "Cotton Shirt - Blue"},
        {"listing_id": "TEST-1002", "title": "Cotton Shirt - Black"},
        {"listing_id": "TEST-1003", "title": "Canvas Tote Bag"},
        {"listing_id": "TEST-1004", "title": "Handmade Ceramic Mug"},
        {"listing_id": "TEST-1005", "title": "Wool Scarf - Winter"},
        {"listing_id": "TEST-1006", "title": "Linen Table Runner"},
    ]


def filtered_mock_listing_rows(title_filter: str | None = None) -> list[dict[str, str]]:
    """Filter mock listings by title keyword using case-insensitive match."""

    rows = mock_listing_rows()
    if not title_filter:
        return rows

    lowered = title_filter.lower()
    return [row for row in rows if lowered in row["title"].lower()]


def _base_price_for_listing(listing_id: str) -> Decimal:
    """Generate a stable base price from listing ID text."""

    checksum = sum(ord(ch) for ch in listing_id)
    # Yields prices roughly between 9.00 and 22.00.
    return Decimal("9.00") + Decimal(checksum % 14)


def mock_inventory_payload(listing_id: str) -> dict[str, Any]:
    """Build deterministic mock inventory payload in Etsy-like structure."""

    base_price = _base_price_for_listing(listing_id)
    second_price = base_price + Decimal("3.50")

    return {
        "products": [
            {
                "sku": f"{listing_id}-A",
                "offerings": [
                    {
                        "price": {"amount": int(base_price * 100), "divisor": 100},
                        "quantity": 8,
                        "is_enabled": True,
                    },
                    {
                        "price": {"amount": int(second_price * 100), "divisor": 100},
                        "quantity": 5,
                        "is_enabled": True,
                    },
                ],
            }
        ]
    }
