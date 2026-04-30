"""
Unified API client for AnkiCollab plugin.

All authenticated requests go through this module, which automatically
attaches the ``Authorization: Bearer <token>`` header.  Unauthenticated
endpoints (pullChanges, CheckDeckAlive, …) can still use plain
``requests`` directly.
"""

from __future__ import annotations

import gzip
import base64
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .var_defs import API_BASE_URL


class _ApiClient:
    """Thin wrapper around ``requests`` that injects the bearer token."""

    def __init__(self):
        self._timeout_default = 30
        self._timeout_large = 120

    # ── internal helpers ──────────────────────────────────────────────

    def _auth_headers(self, token: str) -> dict:
        """Return headers dict with the Authorization bearer token."""
        return {"Authorization": f"Bearer {token}"}

    def _get_token(self) -> str:
        """Lazily import ``auth_manager`` to avoid circular imports."""
        from .auth_manager import auth_manager
        return auth_manager.get_token()

    def _check_for_auth_failure(self, response: requests.Response) -> None:
        """If *response* is 401, clear local credentials and warn the user."""
        if response.status_code == 401:
            from .auth_manager import auth_manager
            auth_manager.handle_auth_failure()

    # ── public API ────────────────────────────────────────────────────

    def post_json(
        self,
        endpoint: str,
        payload: dict | None = None,
        *,
        timeout: int | None = None,
        auth: bool = True,
    ) -> requests.Response:
        """POST *payload* as JSON to *endpoint* with bearer auth.

        Parameters
        ----------
        endpoint : str
            Relative path, e.g. ``"/AddSubscription"``.
        payload : dict | None
            JSON-serializable body.  ``None`` sends an empty body.
        timeout : int | None
            Request timeout in seconds (default: 30).
        auth : bool
            Whether to add the Authorization header (default True).
        """
        url = f"{API_BASE_URL}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if auth:
            token = self._get_token()
            if not token:
                raise RuntimeError("Not logged in – no valid auth token available")
            headers.update(self._auth_headers(token))
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout or self._timeout_default,
            verify=True,
        )
        self._check_for_auth_failure(response)
        return response

    def post_gzip(
        self,
        endpoint: str,
        data: dict,
        *,
        timeout: int | None = None,
    ) -> requests.Response:
        """Compress *data* (gzip → base64) and POST as text with bearer auth.

        Used for ``/createDeck``, ``/submitCard``, ``/UploadDeckStats``.
        """
        url = f"{API_BASE_URL}{endpoint}"
        token = self._get_token()
        if not token:
            raise RuntimeError("Not logged in – no valid auth token available")
        compressed = base64.b64encode(
            gzip.compress(json.dumps(data).encode("utf-8"))
        ).decode("utf-8")
        headers = {
            "Content-Type": "text/plain",
            **self._auth_headers(token),
        }
        response = requests.post(
            url,
            data=compressed,
            headers=headers,
            timeout=timeout or self._timeout_large,
            verify=True,
        )
        self._check_for_auth_failure(response)
        return response

    def get(
        self,
        endpoint: str,
        *,
        timeout: int | None = None,
        auth: bool = False,
    ) -> requests.Response:
        """GET *endpoint*, optionally with bearer auth."""
        url = f"{API_BASE_URL}{endpoint}"
        headers = {}
        if auth:
            token = self._get_token()
            if not token:
                raise RuntimeError("Not logged in – no valid auth token available")
            headers.update(self._auth_headers(token))
        response = requests.get(url, headers=headers, timeout=timeout or self._timeout_default, verify=True)
        self._check_for_auth_failure(response)
        return response

    def post_empty(
        self,
        endpoint: str,
        *,
        timeout: int | None = None,
    ) -> requests.Response:
        """POST with bearer auth but no body (used for token-only endpoints)."""
        url = f"{API_BASE_URL}{endpoint}"
        token = self._get_token()
        if not token:
            raise RuntimeError("Not logged in – no valid auth token available")
        headers = self._auth_headers(token)
        response = requests.post(url, headers=headers, timeout=timeout or self._timeout_default, verify=True)
        self._check_for_auth_failure(response)
        return response

    def session_with_auth(self) -> requests.Session:
        """Return a ``requests.Session`` pre-configured with bearer auth.

        Useful for media_manager which makes multiple requests in a row.
        Mounts a retry adapter to handle transient connection resets
        (e.g. stale keep-alive connections being closed by the server).
        """
        token = self._get_token()
        session = requests.Session()
        if token:
            session.headers.update(self._auth_headers(token))

        # Retry on transport-level errors (connection resets, DNS failures, etc.)
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "PUT"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session


# Singleton – import as ``from .api_client import api_client``
api_client = _ApiClient()
