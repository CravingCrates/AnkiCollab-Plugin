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
from .utils import get_logger

logger = get_logger("ankicollab.api_client")


class ApiConnectionError(requests.exceptions.ConnectionError):
    """Raised when the client cannot connect to the AnkiCollab server."""

    pass


class _ApiClient:
    def __init__(self):
        self._timeout_default = 30
        self._timeout_large = 120

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

    def _request(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> requests.Response:
        """Execute an HTTP request and handle connection errors gracefully."""
        try:
            response = requests.request(method, url, **kwargs)
            self._check_for_auth_failure(response)
            return response
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            logger.warning(
                "Network error during %s %s: %s",
                method.upper(),
                url,
                exc,
            )
            raise ApiConnectionError(
                "Unable to connect to AnkiCollab.\n\n"
                "Please check your internet connection and try again."
            ) from exc

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
        return self._request(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=timeout or self._timeout_default,
            verify=True,
        )

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
        return self._request(
            "POST",
            url,
            data=compressed,
            headers=headers,
            timeout=timeout or self._timeout_large,
            verify=True,
        )

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
        return self._request(
            "GET",
            url,
            headers=headers,
            timeout=timeout or self._timeout_default,
            verify=True,
        )

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
        return self._request(
            "POST",
            url,
            headers=headers,
            timeout=timeout or self._timeout_default,
            verify=True,
        )

    def session_with_retries(self) -> requests.Session:
        """Return a requests session configured for transient failures."""
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "PUT"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)  # type: ignore
        session = requests.Session()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def session_with_auth(self) -> requests.Session:
        """Return a ``requests.Session`` pre-configured with bearer auth.

        Useful for media_manager which makes multiple requests in a row.
        """
        token = self._get_token()
        session = self.session_with_retries()
        if token:
            session.headers.update(self._auth_headers(token))
        return session


# Singleton – import as ``from .api_client import api_client``
api_client = _ApiClient()
