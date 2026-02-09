"""HTTP client wrapper for Etsy OAuth and listing/inventory operations."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings


class EtsyAPIError(RuntimeError):
    """Raised when Etsy API interactions fail irrecoverably."""


class EtsyClient:
    """Encapsulates OAuth, listing, and inventory calls to Etsy APIs."""

    def __init__(self) -> None:
        """Initialize an Etsy client with configured endpoints and retries."""

        self.settings = get_settings()
        self._http = httpx.Client(timeout=self.settings.request_timeout_seconds)

    def close(self) -> None:
        """Close the underlying HTTP client."""

        self._http.close()

    def _headers(self, access_token: str | None = None) -> dict[str, str]:
        """Build common API headers for Etsy requests."""

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.settings.etsy_client_id,
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        use_api_key: bool = True,
    ) -> dict[str, Any]:
        """Execute an Etsy request with retry/backoff on transient errors."""

        headers = self._headers(access_token) if use_api_key else {"Content-Type": "application/json"}

        retryable_statuses = {429, 500, 502, 503, 504}
        last_error: Exception | None = None

        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self._http.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=json,
                    params=params,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < self.settings.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise EtsyAPIError(f"Network error while calling Etsy: {exc}") from exc

            if response.status_code in retryable_statuses and attempt < self.settings.max_retries:
                # Exponential backoff to honor Etsy rate limit and transient failures.
                time.sleep(2**attempt)
                continue

            if response.is_error:
                raise EtsyAPIError(
                    f"Etsy request failed ({response.status_code}) {response.text[:300]}"
                )

            if not response.text.strip():
                return {}

            payload = response.json()
            if isinstance(payload, dict):
                return payload
            return {"results": payload}

        if last_error is not None:
            raise EtsyAPIError(str(last_error))
        raise EtsyAPIError("Unknown Etsy API request failure")

    def build_authorization_url(self, state: str, code_challenge: str) -> str:
        """Construct OAuth consent URL for Etsy Authorization Code + PKCE flow."""

        params = {
            "response_type": "code",
            "redirect_uri": self.settings.etsy_redirect_uri,
            "scope": self.settings.etsy_scopes,
            "client_id": self.settings.etsy_client_id,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.settings.etsy_authorize_url}?{urlencode(params)}"

    def exchange_code_for_tokens(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange OAuth authorization code for access/refresh tokens."""

        payload = {
            "grant_type": "authorization_code",
            "client_id": self.settings.etsy_client_id,
            "redirect_uri": self.settings.etsy_redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        }
        if self.settings.etsy_client_secret:
            payload["client_secret"] = self.settings.etsy_client_secret

        return self._request(
            "POST",
            self.settings.etsy_token_url,
            json=payload,
            use_api_key=False,
        )

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """Refresh an expired/expiring Etsy access token."""

        payload = {
            "grant_type": "refresh_token",
            "client_id": self.settings.etsy_client_id,
            "refresh_token": refresh_token,
        }
        if self.settings.etsy_client_secret:
            payload["client_secret"] = self.settings.etsy_client_secret

        return self._request(
            "POST",
            self.settings.etsy_token_url,
            json=payload,
            use_api_key=False,
        )

    def get_current_etsy_user_id(self, access_token: str) -> str:
        """Resolve Etsy user ID for the currently authenticated OAuth token."""

        endpoints = [
            f"{self.settings.etsy_base_url}/users/__SELF__",
            f"{self.settings.etsy_base_url}/users/me",
        ]

        for endpoint in endpoints:
            try:
                payload = self._request("GET", endpoint, access_token=access_token)
            except EtsyAPIError:
                continue

            for candidate_key in ("user_id", "userId", "id"):
                value = payload.get(candidate_key)
                if value is not None:
                    return str(value)

            results = payload.get("results")
            if isinstance(results, dict):
                for candidate_key in ("user_id", "userId", "id"):
                    value = results.get(candidate_key)
                    if value is not None:
                        return str(value)

        raise EtsyAPIError("Unable to resolve Etsy user ID from OAuth token")

    def get_shop_id_for_user(self, access_token: str, etsy_user_id: str) -> str:
        """Fetch the first shop ID owned by the authenticated Etsy user."""

        payload = self._request(
            "GET",
            f"{self.settings.etsy_base_url}/users/{etsy_user_id}/shops",
            access_token=access_token,
        )

        results = payload.get("results", [])
        if not results:
            raise EtsyAPIError("No shops found for Etsy user")

        first_shop = results[0]
        for candidate_key in ("shop_id", "shopId", "id"):
            value = first_shop.get(candidate_key)
            if value is not None:
                return str(value)

        raise EtsyAPIError("Shop ID missing in Etsy shop response")

    def list_active_listings(
        self,
        access_token: str,
        shop_id: str,
        *,
        title_filter: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List all active listings for a shop, optionally filtered by title."""

        all_results: list[dict[str, Any]] = []
        offset = 0

        while True:
            params = {
                "limit": limit,
                "offset": offset,
                "state": "active",
            }
            payload = self._request(
                "GET",
                f"{self.settings.etsy_base_url}/shops/{shop_id}/listings/active",
                access_token=access_token,
                params=params,
            )

            chunk = payload.get("results", [])
            if title_filter:
                lowered = title_filter.lower()
                chunk = [
                    item
                    for item in chunk
                    if lowered in str(item.get("title", "")).lower()
                ]

            all_results.extend(chunk)

            if len(chunk) < limit:
                break
            offset += limit

        return all_results

    def get_listing_inventory(self, access_token: str, listing_id: str) -> dict[str, Any]:
        """Fetch a listing inventory payload that includes offerings and prices."""

        return self._request(
            "GET",
            f"{self.settings.etsy_base_url}/listings/{listing_id}/inventory",
            access_token=access_token,
        )

    def update_listing_inventory(
        self,
        access_token: str,
        listing_id: str,
        inventory_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Write updated listing inventory payload back to Etsy."""

        return self._request(
            "PUT",
            f"{self.settings.etsy_base_url}/listings/{listing_id}/inventory",
            access_token=access_token,
            json=inventory_payload,
        )
