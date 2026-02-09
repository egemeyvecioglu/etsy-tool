"""Unit tests for admin bypass helper behavior."""

from dataclasses import dataclass

from app.admin_bypass import apply_admin_bypass_identity, filtered_mock_listing_rows, is_admin_bypass_user


@dataclass
class DummyUser:
    """Simple user-shaped object for helper tests."""

    id: int
    etsy_user_id: str | None = None
    etsy_shop_id: str | None = None


def test_apply_admin_bypass_identity_marks_user() -> None:
    """Applying bypass identity should make user detectable as bypass user."""

    user = DummyUser(id=7)
    apply_admin_bypass_identity(user)

    assert user.etsy_user_id == "admin_bypass_user_7"
    assert user.etsy_shop_id == "admin_bypass_shop_7"
    assert is_admin_bypass_user(user) is True


def test_filtered_mock_listing_rows_keyword_filter() -> None:
    """Mock listing filtering should match case-insensitive title keyword."""

    rows = filtered_mock_listing_rows("cotton")
    assert len(rows) == 2
    assert all("Cotton" in row["title"] for row in rows)
