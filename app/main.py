"""FastAPI entry point for the Etsy bulk variation price update web app."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth_helpers import build_code_challenge, generate_code_verifier, generate_state_token
from app.admin_bypass import apply_admin_bypass_identity, filtered_mock_listing_rows, is_admin_bypass_user
from app.config import get_settings
from app.db import SessionLocal, create_database_schema, get_db
from app.etsy_client import EtsyAPIError, EtsyClient
from app.i18n import I18nService, get_runtime_i18n_service
from app.models import Job, JobItem, JobItemStatus, JobStatus, User
from app.schemas import JobCreateResponse, JobSummaryResponse, PriceAdjustJobRequest, SelectionMode
from app.services import (
    add_audit_log,
    build_job_summary,
    clear_session_user,
    convert_form_to_job_request,
    create_price_adjust_job,
    disconnect_etsy_account,
    ensure_etsy_connection,
    get_listing_sync_state,
    get_or_create_session_user,
    get_or_refresh_access_token,
    get_synced_listing_count,
    get_synced_listing_rows,
    has_current_legal_acceptance,
    is_listing_sync_stale,
    parse_listing_ids_csv,
    preview_listings_for_selection,
    record_legal_acceptance,
    resolve_listing_ids_for_selection,
    save_user_tokens,
    serialize_job_items,
    sync_user_listings_snapshot,
)
from app.ui_schema import get_job_form_schema
from app.worker import JobWorker


settings = get_settings()


class _I18nRuntimeProxy:
    """Proxy that resolves latest i18n catalog state at access time."""

    def __getattr__(self, name: str):
        return getattr(get_runtime_i18n_service(), name)


i18n_service: I18nService = cast(I18nService, _I18nRuntimeProxy())
templates = Jinja2Templates(directory="app/templates")
worker = JobWorker(SessionLocal)

# Visual markers for language picker UI.
LANGUAGE_ICON_BY_CODE = {
    "en": "🇬🇧",
    "tr": "🇹🇷",
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize schema and start/stop worker alongside app lifecycle."""

    create_database_schema()
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency resolving the current cookie-backed internal user."""

    return get_or_create_session_user(request, db)


def _job_owned_by_user_or_404(db: Session, job_id: int, user: User) -> Job:
    """Fetch a job and enforce per-user ownership boundary."""

    job = db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _safe_next_path(raw_next: str | None) -> str:
    """Allow only local relative paths for redirect `next` parameter."""

    if not raw_next:
        return "/"

    decoded = unquote(raw_next)
    if not decoded.startswith("/") or decoded.startswith("//"):
        return "/"
    return decoded


def _enum_label(locale: str, enum_group: str, raw_value: str) -> str:
    """Translate enum value using i18n catalogs with fallback to raw string."""

    return i18n_service.enum_label(locale, enum_group, raw_value)


def _template_base_context(request: Request, *, locale: str | None = None) -> dict[str, Any]:
    """Build common template context including translator and locale switch links."""

    resolved_locale = locale or i18n_service.resolve_locale(request)
    t = i18n_service.translator(resolved_locale)

    current_path = request.url.path
    if request.url.query:
        current_path = f"{current_path}?{request.url.query}"

    encoded_next = quote(current_path, safe="")
    language_links = []
    for code in i18n_service.supported_locales:
        language_links.append(
            {
                "code": code,
                "label": t(f"languages.{code}"),
                "icon": LANGUAGE_ICON_BY_CODE.get(code, code.upper()),
                "url": f"/language/{code}?next={encoded_next}",
                "active": code == resolved_locale,
            }
        )

    # Support one-time server-side flash message keys (consumed on first render).
    query_message_key = request.query_params.get("message_key")
    session_message_key = request.session.pop("flash_message_key", None)
    resolved_message_key = query_message_key if query_message_key else session_message_key

    return {
        "request": request,
        "t": t,
        "current_path": request.url.path,
        "current_locale": resolved_locale,
        "current_language_icon": LANGUAGE_ICON_BY_CODE.get(resolved_locale, resolved_locale.upper()),
        "language_links": language_links,
        "status_label": lambda value: _enum_label(resolved_locale, "statuses", str(value)),
        "job_type_label": lambda value: _enum_label(resolved_locale, "job_types", str(value)),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        "message_key": resolved_message_key,
        "error_key": request.query_params.get("error_key"),
        "support_email": settings.support_email,
        "legal_terms_version": settings.legal_terms_version,
        "legal_privacy_version": settings.legal_privacy_version,
    }


def _render_template(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    locale: str | None = None,
):
    """Render a Jinja template with shared i18n/base context."""

    payload = _template_base_context(request, locale=locale)
    if context:
        payload.update(context)

    user_obj = payload.get("user")
    has_local_session = request.session.get("user_id") is not None
    payload["show_sign_out"] = bool(user_obj) and has_local_session and request.url.path != "/"
    payload["show_admin_bypass"] = bool(settings.allow_admin_bypass_login)
    return templates.TemplateResponse(template_name, payload)


def _age_label_for_sync(last_synced_at: datetime | None, t) -> str:
    """Render coarse relative time for listing sync warning UI text."""

    if last_synced_at is None:
        return t("pages.create_job.sync.never")

    normalized_last_synced_at = _normalize_datetime_utc(last_synced_at)
    if normalized_last_synced_at is None:
        return t("pages.create_job.sync.never")

    now = datetime.now(tz=timezone.utc)
    delta_seconds = max(0, int((now - normalized_last_synced_at).total_seconds()))

    if delta_seconds < 60:
        return t("pages.create_job.sync.just_now")

    if delta_seconds < 3600:
        minutes = delta_seconds // 60
        return t("pages.create_job.sync.minutes_ago", count=minutes)

    if delta_seconds < 86400:
        hours = delta_seconds // 3600
        return t("pages.create_job.sync.hours_ago", count=hours)

    days = delta_seconds // 86400
    return t("pages.create_job.sync.days_ago", count=days)


def _format_sync_timestamp(value: datetime | None) -> tuple[str, str]:
    """Return stable ISO + display strings for sync timestamps.

    SQLite may return naive datetimes even for timezone-aware columns, so
    values are normalized to UTC before formatting.
    """

    normalized_value = _normalize_datetime_utc(value)
    if normalized_value is None:
        return "", ""

    return (
        normalized_value.isoformat(),
        normalized_value.strftime("%Y-%m-%d %H:%M UTC"),
    )


def _normalize_datetime_utc(value: datetime | str | None) -> datetime | None:
    """Normalize datetime-like values to timezone-aware UTC datetime."""

    if value is None:
        return None

    parsed = value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None

        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None

    if not isinstance(parsed, datetime):
        return None

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime_for_ui(value: datetime | str | None, locale: str) -> str:
    """Format datetimes for dashboard-friendly display by locale."""

    normalized = _normalize_datetime_utc(value)
    if normalized is None:
        return str(value) if isinstance(value, str) else ""

    if locale == "tr":
        return normalized.strftime("%d.%m.%Y %H:%M UTC")
    return normalized.strftime("%b %d, %Y %H:%M UTC")


def _oauth_popup_result_response(
    request: Request,
    *,
    success: bool,
    error_message: str | None = None,
):
    """Render popup completion page that notifies opener and closes itself."""

    locale = i18n_service.resolve_locale(request)
    return _render_template(
        request,
        "oauth_popup_done.html",
        {
            "success": success,
            "error_message": error_message,
            "page_title_key": "pages.oauth_popup.title",
        },
        locale=locale,
    )


@app.get("/language/{locale_code}")
def set_language(locale_code: str, request: Request):
    """Persist selected UI language in session and redirect back."""

    normalized = i18n_service.normalize_locale(locale_code)
    if normalized is not None:
        request.session["locale"] = normalized

    next_path = _safe_next_path(request.query_params.get("next"))
    return RedirectResponse(next_path, status_code=303)


@app.get("/legal/terms")
def legal_terms(request: Request):
    """Render application terms page required for Etsy API usage."""

    return _render_template(
        request,
        "legal_terms.html",
        {
            "page_title_key": "pages.legal.terms_title",
        },
    )


@app.get("/legal/privacy")
def legal_privacy(request: Request):
    """Render application privacy policy page required for Etsy API usage."""

    return _render_template(
        request,
        "legal_privacy.html",
        {
            "page_title_key": "pages.legal.privacy_title",
        },
    )


@app.get("/")
def index(request: Request, user: User = Depends(current_user)):
    """Render landing page with Etsy connection action."""

    connected = bool(user.etsy_user_id and user.etsy_shop_id)
    return _render_template(
        request,
        "index.html",
        {
            "user": user,
            "connected": connected,
            "page_title_key": "pages.index.title",
        },
    )


@app.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Render dashboard with linked shop info and recent job history."""

    locale = i18n_service.resolve_locale(request)
    jobs = db.scalars(select(Job).where(Job.user_id == user.id).order_by(Job.created_at.desc()).limit(50)).all()
    job_summaries = [build_job_summary(db, job) for job in jobs]
    for summary in job_summaries:
        summary["created_at_display"] = _format_datetime_for_ui(summary.get("created_at"), locale)

    connected = bool(user.etsy_user_id and user.etsy_shop_id)

    return _render_template(
        request,
        "dashboard.html",
        {
            "user": user,
            "connected": connected,
            "job_summaries": job_summaries,
            "page_title_key": "pages.dashboard.title",
        },
        locale=locale,
    )


@app.get("/jobs/new")
def create_job_page(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Render schema-driven job creation form page."""

    locale = i18n_service.resolve_locale(request)
    translator = i18n_service.translator(locale)
    connected = bool(user.etsy_user_id and user.etsy_shop_id)
    form_schema = get_job_form_schema().model_dump(mode="json")
    sync_state = get_listing_sync_state(db, user) if connected else None
    sync_is_stale = bool(sync_state and is_listing_sync_stale(sync_state.last_synced_at))
    preview_rows = (
        preview_listings_for_selection(
            db,
            user,
            selection_mode=SelectionMode.ALL_ACTIVE.value,
            limit=8,
        )
        if sync_state is not None and not sync_is_stale
        else []
    )
    sync_age = _age_label_for_sync(sync_state.last_synced_at if sync_state else None, translator)
    sync_iso, sync_display = _format_sync_timestamp(sync_state.last_synced_at if sync_state else None)
    sync_confirm_message = (
        translator("pages.create_job.sync.confirm_template", age=sync_age)
        if connected and sync_state is not None and not sync_is_stale
        else ""
    )

    return _render_template(
        request,
        "create_job.html",
        {
            "user": user,
            "connected": connected,
            "job_form_schema": form_schema,
            "listing_sync": {
                "has_synced": sync_state is not None and not sync_is_stale,
                "is_stale": sync_is_stale,
                "last_synced_at_iso": sync_iso,
                "last_synced_display": sync_display,
                "last_synced_age": sync_age,
                "listing_count": int(sync_state.listing_count) if sync_state else 0,
                "max_age_hours": int(settings.listing_cache_max_age_hours),
            },
            "synced_preview_rows": preview_rows,
            "sync_confirm_message": sync_confirm_message,
            "page_title_key": "pages.create_job.title",
        },
        locale=locale,
    )


@app.post("/jobs/new")
async def create_job_from_form(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Handle server-rendered form submission and queue a job."""

    if not (user.etsy_user_id and user.etsy_shop_id):
        return RedirectResponse("/?error_key=flash.connect_first", status_code=303)

    form = await request.form()
    try:
        job_request = convert_form_to_job_request(dict(form))
    except Exception as exc:
        return RedirectResponse(f"/jobs/new?error={str(exc)}", status_code=303)

    try:
        listing_ids = resolve_listing_ids_for_selection(
            db=db,
            user=user,
            request_payload=job_request,
        )
        job = create_price_adjust_job(
            db=db,
            user=user,
            request_payload=job_request,
            listing_ids=listing_ids,
        )
    except Exception as exc:
        return RedirectResponse(f"/jobs/new?error={str(exc)}", status_code=303)

    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}")
def job_detail(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Render job detail page with live-polling progress data."""

    locale = i18n_service.resolve_locale(request)
    translator = i18n_service.translator(locale)

    job = _job_owned_by_user_or_404(db, job_id, user)
    summary = build_job_summary(db, job)
    items = db.scalars(
        select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.updated_at.desc()).limit(200)
    ).all()

    status_values = {status.value for status in JobStatus} | {status.value for status in JobItemStatus}
    status_labels = {
        value: i18n_service.enum_label(locale, "statuses", value)
        for value in status_values
    }

    return _render_template(
        request,
        "job_detail.html",
        {
            "user": user,
            "job": job,
            "summary": summary,
            "items": items,
            "status_labels": status_labels,
            "js_progress_template": translator("pages.job_detail.progress_template"),
            "js_counts_template": translator("pages.job_detail.counts_template"),
            "js_not_available": translator("common.not_available"),
            "page_title_key": "pages.job_detail.title_prefix",
        },
        locale=locale,
    )


@app.get("/auth/etsy/start")
def start_etsy_auth(
    request: Request,
    popup: bool = False,
    accept_terms: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Start Etsy OAuth Authorization Code flow with PKCE."""

    has_acceptance = has_current_legal_acceptance(db, user)
    if accept_terms and not has_acceptance:
        acceptance = record_legal_acceptance(db, user)
        add_audit_log(
            db,
            user_id=user.id,
            action="LEGAL_ACCEPTED",
            payload={
                "terms_version": acceptance.terms_version,
                "privacy_version": acceptance.privacy_version,
                "accepted_at": acceptance.accepted_at.isoformat(),
            },
        )
    elif not has_acceptance:
        next_path = "/?error_key=flash.accept_terms_required"
        return RedirectResponse(next_path, status_code=303)

    if not settings.etsy_client_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing OAuth client ID configuration "
                "(ETSY_TOOL_OAUTH_CLIENT_ID)"
            ),
        )

    code_verifier = generate_code_verifier()
    code_challenge = build_code_challenge(code_verifier)
    state = generate_state_token()

    request.session["oauth_state"] = state
    request.session["oauth_code_verifier"] = code_verifier
    request.session["oauth_user_id"] = user.id
    request.session["oauth_popup_mode"] = bool(popup)

    client = EtsyClient()
    redirect_url = client.build_authorization_url(state, code_challenge)
    client.close()

    return RedirectResponse(redirect_url, status_code=302)


@app.get("/oauth/etsy/callback")
def etsy_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """Handle Etsy OAuth callback and persist encrypted tokens."""

    popup_mode = bool(request.session.get("oauth_popup_mode"))

    if error:
        request.session.pop("oauth_popup_mode", None)
        if popup_mode:
            return _oauth_popup_result_response(
                request,
                success=False,
                error_message=f"OAuth failed: {error}",
            )
        return RedirectResponse(f"/?error=OAuth failed: {error}", status_code=303)

    expected_state = request.session.get("oauth_state")
    code_verifier = request.session.get("oauth_code_verifier")

    if not code or not state or expected_state != state or not code_verifier:
        request.session.pop("oauth_popup_mode", None)
        if popup_mode:
            return _oauth_popup_result_response(
                request,
                success=False,
                error_message=i18n_service.translate(
                    i18n_service.resolve_locale(request),
                    "flash.invalid_oauth_state",
                ),
            )
        return RedirectResponse("/?error_key=flash.invalid_oauth_state", status_code=303)

    user = get_or_create_session_user(request, db)

    client = EtsyClient()
    try:
        token_payload = client.exchange_code_for_tokens(code, code_verifier)
        token_row = save_user_tokens(db, user, token_payload, settings.etsy_scopes)

        # Resolve and persist Etsy user/shop binding.
        access_token = get_or_refresh_access_token(db, user, client)
        etsy_user_id = client.get_current_etsy_user_id(access_token)
        shop_id = client.get_shop_id_for_user(access_token, etsy_user_id)

        user.etsy_user_id = etsy_user_id
        user.etsy_shop_id = shop_id
        db.add(user)
        db.commit()

        add_audit_log(
            db,
            user_id=user.id,
            action="ETSY_CONNECTED",
            payload={
                "etsy_user_id": etsy_user_id,
                "etsy_shop_id": shop_id,
                "scopes": token_row.scopes,
            },
        )
    except EtsyAPIError as exc:
        if popup_mode:
            return _oauth_popup_result_response(
                request,
                success=False,
                error_message=f"Etsy connection failed: {str(exc)}",
            )
        return RedirectResponse(f"/?error=Etsy connection failed: {str(exc)}", status_code=303)
    finally:
        client.close()
        request.session.pop("oauth_state", None)
        request.session.pop("oauth_code_verifier", None)
        request.session.pop("oauth_popup_mode", None)

    if popup_mode:
        return _oauth_popup_result_response(request, success=True)
    return RedirectResponse("/dashboard?message_key=flash.etsy_connected", status_code=303)


@app.post("/auth/logout")
def logout(request: Request):
    """Clear local session cookie context."""

    clear_session_user(request)
    request.session["flash_message_key"] = "flash.signed_out"
    return RedirectResponse("/", status_code=303)


@app.post("/auth/etsy/disconnect")
def disconnect_etsy(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Disconnect Etsy account and delete OAuth/cache data for this user."""

    if user.etsy_user_id or user.etsy_shop_id:
        disconnect_etsy_account(db, user)
        add_audit_log(
            db,
            user_id=user.id,
            action="ETSY_DISCONNECTED",
        )

    return RedirectResponse("/", status_code=303)


@app.post("/auth/admin-login")
def admin_login(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Enable local admin bypass login for testing without Etsy OAuth approval."""

    if not settings.allow_admin_bypass_login:
        raise HTTPException(status_code=404, detail="Admin bypass login is disabled")

    apply_admin_bypass_identity(user)
    db.add(user)
    db.commit()
    db.refresh(user)

    add_audit_log(
        db,
        user_id=user.id,
        action="ADMIN_BYPASS_LOGIN",
        payload={"etsy_user_id": user.etsy_user_id, "etsy_shop_id": user.etsy_shop_id},
    )
    return RedirectResponse("/dashboard?message_key=flash.admin_bypass_enabled", status_code=303)


@app.post("/listings/sync")
def sync_listings(
    next: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Refresh and persist listing snapshot for low-cost selection previews."""

    ensure_etsy_connection(user)
    source = "admin_bypass" if is_admin_bypass_user(user) else "etsy"

    if is_admin_bypass_user(user):
        listing_rows = filtered_mock_listing_rows()
    else:
        client = EtsyClient()
        try:
            access_token = get_or_refresh_access_token(db, user, client)
            listing_rows = client.list_active_listings(access_token, str(user.etsy_shop_id))
        finally:
            client.close()

    sync_state = sync_user_listings_snapshot(db, user, listing_rows, source=source)
    add_audit_log(
        db,
        user_id=user.id,
        action="LISTINGS_SYNCED",
        payload={
            "listing_count": sync_state.listing_count,
            "source": sync_state.source,
            "synced_at": sync_state.last_synced_at.isoformat(),
        },
    )

    next_path = _safe_next_path(next or "/jobs/new")
    return RedirectResponse(f"{next_path}?message_key=flash.listings_synced", status_code=303)


@app.get("/api/listings")
def api_listings(
    filter: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Return synced listing rows without hitting Etsy listing APIs."""

    ensure_etsy_connection(user)
    sync_state = get_listing_sync_state(db, user)
    sync_stale = bool(sync_state and is_listing_sync_stale(sync_state.last_synced_at))
    if sync_state is None or sync_stale:
        return JSONResponse(
            {
                "results": [],
                "has_synced": False,
                "sync_stale": sync_stale,
                "last_synced_at": sync_state.last_synced_at.isoformat() if sync_state else None,
                "total_synced": int(sync_state.listing_count) if sync_state else 0,
                "max_age_hours": int(settings.listing_cache_max_age_hours),
            }
        )

    rows = get_synced_listing_rows(db, user, title_filter=filter)
    return JSONResponse(
        {
            "results": rows,
            "has_synced": True,
            "sync_stale": False,
            "last_synced_at": sync_state.last_synced_at.isoformat(),
            "total_synced": sync_state.listing_count,
            "max_age_hours": int(settings.listing_cache_max_age_hours),
        }
    )


@app.get("/api/listings/count")
def api_listing_count(
    filter: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Return count from synced listing snapshot for affected-target confirmation."""

    ensure_etsy_connection(user)
    sync_state = get_listing_sync_state(db, user)
    sync_stale = bool(sync_state and is_listing_sync_stale(sync_state.last_synced_at))
    if sync_state is None or sync_stale:
        return {
            "count": 0,
            "has_synced": False,
            "sync_stale": sync_stale,
            "last_synced_at": sync_state.last_synced_at.isoformat() if sync_state else None,
            "max_age_hours": int(settings.listing_cache_max_age_hours),
        }

    return {
        "count": get_synced_listing_count(db, user, title_filter=filter),
        "has_synced": True,
        "sync_stale": False,
        "last_synced_at": sync_state.last_synced_at.isoformat(),
        "max_age_hours": int(settings.listing_cache_max_age_hours),
    }


@app.get("/api/listings/preview")
def api_listing_preview(
    mode: str = SelectionMode.ALL_ACTIVE.value,
    filter: str | None = None,
    listing_ids_csv: str | None = None,
    limit: int = 25,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Preview affected listings for current form selection from synced snapshot."""

    ensure_etsy_connection(user)
    try:
        normalized_mode = SelectionMode(mode).value
    except ValueError:
        normalized_mode = SelectionMode.ALL_ACTIVE.value

    sync_state = get_listing_sync_state(db, user)
    sync_stale = bool(sync_state and is_listing_sync_stale(sync_state.last_synced_at))
    has_usable_sync = sync_state is not None and not sync_stale
    parsed_listing_ids = parse_listing_ids_csv(listing_ids_csv or "")

    if normalized_mode == SelectionMode.LISTING_IDS.value:
        rows = preview_listings_for_selection(
            db,
            user,
            selection_mode=normalized_mode,
            title_filter=filter,
            listing_ids=parsed_listing_ids,
            limit=limit,
        )
        total_count = len({listing_id for listing_id in parsed_listing_ids if listing_id})
    elif not has_usable_sync:
        rows = []
        total_count = 0
    elif normalized_mode == SelectionMode.FILTER.value:
        rows = preview_listings_for_selection(
            db,
            user,
            selection_mode=normalized_mode,
            title_filter=filter,
            listing_ids=parsed_listing_ids,
            limit=limit,
        )
        total_count = get_synced_listing_count(db, user, title_filter=filter)
    else:
        rows = preview_listings_for_selection(
            db,
            user,
            selection_mode=normalized_mode,
            title_filter=filter,
            listing_ids=parsed_listing_ids,
            limit=limit,
        )
        total_count = int(sync_state.listing_count)

    return {
        "results": rows,
        "count": total_count,
        "has_synced": has_usable_sync,
        "sync_stale": sync_stale,
        "last_synced_at": sync_state.last_synced_at.isoformat() if sync_state else None,
        "total_synced": int(sync_state.listing_count) if has_usable_sync else 0,
        "max_age_hours": int(settings.listing_cache_max_age_hours),
    }


@app.post("/api/jobs/price-adjust", response_model=JobCreateResponse)
def api_create_price_adjust_job(
    payload: PriceAdjustJobRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Create and queue a new bulk variation price adjustment job."""

    ensure_etsy_connection(user)
    listing_ids = resolve_listing_ids_for_selection(
        db=db,
        user=user,
        request_payload=payload,
    )

    job = create_price_adjust_job(
        db=db,
        user=user,
        request_payload=payload,
        listing_ids=listing_ids,
    )

    return JobCreateResponse(job_id=job.id, status=job.status, total_items=len(listing_ids))


@app.get("/api/jobs/{job_id}", response_model=JobSummaryResponse)
def api_get_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Return aggregate job progress/status payload."""

    job = _job_owned_by_user_or_404(db, job_id, user)
    return JobSummaryResponse.model_validate(build_job_summary(db, job))


@app.get("/api/jobs/{job_id}/items")
def api_get_job_items(
    job_id: int,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Return per-listing job item rows, optionally filtered by status."""

    job = _job_owned_by_user_or_404(db, job_id, user)

    stmt = select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.updated_at.desc())
    if status:
        stmt = stmt.where(JobItem.status == status)
    items = db.scalars(stmt).all()

    return {"results": serialize_job_items(items)}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Request cancellation for an active/queued job."""

    job = _job_owned_by_user_or_404(db, job_id, user)

    if job.status in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
        return {"job_id": job.id, "status": job.status, "cancel_requested": bool(job.cancel_requested)}

    if job.status == JobStatus.QUEUED.value:
        job.status = JobStatus.CANCELLED.value

    job.cancel_requested = True
    db.add(job)
    db.commit()

    add_audit_log(db, user_id=user.id, job_id=job.id, action="JOB_CANCEL_REQUESTED")

    return {"job_id": job.id, "status": job.status, "cancel_requested": True}


@app.get("/api/jobs/{job_id}/report.csv")
def api_job_report_csv(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Generate downloadable CSV report for job listing-level outcomes."""

    job = _job_owned_by_user_or_404(db, job_id, user)
    items = db.scalars(select(JobItem).where(JobItem.job_id == job.id).order_by(JobItem.id.asc())).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "job_id",
        "listing_id",
        "status",
        "before_summary",
        "after_summary",
        "error",
        "updated_at",
    ])

    for item in items:
        writer.writerow(
            [
                job.id,
                item.listing_id,
                item.status,
                item.before_summary,
                item.after_summary,
                item.error or "",
                item.updated_at.isoformat(),
            ]
        )

    filename = f"job-{job.id}-report.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=output.getvalue(), media_type="text/csv", headers=headers)


@app.get("/health")
def health() -> dict[str, Any]:
    """Basic health endpoint for local checks and deployment probes."""

    return {
        "status": "ok",
        "app": settings.app_name,
        "default_locale": settings.default_locale,
        "supported_locales": list(i18n_service.supported_locales),
    }
