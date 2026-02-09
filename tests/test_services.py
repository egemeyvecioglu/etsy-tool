"""Unit tests for request conversion and selection parsing helpers."""

from decimal import Decimal
from typing import Generator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import JobStatus, User, UserStatus
from app.schemas import AdjustmentDirection, AdjustmentType, SelectionMode
from app.services import convert_form_to_job_request, create_price_adjust_job, parse_listing_ids_csv


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Create isolated in-memory DB session for service guardrail tests."""

    engine = create_engine("sqlite:///:memory:", future=True)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def _create_user(db: Session) -> User:
    """Insert one active test user."""

    user = User(
        status=UserStatus.ACTIVE.value,
        etsy_user_id="u-test-services",
        etsy_shop_id="s-test-services",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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


def test_create_price_adjust_job_allows_large_percent_increase(db_session: Session) -> None:
    """Large percent increase should be allowed by backend guardrail rules."""

    user = _create_user(db_session)
    payload = convert_form_to_job_request(
        {
            "selection_mode": "listing_ids",
            "listing_ids_csv": "111",
            "adjustment_direction": "increase",
            "adjustment_basis": "percentage",
            "adjustment_value": "200",
            "dry_run": "true",
        }
    )

    job = create_price_adjust_job(
        db=db_session,
        user=user,
        request_payload=payload,
        listing_ids=["111"],
    )

    assert job.status == JobStatus.QUEUED.value


def test_create_price_adjust_job_rejects_percent_decrease_above_100(db_session: Session) -> None:
    """Percent decrease over 100 should be rejected to avoid negative prices."""

    user = _create_user(db_session)
    payload = convert_form_to_job_request(
        {
            "selection_mode": "listing_ids",
            "listing_ids_csv": "111",
            "adjustment_direction": "decrease",
            "adjustment_basis": "percentage",
            "adjustment_value": "101",
            "dry_run": "true",
        }
    )

    with pytest.raises(HTTPException) as exc:
        create_price_adjust_job(
            db=db_session,
            user=user,
            request_payload=payload,
            listing_ids=["111"],
        )

    assert "cannot exceed 100%" in str(exc.value.detail).lower()
