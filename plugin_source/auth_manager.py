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


class AuthManager:
    """
    Handles AnkiCollab auth token storage.
    """

    def __init__(self):
        self.auth_data = {}
        self._lock = threading.Lock()
        self._loaded = False

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

    def _load_auth_data(self):
        """Load the active profile's auth data."""
        with self._lock:
            self.auth_data = config_manager.get_auth()
            self._loaded = True

    def _save_auth_data(self):
        """Persist the active profile's auth data.

        Never raises: a read-only or full ``user_files`` must not take down a
        login, or a 401 handler running on a background thread.
        """
        failure = None
        with self._lock:
            try:
                config_manager.write_auth(self.auth_data)
            except config_manager.NoActiveProfileError:
                # A 401 or a logout can arrive while no profile is open
                # (shutdown, profile switch). Warning beats crashing the caller.
                logging.getLogger(__name__).warning(
                    "AnkiCollab: no profile is open; auth data was not saved"
                )
            except OSError as exc:
                failure = exc
                logging.getLogger(__name__).warning(
                    "AnkiCollab: could not save the login locally: %s", exc
                )
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

    def store_login_result(self, auth_response):
        if not auth_response:
            return False

        self.auth_data = {
            "token": auth_response.get("token", ""),
            "refresh_token": auth_response.get("refresh_token", ""),
        }

        if "expires_at" in auth_response:
            try:
                expires_val = auth_response["expires_at"]
                if isinstance(expires_val, (int, float)):
                    self.auth_data["expires_timestamp"] = float(expires_val)
                elif isinstance(expires_val, str):
                    # Parse ISO format date, handle timezone
                    expires_str = expires_val.replace("Z", "+00:00")
                    expires_dt = datetime.fromisoformat(expires_str)
                    self.auth_data["expires_timestamp"] = expires_dt.timestamp()
                else:
                    raise TypeError("Invalid type for expires_at")
            except Exception:
                self.auth_data["expires_timestamp"] = time.time() + (
                    30 * 86400
                )  # 30 days

        self._save_auth_data()
        return True

    def get_token(self):
        """Get the current access token, refreshing if needed."""
        if not self._loaded:
            self._load_auth_data()

        if not self.auth_data or "token" not in self.auth_data:
            return ""

        # Check if token needs refresh (less than 1 day remaining)
        if self._should_refresh_token():
            if not self.refresh_token():
                # Silently clear credentials to force re-login
                self.auth_data = {}
                self._save_auth_data()
                return ""

        return self.auth_data.get("token", "")

    def _should_refresh_token(self):
        """Check if token needs to be refreshed (less than 1 day to expiration)"""
        if "expires_timestamp" not in self.auth_data:
            return False  # No expiry info, can't determine
        time_remaining = self.auth_data["expires_timestamp"] - time.time()
        return time_remaining < 86400  # 1 day in seconds

    def refresh_token(self):
        """Attempt to refresh the access token using refresh token"""
        if not self.auth_data or "refresh_token" not in self.auth_data:
            return False

        try:
            response = requests.post(
                f"{API_BASE_URL}/refreshToken",
                json={"refresh_token": self.auth_data["refresh_token"]},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            if response.status_code == 200:
                new_auth = response.json()
                return self.store_login_result(new_auth)
            else:
                return False
        except Exception:
            return False

    def is_logged_in(self):
        """Check if user has a valid token"""
        return self.get_token() != ""

    def handle_auth_failure(self):
        """Handle a 401 response by clearing credentials locally and warning the user.

        Unlike logout(), this does NOT contact the server (the token is already
        invalid on the server side). Safe to call from any thread.
        """
        if not self.auth_data:
            return  # Already logged out

        self.auth_data = {}
        self._save_auth_data()

        if mw and mw.taskman:

            def _on_main():
                from .menu import update_ui_for_login_state

                update_ui_for_login_state()

            mw.taskman.run_on_main(_on_main)

    def logout(self):
        """Perform logout by invalidating the token and clearing local storage"""
        if self.auth_data and "token" in self.auth_data:
            try:
                # Tell server to invalidate the token via Bearer auth
                token = self.auth_data["token"]
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

        self.auth_data = {}
        self._save_auth_data()


auth_manager = AuthManager()
