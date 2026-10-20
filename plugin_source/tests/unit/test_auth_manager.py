"""Tests for auth_manager.py — per-profile credential storage, refresh, expiration."""

import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import config_manager
from auth_manager import AuthManager


def _auth_file():
    """The active test profile's on-disk auth.json."""
    return config_manager.profile_dir("TestProfile") / "auth.json"


def _saved_auth():
    """What is actually on disk, not the in-memory cache."""
    return json.loads(_auth_file().read_text(encoding="utf-8"))


@pytest.fixture
def am(tmp_path):
    """A fresh AuthManager bound to a sandboxed profile."""
    config_manager.reset_for_tests(tmp_path / "user_files")
    config_manager.activate_for_tests("TestProfile")
    manager = AuthManager()
    manager._loaded = True
    return manager


class TestAuthManagerInit:
    def test_loads_the_profiles_auth_on_first_access(self, tmp_path):
        """A manager built before a profile's auth.json exists still finds it."""
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("TestProfile")
        config_manager.write_auth({"token": "abc", "refresh_token": "ref"})

        manager = AuthManager()  # not loaded yet

        assert manager.get_token() == "abc"
        assert manager.auth_data == {"token": "abc", "refresh_token": "ref"}


class TestStoreLoginResult:
    def test_stores_token_and_refresh(self, am):
        result = am.store_login_result({"token": "tok_123", "refresh_token": "ref_456"})

        assert result is True
        assert am.auth_data["token"] == "tok_123"
        assert am.auth_data["refresh_token"] == "ref_456"
        saved = _saved_auth()
        assert saved["token"] == "tok_123"
        assert saved["refresh_token"] == "ref_456"

    def test_returns_false_on_none_or_empty_dict(self, am):
        assert am.store_login_result(None) is False
        assert am.store_login_result({}) is False

    @pytest.mark.parametrize(
        "expires_at", [time.time() + 86400, int(time.time()) + 86400]
    )
    def test_stores_numeric_expires_at(self, am, expires_at):
        am.store_login_result(
            {"token": "t", "refresh_token": "r", "expires_at": expires_at}
        )
        assert am.auth_data["expires_timestamp"] == pytest.approx(float(expires_at))

    def test_stores_iso_string_expires_at(self, am):
        am.store_login_result(
            {
                "token": "t",
                "refresh_token": "r",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )
        assert am.auth_data["expires_timestamp"] > time.time()

    def test_stores_timezone_aware_iso_string(self, am):
        expires = "2099-01-01T01:00:00+01:00"
        am.store_login_result({"token": "t", "expires_at": expires})
        expected = datetime.fromisoformat(expires).timestamp()
        assert am.auth_data["expires_timestamp"] == pytest.approx(expected)

    def test_fallback_on_bad_expires(self, am):
        before = time.time()
        am.store_login_result(
            {"token": "t", "refresh_token": "r", "expires_at": object()}
        )
        assert am.auth_data["expires_timestamp"] >= before + 29 * 86400


class TestShouldRefreshToken:
    @pytest.mark.parametrize(
        ("expires_timestamp", "expected"),
        [
            (None, False),
            (time.time() + 7 * 86400, False),
            (time.time() + 3600, True),
            (time.time() - 100, True),
        ],
    )
    def test_refresh_threshold(self, am, expires_timestamp, expected):
        am.auth_data = (
            {"token": "t"}
            if expires_timestamp is None
            else {"expires_timestamp": expires_timestamp}
        )
        assert am._should_refresh_token() is expected


class TestGetToken:
    def test_returns_token_when_valid(self, am):
        config_manager.write_auth(
            {
                "token": "valid",
                "refresh_token": "r",
                "expires_timestamp": time.time() + 7 * 86400,
            }
        )
        am._loaded = False  # force a read from the profile's auth.json

        assert am.get_token() == "valid"

    def test_returns_empty_when_no_auth(self, am):
        assert am.get_token() == ""

    @patch("auth_manager.requests.post")
    def test_auto_refreshes_near_expiry(self, mock_post, am):
        config_manager.write_auth(
            {
                "token": "old_tok",
                "refresh_token": "ref",
                "expires_timestamp": time.time() + 100,
            }
        )
        am._loaded = False
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "token": "new_tok",
            "refresh_token": "new_ref",
            "expires_at": time.time() + 7 * 86400,
        }
        mock_post.return_value = response

        assert am.get_token() == "new_tok"
        mock_post.assert_called_once()
        assert "/refreshToken" in mock_post.call_args.args[0]

    @patch("auth_manager.requests.post")
    def test_clears_credentials_if_refresh_fails(self, mock_post, am):
        config_manager.write_auth(
            {
                "token": "old",
                "refresh_token": "bad",
                "expires_timestamp": time.time() + 10,
            }
        )
        am._loaded = False
        mock_post.return_value = MagicMock(status_code=401)

        assert am.get_token() == ""
        assert am.auth_data == {}
        assert config_manager.get_auth() == {}
        assert _saved_auth() == {}


class TestRefreshToken:
    @pytest.fixture
    def logged_in(self, am):
        am.auth_data = {"token": "old", "refresh_token": "ref_old"}
        return am

    @patch("auth_manager.requests.post")
    def test_refresh_success(self, mock_post, logged_in):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "token": "new_tok",
            "refresh_token": "new_ref",
        }
        mock_post.return_value = response

        assert logged_in.refresh_token() is True
        assert logged_in.auth_data["token"] == "new_tok"
        assert logged_in.auth_data["refresh_token"] == "new_ref"
        assert "/refreshToken" in mock_post.call_args.args[0]
        assert mock_post.call_args.kwargs["json"] == {"refresh_token": "ref_old"}
        assert mock_post.call_args.kwargs["timeout"] == 15

    @patch("auth_manager.requests.post")
    def test_refresh_failure(self, mock_post, logged_in):
        mock_post.return_value = MagicMock(status_code=401)
        assert logged_in.refresh_token() is False

    @patch("auth_manager.requests.post", side_effect=Exception("network error"))
    def test_refresh_exception(self, mock_post, logged_in):
        assert logged_in.refresh_token() is False

    def test_refresh_no_refresh_token(self, am):
        am.auth_data = {}
        assert am.refresh_token() is False


class TestIsLoggedIn:
    def test_logged_in_with_valid_token(self, am):
        config_manager.write_auth(
            {"token": "abc", "expires_timestamp": time.time() + 7 * 86400}
        )
        am._loaded = False
        assert am.is_logged_in() is True

    def test_not_logged_in_without_token(self, am):
        assert am.is_logged_in() is False


class TestLogout:
    @patch("auth_manager.requests.post")
    def test_logout_clears_data_and_storage(self, mock_post, am):
        am.auth_data = {"token": "t", "refresh_token": "r"}
        mock_post.return_value = MagicMock(status_code=200)

        am.logout()

        assert am.auth_data == {}
        assert config_manager.get_auth() == {}
        assert _saved_auth() == {}

    @patch("auth_manager.requests.post")
    def test_logout_sends_bearer_header(self, mock_post, am):
        am.auth_data = {"token": "my_secret_token"}
        mock_post.return_value = MagicMock(status_code=200)

        am.logout()

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["headers"]["Authorization"] == (
            "Bearer my_secret_token"
        )
        assert "/removeToken" in mock_post.call_args.args[0]

    @patch("auth_manager.requests.post", side_effect=Exception("network"))
    def test_logout_succeeds_even_on_network_error(self, mock_post, am):
        am.auth_data = {"token": "t"}

        am.logout()

        assert am.auth_data == {}
        assert config_manager.get_auth() == {}
        assert _saved_auth() == {}


class TestHandleAuthFailure:
    def test_clears_credentials_without_server_request(self, am):
        am.auth_data = {
            "token": "t",
            "refresh_token": "r",
            "expires_timestamp": 123,
        }

        with patch("auth_manager.requests.post") as mock_post:
            am.handle_auth_failure()

        mock_post.assert_not_called()
        assert am.auth_data == {}
        assert config_manager.get_auth() == {}
        assert _saved_auth() == {}


class TestSaveFailureIsContained:
    """A read-only user_files must not take down a login (spec section 6)."""

    def test_a_write_failure_does_not_escape(self, tmp_path, monkeypatch, mw_mock):
        import auth_manager as auth_manager_module
        import config_manager
        from auth_manager import AuthManager

        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("TestProfile")

        def boom(*args, **kwargs):
            raise PermissionError(13, "read-only filesystem")

        monkeypatch.setattr(config_manager.tempfile, "mkstemp", boom)

        # Stand in for aqt.utils.showInfo: real Anki has it, the fake aqt
        # module used by the suite does not.
        fake_aqt = MagicMock()
        monkeypatch.setattr(auth_manager_module, "aqt", fake_aqt, raising=False)

        manager = AuthManager()
        manager.auth_data = {"token": "abc"}
        manager._save_auth_data()  # must not raise

        # The failure must still reach the user, dispatched to the main thread.
        mw_mock.taskman.run_on_main.assert_called_once()
        fake_aqt.utils.showInfo.assert_called_once()
        assert "could not save your login" in (
            fake_aqt.utils.showInfo.call_args.args[0].lower()
        )


class TestProfileSwitchReloadsAuth:
    def test_switching_profiles_swaps_the_token(self, tmp_path, mw_mock):
        import config_manager
        from auth_manager import AuthManager

        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")
        config_manager.write_auth({"token": "medicine-token"})
        config_manager.activate_for_tests("Languages")
        config_manager.write_auth({"token": "languages-token"})

        manager = AuthManager()  # registers the listener
        config_manager.activate_for_tests("Medicine")
        assert manager.get_token() == "medicine-token"

        config_manager.activate_for_tests("Languages")
        assert manager.get_token() == "languages-token"


class TestSaveWithoutProfile:
    def test_saving_without_a_profile_does_not_raise(self, tmp_path):
        config_manager.reset_for_tests(tmp_path / "user_files")
        manager = AuthManager()
        manager.auth_data = {"token": "abc"}
        manager._save_auth_data()  # must log, not raise
        assert config_manager.get_auth() == {}
