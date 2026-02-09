"""Service-layer helpers for user/session, OAuth token, and job orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import get_token_cipher
from app.etsy_client import EtsyClient
from app.models import (
    AuditLog,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    ListingSyncState,
    OAuthToken,
    SyncedListing,
    User,
    UserStatus,
)
from app.schemas import PriceAdjustJobRequest, SelectionMode


def utcnow() -> datetime:
    """Return current UTC datetime with timezone."""

    return datetime.now(tz=timezone.utc)


def get_or_create_session_user(request: Request, db: Session) -> User:
    """Resolve current internal user from session cookie, creating one if missing."""

    session_user_id = request.session.get("user_id")
    user: User | None = None

    if session_user_id is not None:
        user = db.get(User, int(session_user_id))

    if user is None:
        user = User(status=UserStatus.ACTIVE.value)
        db.add(user)
        db.commit()
        db.refresh(user)
        request.session["user_id"] = user.id

    return user


def clear_session_user(request: Request) -> None:
    """Remove user association from browser session."""

    request.session.pop("user_id", None)
    request.session.pop("oauth_state", None)
    request.session.pop("oauth_code_verifier", None)


def _token_from_payload(payload: dict[str, Any], fallback_scopes: str) -> tuple[str, str, datetime, str]:
    """Extract normalized OAuth token fields from an Etsy token response."""

    access_token = str(payload.get("access_token", "")).strip()
    refresh_token = str(payload.get("refresh_token", "")).strip()
    if not access_token or not refresh_token:
        raise ValueError("Token response missing access_token or refresh_token")

    expires_in = int(payload.get("expires_in", 3600))
    scopes = str(payload.get("scope") or payload.get("scopes") or fallback_scopes)
    expires_at = utcnow() + timedelta(seconds=expires_in)

    return access_token, refresh_token, expires_at, scopes


def save_user_tokens(db: Session, user: User, token_payload: dict[str, Any], scopes_hint: str) -> OAuthToken:
    """Encrypt and persist OAuth tokens for a user."""

    access_token, refresh_token, expires_at, scopes = _token_from_payload(token_payload, scopes_hint)
    cipher = get_token_cipher()

    existing = db.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id))
    if existing is None:
        existing = OAuthToken(
            user_id=user.id,
            access_token_encrypted=cipher.encrypt(access_token),
            refresh_token_encrypted=cipher.encrypt(refresh_token),
            expires_at=expires_at,
            scopes=scopes,
        )
        db.add(existing)
    else:
        existing.access_token_encrypted = cipher.encrypt(access_token)
        existing.refresh_token_encrypted = cipher.encrypt(refresh_token)
        existing.expires_at = expires_at
        existing.scopes = scopes

    db.commit()
    db.refresh(existing)
    return existing


def _decrypt_tokens(token_row: OAuthToken) -> tuple[str, str]:
    """Decrypt access/refresh tokens from persistent storage."""

    cipher = get_token_cipher()
    return (
        cipher.decrypt(token_row.access_token_encrypted),
        cipher.decrypt(token_row.refresh_token_encrypted),
    )


def get_or_refresh_access_token(db: Session, user: User, client: EtsyClient) -> str:
    """Fetch a valid access token, refreshing and persisting when needed."""

    settings = get_settings()
    token_row = db.scalar(select(OAuthToken).where(OAuthToken.user_id == user.id))
    if token_row is None:
        raise HTTPException(status_code=400, detail="Etsy account is not connected")

    access_token, refresh_token = _decrypt_tokens(token_row)
    refresh_deadline = utcnow() + timedelta(seconds=settings.token_expiry_buffer_seconds)

    if token_row.expires_at > refresh_deadline:
        return access_token

    refreshed = client.refresh_access_token(refresh_token)
    updated = save_user_tokens(db, user, refreshed, token_row.scopes)
    fresh_access, _ = _decrypt_tokens(updated)
    return fresh_access


def add_audit_log(db: Session, user_id: int, action: str, job_id: int | None = None, payload: dict[str, Any] | None = None) -> None:
    """Insert an audit log row for sensitive actions."""

    db.add(AuditLog(user_id=user_id, job_id=job_id, action=action, payload=payload))
    db.commit()


def normalize_listing_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalize mixed Etsy/mock listing rows to stable `listing_id`/`title` pairs."""

    seen: set[str] = set()
    normalized: list[dict[str, str]] = []

    for row in raw_rows:
        raw_listing_id = row.get("listing_id") or row.get("listingId") or row.get("id")
        if raw_listing_id is None:
            continue

        listing_id = str(raw_listing_id).strip()
        if not listing_id or listing_id in seen:
            continue

        seen.add(listing_id)
        normalized.append(
            {
                "listing_id": listing_id,
                "title": str(row.get("title", "")).strip(),
            }
        )

    return normalized


def get_listing_sync_state(db: Session, user: User) -> ListingSyncState | None:
    """Return latest listings sync metadata for the current user."""

    return db.scalar(select(ListingSyncState).where(ListingSyncState.user_id == user.id))


def sync_user_listings_snapshot(
    db: Session,
    user: User,
    raw_rows: list[dict[str, Any]],
    *,
    source: str,
) -> ListingSyncState:
    """Replace user's listing cache with a fresh synchronized snapshot."""

    normalized_rows = normalize_listing_rows(raw_rows)
    synced_at = utcnow()

    db.execute(delete(SyncedListing).where(SyncedListing.user_id == user.id))
    for row in normalized_rows:
        db.add(
            SyncedListing(
                user_id=user.id,
                listing_id=row["listing_id"],
                title=row["title"],
                source=source,
                synced_at=synced_at,
            )
        )

    state = get_listing_sync_state(db, user)
    if state is None:
        state = ListingSyncState(
            user_id=user.id,
            listing_count=len(normalized_rows),
            source=source,
            last_synced_at=synced_at,
        )
    else:
        state.listing_count = len(normalized_rows)
        state.source = source
        state.last_synced_at = synced_at

    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def get_synced_listing_rows(
    db: Session,
    user: User,
    *,
    title_filter: str | None = None,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Load synced listings for a user with optional case-insensitive title filter."""

    stmt = select(SyncedListing).where(SyncedListing.user_id == user.id)

    normalized_filter = (title_filter or "").strip().lower()
    if normalized_filter:
        stmt = stmt.where(func.lower(SyncedListing.title).contains(normalized_filter))

    # Preserve original sync ordering so preview/selection stays stable.
    stmt = stmt.order_by(SyncedListing.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = db.scalars(stmt).all()
    return [
        {
            "listing_id": row.listing_id,
            "title": row.title,
        }
        for row in rows
    ]


def get_synced_listing_count(db: Session, user: User, *, title_filter: str | None = None) -> int:
    """Count synced listings for a user with optional title keyword filter."""

    stmt = select(func.count(SyncedListing.id)).where(SyncedListing.user_id == user.id)
    normalized_filter = (title_filter or "").strip().lower()
    if normalized_filter:
        stmt = stmt.where(func.lower(SyncedListing.title).contains(normalized_filter))
    return int(db.scalar(stmt) or 0)


def preview_listings_for_selection(
    db: Session,
    user: User,
    *,
    selection_mode: str,
    title_filter: str | None = None,
    listing_ids: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Build listing preview rows for create-job UI from synced snapshots only."""

    try:
        mode = SelectionMode(selection_mode)
    except ValueError:
        mode = SelectionMode.ALL_ACTIVE
    safe_limit = max(1, min(limit, 200))

    if mode == SelectionMode.LISTING_IDS:
        requested = [str(item).strip() for item in listing_ids or [] if str(item).strip()]

        # Keep request order while removing duplicates for predictable previews.
        seen: set[str] = set()
        ordered_ids: list[str] = []
        for listing_id in requested:
            if listing_id in seen:
                continue
            seen.add(listing_id)
            ordered_ids.append(listing_id)

        if not ordered_ids:
            return []

        synced = db.scalars(
            select(SyncedListing).where(
                SyncedListing.user_id == user.id,
                SyncedListing.listing_id.in_(ordered_ids),
            )
        ).all()
        title_by_id = {row.listing_id: row.title for row in synced}

        preview: list[dict[str, Any]] = []
        for listing_id in ordered_ids[:safe_limit]:
            in_sync = listing_id in title_by_id
            preview.append(
                {
                    "listing_id": listing_id,
                    "title": title_by_id.get(listing_id, ""),
                    "in_sync_snapshot": in_sync,
                }
            )
        return preview

    rows = get_synced_listing_rows(
        db,
        user,
        title_filter=title_filter if mode == SelectionMode.FILTER else None,
        limit=safe_limit,
    )
    return [
        {
            "listing_id": row["listing_id"],
            "title": row["title"],
            "in_sync_snapshot": True,
        }
        for row in rows
    ]


def resolve_listing_ids_for_selection(
    *,
    db: Session,
    user: User,
    request_payload: PriceAdjustJobRequest,
) -> list[str]:
    """Resolve concrete listing IDs from synced snapshots or explicit user input."""

    mode = request_payload.selection.mode

    if mode == SelectionMode.LISTING_IDS:
        seen: set[str] = set()
        unique: list[str] = []
        for item in request_payload.selection.listing_ids or []:
            listing_id = str(item)
            if listing_id in seen:
                continue
            seen.add(listing_id)
            unique.append(listing_id)
        return unique

    if get_listing_sync_state(db, user) is None:
        raise HTTPException(status_code=400, detail="No synced listings found. Click Sync Listings first.")

    rows = get_synced_listing_rows(
        db,
        user,
        title_filter=request_payload.selection.filter_keyword if mode == SelectionMode.FILTER else None,
    )
    return [row["listing_id"] for row in rows]


def create_price_adjust_job(
    *,
    db: Session,
    user: User,
    request_payload: PriceAdjustJobRequest,
    listing_ids: list[str],
) -> Job:
    """Create a queued price-adjustment job and pending job items."""

    if not listing_ids:
        raise HTTPException(status_code=400, detail="No listings matched the requested selection")

    settings = get_settings()
    if (
        request_payload.adjustment.type.value == "percent"
        and abs(request_payload.adjustment.value) > Decimal(str(settings.max_percentage_change))
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Percent adjustment exceeds configured guardrail "
                f"(+/-{settings.max_percentage_change}%)"
            ),
        )

    payload_dict = request_payload.model_dump(mode="json")
    payload_dict["resolved_listing_ids"] = listing_ids

    job = Job(
        user_id=user.id,
        type=JobType.PRICE_ADJUST.value,
        status=JobStatus.QUEUED.value,
        request_payload=payload_dict,
    )
    db.add(job)
    db.flush()

    for listing_id in listing_ids:
        db.add(
            JobItem(
                job_id=job.id,
                listing_id=str(listing_id),
                status=JobItemStatus.PENDING.value,
            )
        )

    db.commit()
    db.refresh(job)

    add_audit_log(
        db,
        user_id=user.id,
        job_id=job.id,
        action="JOB_CREATED",
        payload={
            "type": job.type,
            "item_count": len(listing_ids),
            "dry_run": request_payload.options.dry_run,
        },
    )

    return job


def build_job_summary(db: Session, job: Job) -> dict[str, Any]:
    """Build aggregate counters for job status API responses."""

    total_items = db.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id)) or 0
    succeeded = (
        db.scalar(
            select(func.count(JobItem.id)).where(
                JobItem.job_id == job.id,
                JobItem.status == JobItemStatus.SUCCEEDED.value,
            )
        )
        or 0
    )
    failed = (
        db.scalar(
            select(func.count(JobItem.id)).where(
                JobItem.job_id == job.id,
                JobItem.status == JobItemStatus.FAILED.value,
            )
        )
        or 0
    )
    skipped = (
        db.scalar(
            select(func.count(JobItem.id)).where(
                JobItem.job_id == job.id,
                JobItem.status == JobItemStatus.SKIPPED.value,
            )
        )
        or 0
    )

    completed = succeeded + failed + skipped

    return {
        "id": job.id,
        "status": job.status,
        "type": job.type,
        "total_items": int(total_items),
        "completed_items": int(completed),
        "succeeded_items": int(succeeded),
        "failed_items": int(failed),
        "skipped_items": int(skipped),
        "cancel_requested": bool(job.cancel_requested),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def serialize_job_items(items: list[JobItem]) -> list[dict[str, Any]]:
    """Serialize ORM job items into API-friendly dictionaries."""

    return [
        {
            "id": item.id,
            "listing_id": item.listing_id,
            "status": item.status,
            "before_summary": item.before_summary,
            "after_summary": item.after_summary,
            "error": item.error,
            "updated_at": item.updated_at.isoformat(),
        }
        for item in items
    ]


def parse_listing_ids_csv(raw_value: str) -> list[str]:
    """Convert comma/newline separated IDs from UI into normalized list."""

    chunks = [piece.strip() for piece in raw_value.replace("\n", ",").split(",")]
    return [chunk for chunk in chunks if chunk]


def convert_form_to_job_request(form_data: dict[str, Any]) -> PriceAdjustJobRequest:
    """Convert server-rendered form inputs into validated job request schema."""

    selection_mode = str(form_data.get("selection_mode", "all_active"))
    selection_payload: dict[str, Any] = {"mode": selection_mode}

    if selection_mode == SelectionMode.FILTER.value:
        selection_payload["filter_keyword"] = str(form_data.get("filter_keyword", "")).strip()
    elif selection_mode == SelectionMode.LISTING_IDS.value:
        selection_payload["listing_ids"] = parse_listing_ids_csv(str(form_data.get("listing_ids_csv", "")))

    options_payload = {
        # Server-rendered form intentionally exposes only `dry_run`.
        # Other option fields remain available through API payloads and
        # fall back to schema defaults here.
        "dry_run": str(form_data.get("dry_run", "false")).lower() in {"true", "on", "1", "yes"},
    }

    direction = str(form_data.get("adjustment_direction", "increase")).strip().lower()
    basis_raw = str(
        form_data.get(
            "adjustment_basis",
            form_data.get("adjustment_type", "absolute"),
        )
    ).strip().lower()
    type_by_basis = {
        "amount": "absolute",
        "absolute": "absolute",
        "percentage": "percent",
        "percent": "percent",
    }
    normalized_type = type_by_basis.get(basis_raw, basis_raw)

    payload = {
        "selection": selection_payload,
        "adjustment": {
            "type": normalized_type,
            "direction": direction,
            "value": str(form_data.get("adjustment_value", "0")),
        },
        "options": options_payload,
    }

    return PriceAdjustJobRequest.model_validate(payload)


def ensure_etsy_connection(user: User) -> None:
    """Guard endpoint access to users with linked Etsy OAuth/shop context."""

    if not user.etsy_user_id or not user.etsy_shop_id:
        raise HTTPException(status_code=400, detail="Connect your Etsy account first")
