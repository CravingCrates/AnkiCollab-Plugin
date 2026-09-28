import os
import json
import time
import logging
import threading
import requests
from datetime import datetime

from aqt import mw
import aqt.utils
from aqt.qt import *

from .var_defs import API_BASE_URL

_SECRET_KEYS = ("token", "refresh_token", "expires_timestamp")


class AuthManager:
    """
    Handles AnkiCollab auth token storage.
    """

    def __init__(self):
        self.config_key = __name__
        self.auth_data = {}
        self._lock = threading.Lock()
        self._loaded = False

        from aqt import gui_hooks

        gui_hooks.main_window_did_init.append(self._load_auth_data)

    def _user_files_dir(self):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_files")
        os.makedirs(d, exist_ok=True)
        return d

    def _auth_file_path(self):
        return os.path.join(self._user_files_dir(), "auth.json")

    def _read_settings(self):
        strings_data = mw.addonManager.getConfig(self.config_key) or {}
        return strings_data.get("auth", {})

    def _write_settings(self, settings_payload):
        strings_data = mw.addonManager.getConfig(self.config_key) or {}
        strings_data["auth"] = settings_payload
        mw.addonManager.writeConfig(self.config_key, strings_data)

    def _read_secrets(self):
        try:
            path = self._auth_file_path()
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to read stored credentials: %s", e
            )
            return {}

    def _write_secrets(self, secrets_payload):
        path = self._auth_file_path()
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(secrets_payload, f)
            os.replace(tmp_path, path)  # atomic on the same filesystem
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to save credentials: %s", e)
            aqt.utils.showInfo(
                "AnkiCollab could not save your login locally. "
                "You may need to log in again next time you start Anki."
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _load_auth_data(self):
        """Load settings + credentials"""
        with self._lock:
            settings = self._read_settings()
            secrets = self._read_secrets()

            if not secrets and any(k in settings for k in _SECRET_KEYS):
                secrets = {k: settings.pop(k) for k in _SECRET_KEYS if k in settings}
                self._write_secrets(secrets)
                self._write_settings(settings)

            self.auth_data = {**settings, **secrets}
            self._loaded = True

    def _save_auth_data(self):
        """Persist settings and credentials to their respective stores."""
        with self._lock:
            secrets = {
                k: self.auth_data[k] for k in _SECRET_KEYS if k in self.auth_data
            }
            settings = {
                k: v for k, v in self.auth_data.items() if k not in _SECRET_KEYS
            }
            self._write_secrets(secrets)
            self._write_settings(settings)

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

    def get_auto_approve(self):
        """Get auto-approve setting"""
        if not self._loaded:
            self._load_auth_data()
        return self.auth_data.get("auto_approve", False)

    def set_auto_approve(self, value):
        """Set auto-approve setting"""
        self.auth_data["auto_approve"] = bool(value)
        self._save_auth_data()

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
