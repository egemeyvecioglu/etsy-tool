# etsy-tool

Remote seller tool for bulk Etsy variation price updates.

## What is implemented

This MVP implements the full plan in `implementation-plan.md`:

- FastAPI web app with server-rendered pages (Jinja2)
- Etsy OAuth start/callback flow with PKCE
- Encrypted OAuth token storage in database
- Legal acceptance gate before Etsy OAuth start (app terms + privacy policy)
- Listings selection modes:
  - all active
  - title keyword filter
  - explicit listing IDs
- Listings sync cache workflow:
  - manual `Sync Listings` action
  - synced snapshot persisted across sessions
  - stale snapshots blocked by policy (`ETSY_TOOL_LISTING_CACHE_MAX_AGE_HOURS`, default 6h)
  - create-job preview of affected listings from synced data
- Bulk variation price update job system with:
  - async background worker
  - per-listing job item tracking
  - cancellation (best effort)
  - dry-run mode
  - CSV report export
- Core price transform logic:
  - amount/percentage updates with explicit increase/decrease direction
  - non-negative input values (direction controls sign)
  - minimum price floor
  - standard or bankers rounding
  - optional max change guardrail
- Basic audit trail entries
- Unit tests for key logic
- Etsy-required trademark disclaimer + support contact in shared footer

## Stack

- Python 3.10+
- FastAPI + Jinja2
- SQLAlchemy (SQLite by default)
- HTTPX for Etsy API calls
- Cryptography/Fernet for token encryption

## Project structure

- `app/main.py`: FastAPI app, routes, UI pages, API endpoints
- `app/worker.py`: background job runner
- `app/etsy_client.py`: Etsy OAuth + API client with retry/backoff
- `app/pricing.py`: variation price transformation logic
- `app/services.py`: session/user/token/job service helpers
- `app/models.py`: ORM entities (`User`, `OAuthToken`, `Job`, `JobItem`, `AuditLog`)
- `config/defaults.yaml`: non-sensitive default config values
- `.env.example`: env vars/secrets template

## Setup

1. Create and activate a virtualenv.
2. Install dependencies.
3. Copy `.env.example` to `.env` and fill in Etsy + secret values.
4. Run database init (optional; app also creates schema on startup).
5. Start the server.

Example commands:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python scripts/init_db.py
python main.py
```

App URL: `http://localhost:8000`

## Configuration

Configuration can come from:

1. Environment variables (`ETSY_TOOL_*`)
2. `.env`
3. `config/defaults.yaml`

Important variables:

- `ETSY_TOOL_DATABASE_URL`
- `ETSY_TOOL_SESSION_SECRET_KEY`
- `ETSY_TOOL_TOKEN_ENCRYPTION_KEY`
- `ETSY_TOOL_OAUTH_CLIENT_ID`
- `ETSY_TOOL_OAUTH_CLIENT_SECRET`
- `ETSY_TOOL_OAUTH_REDIRECT_URI`
- `ETSY_TOOL_ETSY_SCOPES`
- `ETSY_TOOL_LISTING_CACHE_MAX_AGE_HOURS` (default: `6`)
- `ETSY_TOOL_SUPPORT_EMAIL` (required monitored email in production)
- `ETSY_TOOL_LEGAL_TERMS_VERSION` (default: `2026-02-09`)
- `ETSY_TOOL_LEGAL_PRIVACY_VERSION` (default: `2026-02-09`)
- `ETSY_TOOL_ALLOW_ADMIN_BYPASS_LOGIN` (default: `false`, testing only)
- `ETSY_TOOL_DEFAULT_LOCALE` (default: `en`)
- `ETSY_TOOL_SUPPORTED_LOCALES` (default: `en,tr`)
- `ETSY_TOOL_I18N_PATH` (default: `config/i18n`)
- `ETSY_TOOL_JOB_FORM_SCHEMA_PATH` (default: `config/ui/job_form.yaml`)

Generate encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Localization

- Current languages: English (`en`, default) and Turkish (`tr`)
- Users can switch language from the top header
- Translation files:
  - `config/i18n/en.yaml`
  - `config/i18n/tr.yaml`
- To add a new language, add `<locale>.yaml` under `config/i18n` and include it in `ETSY_TOOL_SUPPORTED_LOCALES`

## Schema-driven UI form

- Job creation UI is rendered from `config/ui/job_form.yaml`
- Adding/editing fields and sections is primarily a config change when using supported field types:
  - `radio_group`, `text`, `textarea`, `select`, `number`, `checkbox`
- Backend parser currently maps core fields required for price-adjust jobs (`app/services.py`)

## API endpoints

Auth:

- `GET /auth/etsy/start`
- `GET /oauth/etsy/callback`
- `POST /auth/etsy/disconnect`
- `POST /auth/logout`

Legal pages:

- `GET /legal/terms`
- `GET /legal/privacy`

Listings:

- `POST /listings/sync` (refresh snapshot cache)
- `GET /api/listings?filter=...`
- `GET /api/listings/count?filter=...`
- `GET /api/listings/preview?mode=...&filter=...&listing_ids_csv=...`

Jobs:

- `POST /api/jobs/price-adjust`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/items?status=...`
- `POST /api/jobs/{job_id}/cancel`
- `GET /api/jobs/{job_id}/report.csv`

## Notes

- The app uses a simple local session identity for MVP; users are linked to Etsy after OAuth callback.
- `all_active` and `filter` selection modes resolve from synced listing cache (not live Etsy listing fetches).
- Users must run `Sync Listings` after changing listings outside the app and before synced data exceeds policy max age.
- Etsy connection links open OAuth in a popup by default; if blocked, flow falls back to full-page mode.
- Testing shortcut: `Admin Login (Test Mode)` bypasses Etsy OAuth and uses mock listing/inventory data.
- Production deployment should use Postgres + real worker process separation + reverse proxy/TLS.
- Etsy endpoint payloads may vary by account and scope; if Etsy schema changes, adjust `app/etsy_client.py` mapping keys.

## Tests

```bash
pytest
```
