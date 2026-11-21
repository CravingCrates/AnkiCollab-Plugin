import time
import logging
import threading
import requests
from datetime import datetime

from aqt import mw
import aqt.utils
from aqt.qt import *

from . import config_manager
from .var_defs import API_BASE_URL

_TRANSIENT_REFRESH_COOLDOWN_SECONDS = 60


class AuthManager:
    """
    Handles AnkiCollab auth token storage.
    """

    def __init__(self):
        self.auth_data = {}
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._loaded = False
        self._last_refresh_transient_error = False
        self._transient_refresh_retry_after = 0.0

        from aqt import gui_hooks

        gui_hooks.main_window_did_init.append(self._load_auth_data)
        # A profile switch happens inside one process, so the data loaded at
        # main-window init belongs to whichever profile was open then. Reload
        # whenever config_manager activates a different profile.
        config_manager.add_profile_changed_listener(self._on_profile_changed)

    def _on_profile_changed(self, _profile_name):
        """Reload the active profile's auth data after a profile switch.

        Called on the main thread by config_manager, after the new profile's
        migration has run, so the new profile's auth data is already in place.
        """
        with self._lock:
            self.auth_data = config_manager.get_auth()
            self._loaded = True
            self._last_refresh_transient_error = False
            self._transient_refresh_retry_after = 0.0

    def _load_auth_data(self):
        """Load the active profile's auth data."""
        with self._lock:
            self.auth_data = config_manager.get_auth()
            self._loaded = True

    def _persist_auth_data(self, expected_profile=None):
        """Write ``self.auth_data`` under ``_lock``, returning any OSError."""
        with self._lock:
            if (
                expected_profile is not None
                and config_manager.current_profile_name() != expected_profile
            ):
                return None
            try:
                config_manager.write_auth(self.auth_data)
                return None
            except config_manager.NoActiveProfileError:
                # A 401 or a logout can arrive while no profile is open
                # (shutdown, profile switch). Warning beats crashing the caller.
                logging.getLogger(__name__).warning(
                    "AnkiCollab: no profile is open; auth data was not saved"
                )
                return None
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "AnkiCollab: could not save the login locally: %s", exc
                )
                return exc

    def _save_auth_data(self):
        """Persist the active profile's auth data.

        Never raises: a read-only or full ``user_files`` must not take down a
        login, or a 401 handler running on a background thread.
        """
        failure = self._persist_auth_data()
        # Outside the lock: showInfo is modal and runs on the main thread.
        if failure is not None:
            self._warn_save_failed()

    def _warn_save_failed(self):
        """Tell the user, on the main thread, that the login could not be saved."""
        try:
            if mw and mw.taskman:
                mw.taskman.run_on_main(
                    lambda: aqt.utils.showInfo(
                        "AnkiCollab could not save your login locally. "
                        "You may need to log in again next time you start Anki."
                    )
                )
        except Exception:
            pass

    @staticmethod
    def _build_login_data(auth_response):
        if not auth_response:
            return None

        data = {
            "token": auth_response.get("token", ""),
            "refresh_token": auth_response.get("refresh_token", ""),
        }

        if "expires_at" in auth_response:
            try:
                expires_val = auth_response["expires_at"]
                if isinstance(expires_val, (int, float)):
                    data["expires_timestamp"] = float(expires_val)
                elif isinstance(expires_val, str):
                    # Parse ISO format date, handle timezone
                    expires_str = expires_val.replace("Z", "+00:00")
                    expires_dt = datetime.fromisoformat(expires_str)
                    data["expires_timestamp"] = expires_dt.timestamp()
                else:
                    raise TypeError("Invalid type for expires_at")
            except Exception:
                data["expires_timestamp"] = time.time() + (30 * 86400)  # 30 days

        return data

    def _matches_snapshot_locked(
        self, snapshot_profile, snapshot_token, snapshot_refresh_token
    ) -> bool:
        return (
            config_manager.current_profile_name() == snapshot_profile
            and self.auth_data.get("token") == snapshot_token
            and self.auth_data.get("refresh_token") == snapshot_refresh_token
        )

    def _has_active_transient_cooldown_locked(self) -> bool:
        now = time.time()
        expires = self.auth_data.get("expires_timestamp")
        return self._transient_refresh_retry_after > now and (
            expires is None or expires > now
        )

    def _cached_valid_token_locked(self):
        """Return cached token if usable without refresh (or ``""`` if missing), else ``None``."""
        if not self._loaded:
            self._load_auth_data()

        if not self.auth_data or "token" not in self.auth_data:
            return ""

        if not self._should_refresh_token():
            return self.auth_data.get("token", "")

        if self._has_active_transient_cooldown_locked():
            return self.auth_data.get("token", "")

        return None

    def _commit_login_data_locked(self, new_data, expected_profile=None):
        self.auth_data = new_data
        self._loaded = True
        self._last_refresh_transient_error = False
        self._transient_refresh_retry_after = 0.0
        return self._persist_auth_data(expected_profile=expected_profile)

    def store_login_result(self, auth_response):
        new_data = self._build_login_data(auth_response)
        if new_data is None:
            return False

        with self._lock:
            failure = self._commit_login_data_locked(new_data)
        if failure is not None:
            self._warn_save_failed()
        return True

    def has_stored_token(self) -> bool:
        """Check locally whether a token is stored without performing network I/O."""
        with self._lock:
            if not self._loaded:
                self._load_auth_data()
            return bool(self.auth_data and self.auth_data.get("token"))

    def get_token(self):
        """Get the current access token, refreshing if needed."""
        with self._lock:
            cached = self._cached_valid_token_locked()
            if cached is not None:
                return cached

        # Serialize refresh attempts across threads so rotated refresh tokens
        # aren't invalidated by duplicate concurrent /refreshToken calls.
        with self._refresh_lock:
            with self._lock:
                cached = self._cached_valid_token_locked()
                if cached is not None:
                    return cached

                snapshot_token = self.auth_data.get("token")
                snapshot_refresh_token = self.auth_data.get("refresh_token")
                snapshot_profile = config_manager.current_profile_name()
                self._last_refresh_transient_error = False

            refreshed = self.refresh_token()

            failure = None
            with self._lock:
                if refreshed:
                    if config_manager.current_profile_name() != snapshot_profile:
                        return ""
                    return self.auth_data.get("token", "")

                if config_manager.current_profile_name() != snapshot_profile:
                    return ""
                if not self._matches_snapshot_locked(
                    snapshot_profile, snapshot_token, snapshot_refresh_token
                ):
                    return self.auth_data.get("token", "")

                if self._last_refresh_transient_error:
                    now = time.time()
                    expires = self.auth_data.get("expires_timestamp")
                    if expires is None or expires > now:
                        self._transient_refresh_retry_after = (
                            now + _TRANSIENT_REFRESH_COOLDOWN_SECONDS
                        )
                        return self.auth_data.get("token", "")
                    self._transient_refresh_retry_after = 0.0
                    return ""

                # Silently clear credentials to force re-login
                self.auth_data = {}
                self._transient_refresh_retry_after = 0.0
                failure = self._persist_auth_data(expected_profile=snapshot_profile)

            if failure is not None:
                self._warn_save_failed()
            self._schedule_ui_login_state_update()
            return ""

    def _should_refresh_token(self):
        """Check if token needs to be refreshed (less than 1 day to expiration)"""
        with self._lock:
            if "expires_timestamp" not in self.auth_data:
                return False  # No expiry info, can't determine
            time_remaining = self.auth_data["expires_timestamp"] - time.time()
            return time_remaining < 86400  # 1 day in seconds

    def refresh_token(self) -> bool:
        """Attempt to refresh the access token using refresh token"""
        with self._lock:
            self._last_refresh_transient_error = False
            if not self.auth_data or "refresh_token" not in self.auth_data:
                return False
            snapshot_token = self.auth_data.get("token")
            snapshot_refresh_token = self.auth_data.get("refresh_token")
            snapshot_profile = config_manager.current_profile_name()

        try:
            response = requests.post(
                f"{API_BASE_URL}/refreshToken",
                json={"refresh_token": snapshot_refresh_token},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if response.status_code == 200:
                new_auth = response.json()
                new_data = self._build_login_data(new_auth)
                if new_data is None:
                    return False
                with self._lock:
                    if not self._matches_snapshot_locked(
                        snapshot_profile, snapshot_token, snapshot_refresh_token
                    ):
                        # Profile switched or auth changed concurrently; treat as
                        # transient so we never clobber the active profile/login.
                        self._last_refresh_transient_error = True
                        return False
                    failure = self._commit_login_data_locked(
                        new_data, expected_profile=snapshot_profile
                    )
                if failure is not None:
                    self._warn_save_failed()
                return True
            if response.status_code in (408, 429) or response.status_code >= 500:
                with self._lock:
                    self._last_refresh_transient_error = True
                return False
            return False
        except Exception:
            with self._lock:
                self._last_refresh_transient_error = True
            return False

    def is_logged_in(self):
        """Check if user has a valid token"""
        return self.get_token() != ""

    def _schedule_ui_login_state_update(self):
        if mw and getattr(mw, "taskman", None):

            def _on_main():
                try:
                    from .menu import update_ui_for_login_state

                    update_ui_for_login_state()
                except Exception:
                    pass

            try:
                mw.taskman.run_on_main(_on_main)
            except Exception:
                pass

    def handle_auth_failure(self):
        """Handle a 401 response by clearing credentials locally and warning the user.

        Unlike logout(), this does NOT contact the server (the token is already
        invalid on the server side). Safe to call from any thread.
        """
        with self._lock:
            if not self.auth_data:
                return  # Already logged out

            self.auth_data = {}
            self._last_refresh_transient_error = False
            self._transient_refresh_retry_after = 0.0
            failure = self._persist_auth_data()

        if failure is not None:
            self._warn_save_failed()

        self._schedule_ui_login_state_update()

    def logout(self):
        """Perform logout by invalidating the token and clearing local storage"""
        with self._lock:
            token = self.auth_data.get("token") if self.auth_data else None
        if token:
            try:
                # Tell server to invalidate the token via Bearer auth
                requests.post(
                    f"{API_BASE_URL}/removeToken",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
            except Exception as e:
                # Server-side token may remain valid until expiry
                logging.getLogger(__name__).warning(
                    "Failed to invalidate token on server: %s", e
                )

        with self._lock:
            self.auth_data = {}
            self._last_refresh_transient_error = False
            self._transient_refresh_retry_after = 0.0
            failure = self._persist_auth_data()
        if failure is not None:
            self._warn_save_failed()


auth_manager = AuthManager()
