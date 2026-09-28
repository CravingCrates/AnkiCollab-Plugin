"""Tests for auth_manager.py — JSON credential storage, refresh, and expiration."""

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from auth_manager import AuthManager


@pytest.fixture
def auth_env(tmp_path, mw_mock):
    """Give each test isolated config, hooks, and on-disk credential storage."""
    config = {}
    mw_mock.addonManager.getConfig.side_effect = lambda *args, **kwargs: dict(config)
    mw_mock.addonManager.writeConfig.side_effect = lambda key, value: config.update(
        value
    )

    with (
        patch("auth_manager.mw", mw_mock),
        patch("aqt.gui_hooks.main_window_did_init.append"),
    ):
        am = AuthManager()
        # Avoid depending on the add-on's on-disk directory.
        am._user_files_dir = lambda: str(tmp_path)
        yield am, config, tmp_path


class TestAuthManagerInit:
    def test_registers_load_hook(self, auth_env):
        # The fixture constructs the manager with the hook registration patched.
        am, _, _ = auth_env
        assert am._loaded is False
        assert am.auth_data == {}

    def test_loads_empty_auth_data_when_no_config(self, auth_env):
        am, _, _ = auth_env
        am._load_auth_data()
        assert am.auth_data == {}
        assert am._loaded is True

    def test_loads_settings_and_json_secrets(self, auth_env):
        am, config, tmp_path = auth_env
        config["auth"] = {"auto_approve": True, "theme": "dark"}
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "token": "abc",
                    "refresh_token": "ref",
                    "expires_timestamp": 12345,
                }
            ),
            encoding="utf-8",
        )

        am._load_auth_data()

        assert am.auth_data == {
            "auto_approve": True,
            "theme": "dark",
            "token": "abc",
            "refresh_token": "ref",
            "expires_timestamp": 12345,
        }

    def test_migrates_legacy_secrets_from_config(self, auth_env):
        am, config, tmp_path = auth_env
        config["auth"] = {
            "token": "legacy-token",
            "refresh_token": "legacy-refresh",
            "expires_timestamp": 123.0,
            "auto_approve": True,
        }

        am._load_auth_data()

        assert am.auth_data["token"] == "legacy-token"
        assert am.auth_data["refresh_token"] == "legacy-refresh"
        assert am.auth_data["expires_timestamp"] == 123.0
        assert am.auth_data["auto_approve"] is True
        assert set(json.loads((tmp_path / "auth.json").read_text())) == {
            "token",
            "refresh_token",
            "expires_timestamp",
        }
        assert config["auth"] == {"auto_approve": True}


class TestStoreLoginResult:
    def test_stores_token_and_refresh(self, auth_env):
        am, _, tmp_path = auth_env
        result = am.store_login_result({"token": "tok_123", "refresh_token": "ref_456"})

        assert result is True
        assert am.auth_data["token"] == "tok_123"
        assert am.auth_data["refresh_token"] == "ref_456"
        saved = json.loads((tmp_path / "auth.json").read_text())
        assert saved["token"] == "tok_123"
        assert saved["refresh_token"] == "ref_456"

    def test_returns_false_on_none_or_empty_dict(self, auth_env):
        am, _, _ = auth_env
        assert am.store_login_result(None) is False
        assert am.store_login_result({}) is False

    def test_stores_numeric_expires_at(self, auth_env):
        am, _, _ = auth_env
        future_ts = time.time() + 86400
        am.store_login_result(
            {"token": "t", "refresh_token": "r", "expires_at": future_ts}
        )
        assert am.auth_data["expires_timestamp"] == pytest.approx(future_ts)

    def test_stores_iso_string_expires_at(self, auth_env):
        am, _, _ = auth_env
        am.store_login_result(
            {
                "token": "t",
                "refresh_token": "r",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        )
        assert am.auth_data["expires_timestamp"] > time.time()

    def test_stores_timezone_aware_iso_string(self, auth_env):
        am, _, _ = auth_env
        expires = "2099-01-01T01:00:00+01:00"
        am.store_login_result({"token": "t", "expires_at": expires})
        expected = datetime.fromisoformat(expires).timestamp()
        assert am.auth_data["expires_timestamp"] == pytest.approx(expected)

    def test_fallback_on_bad_expires(self, auth_env):
        am, _, _ = auth_env
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
    def test_refresh_threshold(self, auth_env, expires_timestamp, expected):
        am, _, _ = auth_env
        am.auth_data = (
            {"token": "t"}
            if expires_timestamp is None
            else {"expires_timestamp": expires_timestamp}
        )
        assert am._should_refresh_token() is expected


class TestGetToken:
    def test_returns_token_when_valid(self, auth_env):
        am, _, tmp_path = auth_env
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "token": "valid",
                    "refresh_token": "r",
                    "expires_timestamp": time.time() + 7 * 86400,
                }
            ),
            encoding="utf-8",
        )
        assert am.get_token() == "valid"

    def test_returns_empty_when_no_auth(self, auth_env):
        am, _, _ = auth_env
        assert am.get_token() == ""

    @patch("auth_manager.requests.post")
    def test_auto_refreshes_near_expiry(self, mock_post, auth_env):
        am, _, tmp_path = auth_env
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "token": "old_tok",
                    "refresh_token": "ref",
                    "expires_timestamp": time.time() + 100,
                }
            ),
            encoding="utf-8",
        )
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
    def test_clears_credentials_if_refresh_fails(self, mock_post, auth_env):
        am, _, tmp_path = auth_env
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "token": "old",
                    "refresh_token": "bad",
                    "expires_timestamp": time.time() + 10,
                }
            ),
            encoding="utf-8",
        )
        mock_post.return_value = MagicMock(status_code=401)

        assert am.get_token() == ""
        assert am.auth_data == {}
        assert json.loads((tmp_path / "auth.json").read_text()) == {}


class TestRefreshToken:
    @pytest.fixture
    def am(self, auth_env):
        am, _, _ = auth_env
        am.auth_data = {"token": "old", "refresh_token": "ref_old"}
        return am

    @patch("auth_manager.requests.post")
    def test_refresh_success(self, mock_post, am):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "token": "new_tok",
            "refresh_token": "new_ref",
        }
        mock_post.return_value = response

        assert am.refresh_token() is True
        assert am.auth_data["token"] == "new_tok"
        assert am.auth_data["refresh_token"] == "new_ref"
        assert "/refreshToken" in mock_post.call_args.args[0]
        assert mock_post.call_args.kwargs["json"] == {"refresh_token": "ref_old"}
        assert mock_post.call_args.kwargs["timeout"] == 15

    @patch("auth_manager.requests.post")
    def test_refresh_failure(self, mock_post, am):
        mock_post.return_value = MagicMock(status_code=401)
        assert am.refresh_token() is False

    @patch("auth_manager.requests.post", side_effect=Exception("network error"))
    def test_refresh_exception(self, mock_post, am):
        assert am.refresh_token() is False

    def test_refresh_no_refresh_token(self, am):
        am.auth_data = {}
        assert am.refresh_token() is False


class TestIsLoggedIn:
    def test_logged_in_with_valid_token(self, auth_env):
        am, _, tmp_path = auth_env
        (tmp_path / "auth.json").write_text(
            json.dumps({"token": "abc", "expires_timestamp": time.time() + 7 * 86400}),
            encoding="utf-8",
        )
        assert am.is_logged_in() is True

    def test_not_logged_in_without_token(self, auth_env):
        am, _, _ = auth_env
        assert am.is_logged_in() is False


class TestSettings:
    def test_auto_approve_defaults_to_false(self, auth_env):
        am, _, _ = auth_env
        assert am.get_auto_approve() is False

    def test_set_and_get_auto_approve(self, auth_env):
        am, config, _ = auth_env
        am.set_auto_approve(True)
        assert am.get_auto_approve() is True
        assert config["auth"]["auto_approve"] is True

    def test_settings_are_saved_separately_from_secrets(self, auth_env):
        am, config, tmp_path = auth_env
        am.auth_data = {
            "token": "secret",
            "refresh_token": "refresh-secret",
            "expires_timestamp": 123.0,
            "auto_approve": True,
        }
        am._save_auth_data()

        assert config["auth"] == {"auto_approve": True}
        saved = json.loads((tmp_path / "auth.json").read_text())
        assert saved == {
            "token": "secret",
            "refresh_token": "refresh-secret",
            "expires_timestamp": 123.0,
        }


class TestLogout:
    @patch("auth_manager.requests.post")
    def test_logout_clears_data_and_storage(self, mock_post, auth_env):
        am, _, tmp_path = auth_env
        am.auth_data = {"token": "t", "refresh_token": "r"}
        mock_post.return_value = MagicMock(status_code=200)

        am.logout()

        assert am.auth_data == {}
        assert json.loads((tmp_path / "auth.json").read_text()) == {}

    @patch("auth_manager.requests.post")
    def test_logout_sends_bearer_header(self, mock_post, auth_env):
        am, _, _ = auth_env
        am.auth_data = {"token": "my_secret_token"}
        mock_post.return_value = MagicMock(status_code=200)

        am.logout()

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["headers"]["Authorization"] == (
            "Bearer my_secret_token"
        )
        assert "/removeToken" in mock_post.call_args.args[0]

    @patch("auth_manager.requests.post", side_effect=Exception("network"))
    def test_logout_succeeds_even_on_network_error(self, mock_post, auth_env):
        am, _, tmp_path = auth_env
        am.auth_data = {"token": "t"}

        am.logout()

        assert am.auth_data == {}
        assert json.loads((tmp_path / "auth.json").read_text()) == {}


class TestHandleAuthFailure:
    def test_clears_credentials_without_server_request(self, auth_env):
        am, _, tmp_path = auth_env
        am.auth_data = {
            "token": "t",
            "refresh_token": "r",
            "expires_timestamp": 123,
        }

        with patch("auth_manager.requests.post") as mock_post:
            am.handle_auth_failure()

        mock_post.assert_not_called()
        assert am.auth_data == {}
        assert json.loads((tmp_path / "auth.json").read_text()) == {}


class TestJsonStorageFailures:
    def test_missing_or_invalid_json_loads_as_empty(self, auth_env):
        am, _, tmp_path = auth_env
        path = tmp_path / "auth.json"

        assert am._read_secrets() == {}
        path.write_text("{not valid json", encoding="utf-8")
        assert am._read_secrets() == {}

    def test_write_failure_shows_user_message(self, auth_env):
        am, _, _ = auth_env
        with (
            patch.object(am, "_auth_file_path", return_value="/no-such-dir/auth.json"),
            patch("auth_manager.aqt.utils.showInfo") as show_info,
        ):
            am._write_secrets({"token": "secret"})

        show_info.assert_called_once()
        assert "could not save" in show_info.call_args.args[0].lower()
