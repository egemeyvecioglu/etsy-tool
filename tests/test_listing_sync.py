"""Unit tests for listings sync snapshot behavior."""

from __future__ import annotations

from datetime import timedelta
from typing import Generator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import User, UserStatus
from app.schemas import PriceAdjustJobRequest
from app.services import (
    get_listing_sync_state,
    get_synced_listing_rows,
    is_listing_sync_stale,
    preview_listings_for_selection,
    resolve_listing_ids_for_selection,
    sync_user_listings_snapshot,
    utcnow,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Create isolated in-memory DB session for listing sync tests."""

    engine = create_engine("sqlite:///:memory:", future=True)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def _create_user(db: Session) -> User:
    """Insert one Etsy-connected test user."""

    user = User(
        status=UserStatus.ACTIVE.value,
        etsy_user_id="u-test",
        etsy_shop_id="s-test",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_sync_snapshot_replaces_previous_rows(db_session: Session) -> None:
    """Sync operation should deduplicate listing IDs and replace old cache rows."""

    user = _create_user(db_session)

    state = sync_user_listings_snapshot(
        db_session,
        user,
        [
            {"listing_id": "100", "title": "Cotton Tee"},
            {"listing_id": "100", "title": "Cotton Tee Duplicate"},
            {"listing_id": "200", "title": "Linen Scarf"},
        ],
        source="admin_bypass",
    )
    assert state.listing_count == 2

    first_rows = get_synced_listing_rows(db_session, user)
    assert [row["listing_id"] for row in first_rows] == ["100", "200"]

    second_state = sync_user_listings_snapshot(
        db_session,
        user,
        [{"listing_id": "300", "title": "Ceramic Mug"}],
        source="admin_bypass",
    )
    assert second_state.listing_count == 1

    second_rows = get_synced_listing_rows(db_session, user)
    assert [row["listing_id"] for row in second_rows] == ["300"]


def test_resolve_listing_ids_requires_sync_for_all_active(db_session: Session) -> None:
    """All-active selection should fail until user performs listings sync."""

    user = _create_user(db_session)
    payload = PriceAdjustJobRequest.model_validate(
        {
            "selection": {"mode": "all_active"},
            "adjustment": {"type": "absolute", "direction": "increase", "value": "2"},
            "options": {"dry_run": True},
        }
    )

    with pytest.raises(HTTPException) as exc:
        resolve_listing_ids_for_selection(db=db_session, user=user, request_payload=payload)

    assert "No synced listings found" in str(exc.value.detail)


def test_filter_selection_resolves_from_synced_snapshot(db_session: Session) -> None:
    """Keyword-filter selection should resolve IDs from synced listing cache."""

    user = _create_user(db_session)
    sync_user_listings_snapshot(
        db_session,
        user,
        [
            {"listing_id": "100", "title": "Cotton Tee"},
            {"listing_id": "200", "title": "Linen Scarf"},
            {"listing_id": "300", "title": "Cotton Socks"},
        ],
        source="etsy",
    )

    payload = PriceAdjustJobRequest.model_validate(
        {
            "selection": {"mode": "filter", "filter_keyword": "cotton"},
            "adjustment": {"type": "absolute", "direction": "increase", "value": "2"},
            "options": {"dry_run": True},
        }
    )
    listing_ids = resolve_listing_ids_for_selection(db=db_session, user=user, request_payload=payload)
    assert listing_ids == ["100", "300"]


def test_listing_id_preview_marks_rows_missing_from_sync_snapshot(db_session: Session) -> None:
    """Manual listing-id preview should mark IDs absent in synced snapshot."""

    user = _create_user(db_session)
    sync_user_listings_snapshot(
        db_session,
        user,
        [
            {"listing_id": "100", "title": "Cotton Tee"},
            {"listing_id": "200", "title": "Linen Scarf"},
        ],
        source="etsy",
    )
    state = get_listing_sync_state(db_session, user)
    assert state is not None

    rows = preview_listings_for_selection(
        db_session,
        user,
        selection_mode="listing_ids",
        listing_ids=["100", "999", "100"],
        limit=10,
    )
    assert len(rows) == 2
    assert rows[0]["listing_id"] == "100"
    assert rows[0]["in_sync_snapshot"] is True
    assert rows[1]["listing_id"] == "999"
    assert rows[1]["in_sync_snapshot"] is False


def test_stale_listing_sync_is_detected() -> None:
    """Listing sync older than policy window should be treated as stale."""

    stale_timestamp = utcnow() - timedelta(hours=7)
    assert is_listing_sync_stale(stale_timestamp) is True


def test_resolve_listing_ids_rejects_stale_sync_for_all_active(db_session: Session) -> None:
    """All-active selection should fail when synced snapshot is stale."""

    user = _create_user(db_session)
    state = sync_user_listings_snapshot(
        db_session,
        user,
        [{"listing_id": "100", "title": "Cotton Tee"}],
        source="etsy",
    )
    state.last_synced_at = utcnow() - timedelta(hours=7)
    db_session.add(state)
    db_session.commit()

    payload = PriceAdjustJobRequest.model_validate(
        {
            "selection": {"mode": "all_active"},
            "adjustment": {"type": "absolute", "direction": "increase", "value": "2"},
            "options": {"dry_run": True},
        }
    )

    with pytest.raises(HTTPException) as exc:
        resolve_listing_ids_for_selection(db=db_session, user=user, request_payload=payload)

    assert "stale" in str(exc.value.detail).lower()
