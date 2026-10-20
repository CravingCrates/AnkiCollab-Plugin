"""Tests for pulling deck changes and applying the result.

* ``async_start_pull`` decides *what* is asked of the server (which
  subscriptions, with credentials stripped out of the payload) and what the user
  is told when the server, the network or the cache archive fails.
* ``import_webresult`` is the step that turns a pull result into an install —
  it must not import on top of a failed backup, and it must not silently do
  nothing when there is something to install.

Assertions target request payloads, decided outcomes and the collection backup
gate.  Wording of user-facing messages is not pinned.
"""

from __future__ import annotations

import base64
import gzip
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import config_manager
import import_manager
from import_manager import (
    CacheArchiveRefreshError,
    CacheBootstrapError,
    async_start_pull,
    import_webresult,
)
from utils import BackupFailedError, OperationAbortedError


def _encoded(payload):
    return base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8")))


def _pull_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = _encoded(payload)
    return resp


class TestPullRequest:
    def _run(self, mw_mock, config, input_hash=None, silent=False, response=None):
        config_manager.write_config(config)
        with (
            patch("import_manager.remove_nonexistent_decks") as remove,
            patch(
                "import_manager.api_client.post_json",
                return_value=response or _pull_response({}),
            ) as post,
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=lambda entries: entries,
            ),
        ):
            result = async_start_pull(input_hash, silent)
        return result, post, remove

    def test_credentials_and_settings_are_never_sent_to_the_server(self, mw_mock):
        config = {
            "settings": {"push_counter": 3},
            "auth": {"token": "x"},
            "hash1": {"deckId": 1},
        }
        # The token now travels in the Authorization header; the payload must stay free of it.
        with patch("import_manager.auth_manager.is_logged_in", return_value=True):
            _result, post, remove = self._run(mw_mock, config)

        assert post.call_args.args == ("/pullChanges", {"hash1": {"deckId": 1}})
        assert post.call_args.kwargs == {"auth": True}
        remove.assert_called_once()
        # The stored config is not modified by the copy that is sent.
        stored = config_manager.get_stored_config()
        assert "settings" in stored and "auth" in stored

    def test_the_token_is_sent_when_signed_in(self, mw_mock):
        # Deliberately amends an older guarantee: /pullChanges used to be anonymous, but the server
        # now needs the caller's identity to count active subscribers and restore deleted decks.
        config = {"hash1": {"deckId": 1}}
        with patch("import_manager.auth_manager.is_logged_in", return_value=True):
            _result, post, _remove = self._run(mw_mock, config)

        assert post.call_args.kwargs == {"auth": True}

    def test_a_signed_out_pull_stays_anonymous(self, mw_mock):
        config = {"hash1": {"deckId": 1}}
        with patch("import_manager.auth_manager.is_logged_in", return_value=False):
            _result, post, _remove = self._run(mw_mock, config)

        assert post.call_args.kwargs == {"auth": False}

    def test_a_new_subscription_pull_sends_only_that_deck(self, mw_mock):
        config = {"hash1": {"deckId": 1}, "hash2": {"deckId": 2}}
        _result, post, _remove = self._run(mw_mock, config, input_hash="hash2")
        assert post.call_args.args[1] == {"hash2": {"deckId": 2}}

    def test_an_unknown_hash_sends_an_empty_request(self, mw_mock):
        _result, post, _remove = self._run(
            mw_mock, {"hash1": {"deckId": 1}}, input_hash="missing"
        )
        assert post.call_args.args[1] == {}

    @pytest.mark.parametrize("config", [None, {}])
    def test_without_subscriptions_nothing_is_requested(self, mw_mock, config):
        config_manager.write_config(config)
        with (
            patch("import_manager.remove_nonexistent_decks") as remove,
            patch("import_manager.api_client.post_json") as post,
        ):
            assert async_start_pull(None) is None

        remove.assert_called_once()
        post.assert_not_called()

    def test_a_successful_pull_returns_the_decoded_entries(self, mw_mock):
        payload = {"hash1": {"deck": {"name": "X"}}}
        result, post, _remove = self._run(
            mw_mock, {"hash1": {"deckId": 0}}, response=_pull_response(payload)
        )

        assert result == (payload, None, False)
        assert post.call_count == 1

    def test_the_silent_flag_is_echoed_back(self, mw_mock):
        result, _post, _remove = self._run(
            mw_mock, {"hash1": {"deckId": 0}}, silent=True
        )
        assert result == ({}, None, True)


class TestPullFailures:
    def _call(self, mw_mock, side_effect, input_hash=None, config=None):
        tooltips = []
        mw_mock.taskman.run_on_main.side_effect = lambda fn: fn()
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", side_effect=side_effect),
            patch.object(
                import_manager.aqt.utils,
                "tooltip",
                side_effect=lambda *a, **kw: tooltips.append((a, kw)),
            ),
            patch("import_manager._remove_subscription_from_config") as remove,
        ):
            config_manager.write_config(
                {"hash1": {"deckId": 0}} if config is None else config
            )
            result = async_start_pull(input_hash)
        return result, tooltips, remove

    def test_an_expired_session_fails_quietly(self, mw_mock):
        resp = MagicMock(status_code=401)
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=resp),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            config_manager.write_config({"hash1": {"deckId": 0}})
            assert async_start_pull(None) == (None, None, False)

        tooltip.assert_not_called()

    def test_a_server_error_is_shown_to_the_user(self, mw_mock):
        resp = MagicMock(status_code=503)
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=resp),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            config_manager.write_config({"hash1": {"deckId": 0}})
            assert async_start_pull(None) == (None, None, False)

        tooltip.assert_called_once()

    def test_a_new_subscription_is_purged_when_the_user_cancels(self, mw_mock):
        result, tooltips, remove = self._call(
            mw_mock, OperationAbortedError("cancelled"), input_hash="hash1"
        )

        assert result == (None, None, True)
        remove.assert_called_once_with("hash1")
        assert tooltips

    def test_an_existing_subscription_survives_a_cancelled_update(self, mw_mock):
        result, tooltips, remove = self._call(
            mw_mock, OperationAbortedError("cancelled")
        )

        assert result == (None, None, True)
        remove.assert_not_called()
        assert tooltips

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError("net"),
            OSError("disk"),
            ValueError("bad"),
            gzip.BadGzipFile("bad gzip"),
            base64.binascii.Error("bad b64"),
        ],
    )
    def test_transport_and_decode_errors_are_reported_not_raised(self, mw_mock, exc):
        result, tooltips, _remove = self._call(mw_mock, exc)

        assert result == (None, None, False)
        assert tooltips

    def test_an_unexpected_error_is_reported_not_raised(self, mw_mock):
        result, tooltips, _remove = self._call(mw_mock, RuntimeError("boom"))

        assert result == (None, None, False)
        assert tooltips


class TestPullCacheRefresh:
    def _config(self, mw_mock):
        config_manager.write_config({"hash1": {"deckId": 0}})

    def test_an_expired_archive_url_is_retried_once_with_the_same_request(
        self, mw_mock
    ):
        first = _pull_response({"hash1": "old"})
        second = _pull_response({"hash1": "new"})
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json", side_effect=[first, second]
            ) as post,
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=[CacheArchiveRefreshError("expired"), "resolved"],
            ),
        ):
            self._config(mw_mock)
            result = async_start_pull(None)

        assert result == ("resolved", None, False)
        assert post.call_count == 2
        assert post.call_args_list[0].args == post.call_args_list[1].args

    def test_a_failed_refresh_asks_the_user_to_retry_later(self, mw_mock):
        first = _pull_response({"hash1": "old"})
        second = MagicMock(status_code=500)
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", side_effect=[first, second]),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=CacheArchiveRefreshError("expired"),
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as notify,
        ):
            self._config(mw_mock)
            result = async_start_pull(None)

        assert result == (None, None, False)
        notify.assert_called_once()

    def test_a_broken_archive_after_refresh_asks_the_user_to_retry_later(self, mw_mock):
        first = _pull_response({"hash1": "old"})
        second = _pull_response({"hash1": "new"})
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", side_effect=[first, second]),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=[
                    CacheArchiveRefreshError("expired"),
                    CacheBootstrapError("still broken"),
                ],
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as notify,
        ):
            self._config(mw_mock)
            result = async_start_pull(None)

        assert result == (None, None, False)
        notify.assert_called_once()

    def test_a_broken_archive_without_an_expired_url_is_not_retried(self, mw_mock):
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json",
                return_value=_pull_response({"hash1": "x"}),
            ) as post,
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=CacheBootstrapError("bad manifest"),
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as notify,
        ):
            self._config(mw_mock)
            result = async_start_pull(None)

        assert result == (None, None, False)
        assert post.call_count == 1
        notify.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# import_webresult
# ──────────────────────────────────────────────────────────────────────


class TestImportWebresult:
    def _run(self, mw_mock, data):
        with (
            patch("import_manager.is_collection_available", return_value=True),
            patch("import_manager.create_backup") as backup,
            patch("import_manager.install_update") as install_update,
            patch("import_manager.show_changelog_popup") as changelog,
            patch("import_manager.update_stats") as stats,
            patch.object(import_manager.aqt.utils, "showWarning") as warning,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            import_webresult(data)
        return {
            "backup": backup,
            "install_update": install_update,
            "changelog": changelog,
            "stats": stats,
            "warning": warning,
            "tooltip": tooltip,
        }

    def test_a_cancelled_pull_is_a_no_op(self, mw_mock):
        mocks = self._run(mw_mock, (None, None, False))

        assert mocks["backup"].call_count == 0
        assert mocks["tooltip"].call_count == 0
        assert mocks["stats"].call_count == 0

    def test_without_a_collection_nothing_is_imported(self, mw_mock):
        with (
            patch("import_manager.is_collection_available", return_value=False),
            patch.object(import_manager.aqt.utils, "showWarning") as warning,
            patch("import_manager.create_backup") as backup,
        ):
            import_webresult(([{"deck": {}}], None, False))

        warning.assert_called_once()
        backup.assert_not_called()

    @pytest.mark.parametrize("silent", [True, False])
    def test_an_up_to_date_deck_backs_nothing_up(self, mw_mock, silent):
        mocks = self._run(mw_mock, ({}, None, silent))

        mocks["tooltip"].assert_called_once()
        mocks["backup"].assert_not_called()
        mocks["stats"].assert_called_once()

    def test_a_failed_backup_aborts_the_import(self, mw_mock):
        with (
            patch("import_manager.is_collection_available", return_value=True),
            patch(
                "import_manager.create_backup", side_effect=BackupFailedError("disk")
            ),
            patch("import_manager.install_update") as install_update,
            patch("import_manager.show_changelog_popup") as changelog,
            patch("import_manager.update_stats") as stats,
            patch.object(import_manager.aqt.utils, "showWarning") as warning,
        ):
            import_webresult(([{"deck": {}}], None, False))

        warning.assert_called_once()
        install_update.assert_not_called()
        changelog.assert_not_called()
        stats.assert_not_called()

    def test_a_new_deck_is_installed(self, mw_mock):
        subscription = {"deck": {"name": "New"}, "deck_hash": "h"}
        mocks = self._run(mw_mock, ([subscription], "input-hash", False))

        mocks["backup"].assert_called_once_with(critical=True)
        mocks["install_update"].assert_called_once_with(subscription, "input-hash")
        mocks["changelog"].assert_not_called()
        mocks["stats"].assert_called_once()

    def test_an_existing_deck_shows_its_changelog(self, mw_mock):
        subscription = {"deck": {"name": "Existing"}, "deck_hash": "h"}
        mocks = self._run(mw_mock, ([subscription], None, False))

        mocks["changelog"].assert_called_once_with(subscription)
        mocks["install_update"].assert_not_called()
        mocks["stats"].assert_called_once()

    def test_every_subscription_in_the_result_is_processed(self, mw_mock):
        subs = [{"deck_hash": "a"}, {"deck_hash": "b"}]
        mocks = self._run(mw_mock, (subs, None, False))

        assert [c.args for c in mocks["changelog"].call_args_list] == [
            (subs[0],),
            (subs[1],),
        ]
