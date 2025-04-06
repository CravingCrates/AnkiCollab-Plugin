import os
import json
import time
import requests
from datetime import datetime
from aqt import mw
import aqt.utils
from aqt.qt import *
from aqt import mw

from .var_defs import API_BASE_URL

class AuthManager:    
    def __init__(self):
        self.config_key = __name__
        self._load_auth_data()
    
    def _load_auth_data(self):
        """Load authentication data from Anki config"""
        self.auth_data = {}
        strings_data = mw.addonManager.getConfig(self.config_key)
        if strings_data and "auth" in strings_data:
            self.auth_data = strings_data["auth"]
    
    def _save_auth_data(self):
        """Save authentication data to Anki config"""
        strings_data = mw.addonManager.getConfig(self.config_key) or {}
        if "auth" not in strings_data:
            strings_data["auth"] = {}
        
        strings_data["auth"] = self.auth_data
        mw.addonManager.writeConfig(self.config_key, strings_data)
    
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
                print(f"Received expiration date: {expires_val}")
                if isinstance(expires_val, int) or isinstance(expires_val, float):
                    self.auth_data["expires_timestamp"] = float(expires_val)
                elif isinstance(expires_val, str):
                    # Parse ISO format date, handle timezone
                    expires_str = expires_val.replace("Z", "+00:00")
                    expires_dt = datetime.fromisoformat(expires_str)
                    self.auth_data["expires_timestamp"] = expires_dt.timestamp()
                else:
                    raise TypeError("Invalid type for expires_at")
            except Exception as e:
                print(f"Error parsing expiration date: {e}")
                self.auth_data["expires_timestamp"] = time.time() + (30 * 86400)  # 30 days
        
        strings_data = mw.addonManager.getConfig(self.config_key) or {}
        if "settings" in strings_data: #legacy
            self.auth_data["auto_approve"] = strings_data["settings"].get("auto_approve", False)
        
        # Save to config
        self._save_auth_data()
        return True
    
    def get_token(self):
        """Get the current access token, refreshing if needed"""
        self._load_auth_data()  # Reload in case it changed
        
        if not self.auth_data or "token" not in self.auth_data:
            return ""
            
        # Check if token needs refresh (less than 1 day remaining)
        if self._should_refresh_token():
            if not self.refresh_token():
                # If refresh fails, return empty token to force re-login
                return ""
        
        return self.auth_data.get("token", "")
    
    def _should_refresh_token(self):
        """Check if token needs to be refreshed (less than 1 day to expiration)"""
        if "expires_timestamp" not in self.auth_data:
            return False  # No expiry info, can't determine
            
        # Refresh if less than 1 day remaining
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
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code == 200:
                new_auth = response.json()
                return self.store_login_result(new_auth)
            else:
                print(f"Token refresh failed: {response.text}")
                return False
        except Exception as e:
            print(f"Error refreshing token: {e}")
            return False
    
    def is_logged_in(self):
        """Check if user has a valid token"""
        return self.get_token() != ""
    
    def get_auto_approve(self):
        """Get auto-approve setting"""
        self._load_auth_data()
        return self.auth_data.get("auto_approve", False)
    
    def set_auto_approve(self, value):
        """Set auto-approve setting"""
        self._load_auth_data()
        self.auth_data["auto_approve"] = bool(value)
        self._save_auth_data()
    
    def logout(self):
        """Perform logout by invalidating the token and clearing local storage"""
        if self.auth_data and "token" in self.auth_data:
            try:
                # Tell server to invalidate the token
                token = self.auth_data["token"]
                requests.get(f"{API_BASE_URL}/removeToken/{token}")
            except Exception as e:
                print(f"Error during logout: {e}")
        
        # Clear stored credentials regardless of server response
        self.auth_data = {}
        self._save_auth_data()
        
        # Update legacy storage for compatibility
        strings_data = mw.addonManager.getConfig(self.config_key) or {}
        if "settings" in strings_data:
            strings_data["settings"]["token"] = ""
            mw.addonManager.writeConfig(self.config_key, strings_data)

# singleton
auth_manager = AuthManager()


# Legacy function for backward compatibility
def store_login_token(token):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        if "settings" not in strings_data:
            strings_data["settings"] = {}
        strings_data["settings"]["token"] = token
        strings_data["settings"]["auto_approve"] = auth_manager.get_auto_approve()
    mw.addonManager.writeConfig(__name__, strings_data)
    
