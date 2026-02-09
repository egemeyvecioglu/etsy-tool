# Implementation Plan — Remote Seller Tool for Bulk Variation Price Updates (Etsy)

## 0) Goal and Scope
Build a small web application that lets authorized Etsy sellers:
- Connect their Etsy account via OAuth
- Select listings (all / filtered / specific IDs)
- Apply a bulk price adjustment across all variation combinations (e.g., +$2 / -$2, or percentage)
- Execute the job server-side and show progress + results

Initial supported operation:
- **Bulk adjust variation prices** (absolute delta or percent) for listings that have variations / inventory offerings.

Non-goals (initially):
- Sales analytics, messaging, order management, email notifications, complex rules engine.

---

## 1) High-Level Architecture
**Web App (Frontend)**
- Simple UI for login, select listings, configure adjustment, start job, view status/history.

**API Server (Backend)**
- Handles OAuth, stores tokens securely, creates jobs, runs updates, exposes job status.

**Worker / Job Runner**
- Processes jobs asynchronously (rate-limited), performs Etsy API calls, records per-listing results.

**Database**
- Stores users, tokens (encrypted), jobs, job items (listing-level results), audit logs.

**Optional Cache/Queue**
- Redis + RQ/Celery (or a lightweight in-process queue for MVP).

### 1.1 Language and Runtime Requirements
- **Python-only MVP (recommended):** Backend (FastAPI), server-rendered frontend (Jinja2 templates), job runner/worker, and database access can all be implemented in Python.
- **Optional JavaScript (not required):** You may add a small amount of JS for UI polling/progress updates. A full React/Node frontend is only needed if you want a richer SPA experience.
- **Not “languages” but required components in production:**
  - **Reverse proxy / TLS:** Nginx or Caddy for HTTPS termination and routing.
  - **Database:** Postgres recommended; SQLite acceptable for MVP/testing.
  - **Queue/Cache (optional):** Redis if using Celery/RQ or if you want reliable background jobs.

---

## 2) Etsy OAuth & Account Linking
### 2.1 Etsy App Setup (One-time)
- Register an Etsy app.
- Configure a **fixed HTTPS redirect URL** (callback), e.g.:
  - `https://your-domain.com/oauth/etsy/callback`

### 2.2 OAuth Flow (Per user)
- Use OAuth 2.0 Authorization Code with PKCE:
  1. Backend generates `code_verifier` + `code_challenge`
  2. Frontend redirects user to Etsy consent screen
  3. Etsy redirects back with `code`
  4. Backend exchanges `code` for:
     - `access_token` + `refresh_token` (and expiry)
- Persist tokens per user; refresh automatically when needed.

### 2.3 Required Scopes (Minimum for MVP)
- Read listings + inventory
- Write listings/inventory (for price updates)
(Exact scope names depend on Etsy docs; implement as configurable constants.)

---

## 3) Data Model (Minimal)
### 3.1 Tables / Entities
**User**
- `id`, `created_at`, `email (optional)`, `etsy_user_id`, `status`

**OAuthToken**
- `user_id`
- `access_token_encrypted`
- `refresh_token_encrypted`
- `expires_at`
- `scopes`
- `updated_at`

**Job**
- `id`, `user_id`, `type` (PRICE_ADJUST)
- `status` (QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED)
- `request_payload` (JSON: selection + adjustment config)
- `created_at`, `started_at`, `finished_at`

**JobItem**
- `job_id`, `listing_id`
- `status` (PENDING/SUCCEEDED/FAILED/SKIPPED)
- `before_summary` (JSON)
- `after_summary` (JSON)
- `error` (text), `updated_at`

### 3.2 Security Fields
- Encryption key stored in server secret manager / env vars.
- Token columns encrypted at rest.

---

## 4) Frontend (Very Simple)
### 4.1 Pages
1. **Login / Connect Etsy**
   - Button: “Connect Etsy Account”
2. **Dashboard**
   - Show connected shop(s)
   - Job history list
3. **Create Job: Bulk Price Update**
   - Listing selection:
     - Option A: All active listings
     - Option B: Filter by title keyword
     - Option C: Provide explicit listing IDs (CSV paste)
   - Adjustment:
     - Absolute: `+2.00` or `-2.00`
     - Percent: `+5%` or `-10%`
   - Options:
     - Minimum price floor (e.g., 0.01)
     - Rounding mode (2 decimals, bankers vs standard)
     - Currency handling (assume listing currency; do not convert)
   - Button: “Run”
4. **Job Detail**
   - Progress bar (items completed / total)
   - Table of listing results (success/fail, error message)
   - Downloadable report (CSV)

### 4.2 Tech Choice
- MVP: **server-rendered HTML** (FastAPI + Jinja2) + minimal JS (optional).
- Keep it simple: form posts to backend; polling for job status (JS optional).
- Optional: React/Node SPA if you later need a richer UI; not required for functionality.

---

## 5) Backend API (Endpoints)
### 5.1 Auth & User
- `GET /auth/etsy/start` → redirects to Etsy consent
- `GET /oauth/etsy/callback` → exchanges code, stores tokens, redirects to dashboard
- `POST /auth/logout`

### 5.2 Listings (Selection Helpers)
- `GET /api/listings?filter=...` (optional for UI preview)
- `GET /api/listings/count?...` (for quick “how many will be affected”)

### 5.3 Jobs
- `POST /api/jobs/price-adjust`
  - Body:
    - `selection`: {all_active | filter | listing_ids}
    - `adjustment`: {type: absolute|percent, value: number}
    - `options`: {min_price, rounding, dry_run}
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/items?status=...`
- `POST /api/jobs/{job_id}/cancel` (best-effort)

---

## 6) Core Edit Logic (Variation Price Update)
### 6.1 Listing Inventory Retrieval
For each listing:
1. Fetch inventory/offerings that contain variation combinations and prices.
2. Identify all purchasable offerings (skip disabled/unavailable ones if required).

### 6.2 Price Transformation Function
For each offering price:
- Input: price as decimal (string/amount + divisor depending on API)
- Apply:
  - If absolute: `new = old + delta`
  - If percent: `new = old * (1 + pct)`
- Enforce:
  - `new >= min_price`
  - Round to 2 decimals (configurable)
- If `new == old`, optionally skip update for that offering.

### 6.3 Update Write-back
- Construct updated inventory payload preserving:
  - Variation properties
  - Quantities
  - Enabled/disabled flags
  - SKU if present
- Send update request.
- Validate response; record per-listing success/failure.

### 6.4 Dry Run Mode (Highly Recommended)
- Compute and show deltas without applying updates.
- Store preview results in job items; user can re-run as “apply”.

---

## 7) Job Execution & Rate Limiting
### 7.1 Why Async Jobs
- Updating many listings can take minutes.
- Avoid browser timeouts; provide progress.

### 7.2 Worker Strategy
- Use a queue (Redis + RQ/Celery) or a background task runner.
- Concurrency:
  - Start with single worker per user to avoid bursts.
  - Implement per-user throttling.

### 7.3 Rate Limits / Retries
- Implement:
  - Exponential backoff on 429/5xx
  - Max retries per listing
- If a listing update fails, continue with the rest, then mark job partially failed.

---

## 8) Token Refresh & Request Signing
- Before each Etsy API call:
  - If token expiring soon, refresh using refresh_token.
  - Update stored tokens atomically.
- Attach required headers:
  - API key header(s)
  - Authorization header (Bearer format required by Etsy)
- Centralize this in a single `EtsyClient` module.

---

## 9) Hosting & Deployment
### 9.1 Minimal Production Setup
- Single VM or container platform (Python app + worker processes).
- Components:
  - Web server: FastAPI (uvicorn/gunicorn)
  - Worker: separate process
  - DB: Postgres (or SQLite for MVP only)
  - Redis (if using a real queue)

### 9.2 HTTPS & Redirect URL
- Must be HTTPS for OAuth callback (use a real domain).
- Use a reverse proxy (Caddy/Nginx) + LetsEncrypt.
- Note: Nginx/Caddy are infrastructure components (not programming languages) and can be added without introducing a new application language.

### 9.3 Secrets Management
- Store:
  - Etsy client key/secret
  - Token encryption key
  - DB credentials
- Use environment variables + secret manager in production.

---

## 10) Observability & Safety
### 10.1 Logging
- Request IDs, job IDs, listing IDs.
- Do not log tokens or sensitive headers.

### 10.2 Audit Trail
- Persist:
  - Who ran the job
  - When
  - What adjustment config
  - Which listings updated

### 10.3 Guardrails
- Confirmation step showing:
  - Number of listings
  - Number of offerings affected
  - Example price changes
- Price floor required (to prevent negative/zero)
- Optional max-change cap (e.g., reject >50% changes unless explicitly allowed)

---

## 11) MVP Milestones (Order of Work)
1. **Project skeleton**
   - FastAPI app, DB migrations, basic templates
2. **OAuth**
   - Start/callback endpoints, token storage, refresh logic
3. **Etsy client module**
   - Auth headers, retry/backoff, typed responses
4. **Listing selection**
   - Fetch all active listings; basic filtering
5. **Inventory read + update for 1 listing**
   - End-to-end success case
6. **Job system**
   - Job table + worker loop + progress
7. **UI**
   - Create job form + job status page
8. **Dry run + report**
   - CSV export of changes
9. **Hardening**
   - Better errors, rate limiting, cancellation, logs

---

## 12) Future Extensions (After MVP)
- Rule-based adjustments (by style/size name, by SKU, by price ranges)
- Bulk quantity edits
- Enable/disable specific variations
- Scheduled jobs (e.g., weekly price updates)
- Multi-shop per user support (if Etsy returns multiple shops)
- Team accounts and delegated access (admin roles)