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
import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest
import requests

import config_manager
import import_manager
import startup_sync
from api_client import ApiConnectionError
from import_manager import (
    CacheArchiveRefreshError,
    CacheBootstrapError,
    _resolve_cache_bootstrap_entries,
    _subscription_from_manifest,
    async_start_pull,
    handle_pull,
    import_webresult,
    remove_nonexistent_decks,
    update_stats,
    validate_user_token,
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
                side_effect=lambda entries, **_kw: entries,
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

    @pytest.mark.parametrize(
        "config",
        [None, {}, {"settings": {"pull_on_startup": True}, "auth": {"token": "x"}}],
    )
    def test_without_subscriptions_nothing_is_requested(self, mw_mock, config):
        config_manager.write_config(config)
        with (
            patch("import_manager.remove_nonexistent_decks") as remove,
            patch("import_manager.api_client.post_json") as post,
        ):
            assert async_start_pull(None, silent=True) is None

        remove.assert_called_once_with(silent=True)
        post.assert_not_called()

    def test_remove_nonexistent_decks_skips_when_only_settings_and_auth(self, mw_mock):
        config_manager.write_config(
            {"settings": {"pull_on_startup": True}, "auth": {"token": "x"}}
        )
        with patch("import_manager.api_client.post_json") as post:
            remove_nonexistent_decks(silent=True)

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

        if silent:
            mocks["tooltip"].assert_not_called()
        else:
            mocks["tooltip"].assert_called_once()
        mocks["backup"].assert_not_called()
        mocks["stats"].assert_called_once_with(silent=silent)

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


class TestSilentPullAndBackgroundGuards:
    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError("offline"),
            requests.exceptions.Timeout("timeout"),
            ApiConnectionError("offline"),
        ],
    )
    def test_silent_pull_offline_errors_stay_quiet_and_skip_sentry(self, mw_mock, exc):
        config_manager.write_config({"hash1": {"deckId": 0}})
        with (
            patch("import_manager.remove_nonexistent_decks") as remove,
            patch("import_manager.api_client.post_json", side_effect=exc),
            patch("import_manager.sentry_sdk.capture_exception") as sentry,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            result = async_start_pull(None, silent=True)

        assert result == (None, None, True)
        remove.assert_called_once_with(silent=True)
        tooltip.assert_not_called()
        sentry.assert_not_called()

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ConnectionError("offline"),
            requests.exceptions.Timeout("timeout"),
            ApiConnectionError("offline"),
        ],
    )
    def test_silent_remove_nonexistent_decks_offline_skips_sentry(self, mw_mock, exc):
        config_manager.write_config({"hash1": {"deckId": 0}})
        with (
            patch("import_manager.api_client.post_json", side_effect=exc),
            patch("import_manager.sentry_sdk.capture_exception") as sentry,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            remove_nonexistent_decks(silent=True)

        sentry.assert_not_called()
        tooltip.assert_not_called()

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.HTTPError("500"),
            OSError("disk"),
            ValueError("bad json"),
            gzip.BadGzipFile("bad gzip"),
            base64.binascii.Error("bad b64"),
        ],
    )
    def test_silent_pull_non_network_and_decode_errors_show_tooltip(self, mw_mock, exc):
        config_manager.write_config({"hash1": {"deckId": 0}})
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", side_effect=exc),
            patch("import_manager.sentry_sdk.capture_exception") as sentry,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            result = async_start_pull(None, silent=True)

        assert result == (None, None, True)
        sentry.assert_called_once()
        tooltip.assert_called_once()

    def test_silent_pull_server_5xx_shows_tooltip(self, mw_mock):
        config_manager.write_config({"hash1": {"deckId": 0}})
        resp = MagicMock(status_code=503)
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=resp),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            result = async_start_pull(None, silent=True)

        assert result == (None, None, True)
        tooltip.assert_called_once()

    def test_silent_cache_bootstrap_network_cause_stays_quiet(self, mw_mock):
        config_manager.write_config({"hash1": {"deckId": 0}})
        err = CacheBootstrapError("Unable to download cache manifest")
        err.__cause__ = requests.exceptions.ConnectionError("offline")
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json",
                return_value=_pull_response({"hash1": "x"}),
            ),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=err,
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as notify,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            result = async_start_pull(None, silent=True)

        assert result == (None, None, True)
        notify.assert_not_called()
        tooltip.assert_not_called()

    def test_silent_cache_bootstrap_non_network_error_uses_tooltip_not_modal(
        self, mw_mock
    ):
        config_manager.write_config({"hash1": {"deckId": 0}})
        err = CacheBootstrapError("invalid manifest json")
        err.__cause__ = ValueError("bad json")
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json",
                return_value=_pull_response({"hash1": "x"}),
            ),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=err,
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as notify,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            result = async_start_pull(None, silent=True)

        assert result == (None, None, True)
        notify.assert_not_called()
        tooltip.assert_called_once()

    def test_silent_pull_defers_cache_bootstrap_when_media_sync_active(self, mw_mock):
        config_manager.write_config({"hash1": {"deckId": 0}})
        notified = []
        payload = [
            {
                "mode": "cache-bootstrap",
                "deck_hash": "hash1",
                "manifest": {
                    "manifest_presigned_url": "http://x/m.json",
                    "archive_presigned_url": "http://x/a.zip",
                },
            }
        ]
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json",
                return_value=_pull_response(payload),
            ),
            patch("import_manager._fetch_manifest") as fetch_manifest,
            patch("import_manager._subscription_from_manifest") as sub_from_manifest,
            patch("import_manager.sentry_sdk.capture_exception") as sentry,
        ):
            result = async_start_pull(
                None,
                silent=True,
                notify=notified.append,
                defer_cache_bootstrap=True,
            )

        assert result == (None, None, True)
        fetch_manifest.assert_not_called()
        sub_from_manifest.assert_not_called()
        sentry.assert_not_called()
        assert len(notified) == 1
        assert "Update Decks" in notified[0]

    def test_manual_pull_does_not_defer_cache_bootstrap(self, mw_mock):
        config_manager.write_config({"hash1": {"deckId": 0}})
        payload = [
            {
                "mode": "cache-bootstrap",
                "deck_hash": "hash1",
                "manifest": {
                    "manifest_presigned_url": "http://x/m.json",
                    "archive_presigned_url": "http://x/a.zip",
                },
            }
        ]
        sub = {"deck_hash": "hash1", "deck": {"name": "Deck"}}
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json",
                return_value=_pull_response(payload),
            ),
            patch("import_manager._fetch_manifest", return_value={}) as fetch_manifest,
            patch(
                "import_manager._subscription_from_manifest", return_value=sub
            ) as sub_from_manifest,
        ):
            result = async_start_pull(None, silent=False, defer_cache_bootstrap=True)

        assert result == ([sub], None, False)
        fetch_manifest.assert_called_once()
        sub_from_manifest.assert_called_once()

    def test_silent_pull_still_applies_incremental_updates_when_deferring_cache_bootstrap(
        self, mw_mock
    ):
        config_manager.write_config({"hash1": {"deckId": 1}})
        notified = []
        payload = [{"deck_hash": "hash1", "deck": {"name": "Deck"}}]
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json",
                return_value=_pull_response(payload),
            ),
        ):
            result = async_start_pull(
                None,
                silent=True,
                notify=notified.append,
                defer_cache_bootstrap=True,
            )

        assert result == (payload, None, True)
        assert notified == []


class TestUpdateStatsAndHandlePull:
    def test_update_stats_skips_when_collection_unavailable(self, mw_mock, mw_no_col):
        with patch("import_manager.QueryOp") as query_op:
            update_stats(silent=True)

        query_op.assert_not_called()

    def test_update_stats_silent_skips_with_progress_and_attaches_failure(
        self, mw_mock
    ):
        config_manager.write_config(
            {"hash1": {"deckId": 1, "stats_enabled": True, "last_stats_timestamp": 0}}
        )
        with (
            patch("import_manager.consent.has_granted_consent", return_value=True),
            patch("import_manager.ReviewHistory"),
            patch("import_manager._retry_pending_erasures"),
            patch("import_manager.QueryOp") as query_op,
        ):
            op_inst = query_op.return_value
            op_inst.failure.return_value = op_inst
            update_stats(silent=True)

        op_inst.with_progress.assert_not_called()
        op_inst.failure.assert_called_once()
        failure_cb = op_inst.failure.call_args.args[0]
        failure_cb(RuntimeError("upload failed"))
        op_inst.run_in_background.assert_called_once()

        # Silent success callback must NOT call mw.progress.finish() and must persist watermark.
        success_cb = query_op.call_args.kwargs["success"]
        success_cb(4242)
        mw_mock.progress.finish.assert_not_called()
        assert (
            config_manager.get_stored_config()["hash1"]["last_stats_timestamp"] == 4242
        )

    def test_update_stats_non_silent_keeps_with_progress(self, mw_mock):
        config_manager.write_config(
            {"hash1": {"deckId": 1, "stats_enabled": True, "last_stats_timestamp": 0}}
        )
        with (
            patch("import_manager.consent.has_granted_consent", return_value=True),
            patch("import_manager.ReviewHistory"),
            patch("import_manager._retry_pending_erasures"),
            patch("import_manager.QueryOp") as query_op,
        ):
            op_inst = query_op.return_value
            update_stats(silent=False)

        op_inst.with_progress.assert_called_once_with("Uploading Review History...")
        op_inst.with_progress.return_value.run_in_background.assert_called_once()

        success_cb = query_op.call_args.kwargs["success"]
        success_cb(777)
        mw_mock.progress.finish.assert_called_once()
        assert (
            config_manager.get_stored_config()["hash1"]["last_stats_timestamp"] == 777
        )

    def test_silent_cache_download_skips_progress_updates_and_cancel_checks(
        self, mw_mock
    ):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", json.dumps({"deck": {"name": "Remote"}}).encode())
        archive_bytes = buf.getvalue()

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Length": str(len(archive_bytes))}
        resp.iter_content.return_value = [archive_bytes]
        resp.close = MagicMock()
        session = MagicMock()
        session.get.return_value = resp
        mw_mock.progress.want_cancel.return_value = True

        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        entry = {
            "mode": "cache-bootstrap",
            "deck_hash": "h1",
            "manifest": {
                "manifest_presigned_url": "http://x/m.json",
                "archive_presigned_url": "http://x/a.zip",
            },
        }
        with (
            patch("import_manager._fetch_manifest", return_value=manifest),
            patch(
                "import_manager.api_client.session_with_retries", return_value=session
            ),
        ):
            resolved = _resolve_cache_bootstrap_entries([entry], silent=True)

        assert len(resolved) == 1
        assert resolved[0]["deck"]["name"] == "Remote"
        mw_mock.progress.update.assert_not_called()
        mw_mock.progress.want_cancel.assert_not_called()

    def test_non_silent_cache_download_skips_progress_update_when_not_busy(
        self, mw_mock
    ):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", json.dumps({"deck": {"name": "Remote"}}).encode())
        archive_bytes = buf.getvalue()

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Length": str(len(archive_bytes))}
        resp.iter_content.return_value = [archive_bytes]
        resp.close = MagicMock()
        session = MagicMock()
        session.get.return_value = resp
        mw_mock.progress.want_cancel.return_value = False
        mw_mock.progress.busy.return_value = False

        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            sub = _subscription_from_manifest(
                "h1", manifest, "http://x/a.zip", silent=False
            )

        assert sub["deck"]["name"] == "Remote"
        mw_mock.progress.update.assert_not_called()

    def test_handle_pull_respects_concurrency_lock(self, mw_mock):
        assert startup_sync.acquire_pull_slot() is not None
        with (
            patch("import_manager.QueryOp") as query_op,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            handle_pull(None, silent=False)

        query_op.assert_not_called()
        tooltip.assert_called_once()

        with (
            patch("import_manager.QueryOp") as query_op,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            handle_pull(None, silent=True)

        query_op.assert_not_called()
        tooltip.assert_not_called()

    def test_handle_pull_with_input_hash_cleans_up_pending_sub_when_slot_busy(
        self, mw_mock
    ):
        assert startup_sync.acquire_pull_slot() is not None
        config_manager.write_config(
            {
                "pending_hash": {"deckId": 0, "timestamp": "2022-12-31 23:59:59"},
                "installed_hash": {"deckId": 42, "timestamp": "2025-01-01 00:00:00"},
            }
        )
        with (
            patch("import_manager.QueryOp") as query_op,
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            handle_pull("pending_hash", silent=False)

        query_op.assert_not_called()
        tooltip.assert_called_once()
        stored = config_manager.get_stored_config()
        assert "pending_hash" not in stored
        assert "installed_hash" in stored

        # Existing installed deck (deckId != 0) must never be purged on collision
        with patch("import_manager.QueryOp") as query_op:
            handle_pull("installed_hash", silent=False)
        assert "installed_hash" in config_manager.get_stored_config()

    def test_handle_pull_silent_uses_without_collection_and_no_progress(self, mw_mock):
        with (
            patch("import_manager.point_version", return_value=240000),
            patch("import_manager.QueryOp") as query_op,
        ):
            op_inst = query_op.return_value
            op_inst.failure.return_value = op_inst
            op_inst.without_collection.return_value = op_inst
            handle_pull(None, silent=True, validate_token=True)

            assert startup_sync.is_pull_in_progress() is True
            op_inst.with_progress.assert_not_called()
            op_inst.without_collection.assert_called_once()
            op_inst.run_in_background.assert_called_once()

            success_cb = query_op.call_args.kwargs["success"]
            with patch("import_manager.import_webresult"):
                success_cb((None, None, True))
            assert startup_sync.is_pull_in_progress() is False

    def test_handle_pull_runs_validate_token_in_bg_op_only(self, mw_mock):
        with (
            patch("import_manager.QueryOp") as query_op,
            patch("import_manager.validate_user_token", return_value=False) as val,
            patch("import_manager.async_start_pull") as pull,
        ):
            op_inst = query_op.return_value
            op_inst.failure.return_value = op_inst
            handle_pull(None, silent=False, validate_token=True)

            # Main thread must NOT validate token synchronously
            val.assert_not_called()

            bg_op = query_op.call_args.kwargs["op"]
            res = bg_op(mw_mock.col)

        val.assert_called_once()
        assert val.call_args.kwargs["silent"] is False
        assert callable(val.call_args.kwargs["can_notify"])
        pull.assert_not_called()
        assert res == (None, None, False)

    def test_handle_pull_releases_slot_on_failure_and_launch_error(self, mw_mock):
        with patch("import_manager.QueryOp") as query_op:
            op_inst = query_op.return_value
            op_inst.failure.return_value = op_inst
            handle_pull(None, silent=False, validate_token=False)

            op_inst.with_progress.assert_called_once_with(
                "Fetching Changes from AnkiCollab..."
            )
            failure_cb = op_inst.failure.call_args.args[0]
            with patch.object(import_manager.aqt.utils, "tooltip") as tooltip:
                failure_cb(RuntimeError("boom"))
            assert startup_sync.is_pull_in_progress() is False
            tooltip.assert_called_once()

        with patch("import_manager.QueryOp") as query_op:
            op_inst = query_op.return_value
            op_inst.failure.return_value = op_inst
            op_inst.with_progress.return_value.run_in_background.side_effect = (
                RuntimeError("launch failed")
            )
            handle_pull(None, silent=False)

        assert startup_sync.is_pull_in_progress() is False

    def test_validate_user_token_preserves_auth_on_5xx_and_offline(self, mw_mock):
        resp_503 = MagicMock(status_code=503, text="unavailable")
        with (
            patch("import_manager.auth_manager.get_token", return_value="tok"),
            patch("import_manager.auth_manager.handle_auth_failure") as handle_fail,
            patch("import_manager.api_client.post_empty", return_value=resp_503),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            assert validate_user_token(silent=False) is False

        handle_fail.assert_not_called()
        tooltip.assert_called_once()

        with (
            patch("import_manager.auth_manager.get_token", return_value="tok"),
            patch("import_manager.auth_manager.handle_auth_failure") as handle_fail,
            patch(
                "import_manager.api_client.post_empty",
                side_effect=ApiConnectionError("offline"),
            ),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            assert validate_user_token(silent=True) is False

        handle_fail.assert_not_called()
        tooltip.assert_not_called()

    def test_validate_user_token_clears_auth_on_403_or_false_body(self, mw_mock):
        resp_403 = MagicMock(status_code=403, text="forbidden")
        with (
            patch("import_manager.auth_manager.get_token", return_value="tok"),
            patch("import_manager.auth_manager.handle_auth_failure") as handle_fail,
            patch("import_manager.api_client.post_empty", return_value=resp_403),
        ):
            assert validate_user_token(silent=False) is False
        handle_fail.assert_called_once()

        resp_false = MagicMock(status_code=200, text="false")
        with (
            patch("import_manager.auth_manager.get_token", return_value="tok"),
            patch("import_manager.auth_manager.handle_auth_failure") as handle_fail,
            patch("import_manager.api_client.post_empty", return_value=resp_false),
        ):
            assert validate_user_token(silent=False) is False
        handle_fail.assert_called_once()

    def test_validate_user_token_cross_profile_guard_and_runtime_error_feedback(
        self, mw_mock
    ):
        resp_403 = MagicMock(status_code=403, text="forbidden")
        with (
            patch("import_manager.auth_manager.get_token", return_value="tok"),
            patch("import_manager.auth_manager.handle_auth_failure") as handle_fail,
            patch("import_manager.api_client.post_empty", return_value=resp_403),
        ):
            assert validate_user_token(silent=True, can_notify=lambda: False) is False
        handle_fail.assert_not_called()

        with (
            patch("import_manager.auth_manager.get_token", return_value="tok"),
            patch(
                "import_manager.api_client.post_empty",
                side_effect=RuntimeError("Not logged in"),
            ),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            assert validate_user_token(silent=False) is False
        tooltip.assert_called_once()

    def test_remove_nonexistent_decks_skips_noop_and_profile_switch_writes(
        self, mw_mock
    ):
        config_manager.write_config({"hash1": {"deckId": 1}})
        ok_empty = MagicMock(status_code=200, content=b"[]")
        with (
            patch("import_manager.api_client.post_json", return_value=ok_empty),
            patch.object(import_manager.config_manager, "write_config") as write_cfg,
        ):
            remove_nonexistent_decks(silent=True)
        write_cfg.assert_not_called()

        # Unknown hash not in config -> no write either
        ok_unknown = MagicMock(status_code=200, content=b'["other_hash"]')
        with (
            patch("import_manager.api_client.post_json", return_value=ok_unknown),
            patch.object(import_manager.config_manager, "write_config") as write_cfg,
        ):
            remove_nonexistent_decks(silent=True)
        write_cfg.assert_not_called()

        # Profile switch while /CheckDeckAlive is in flight -> abort write!
        config_manager.activate_for_tests("OtherProfile")
        config_manager.write_config({"hash1": {"deckId": 99}})
        config_manager.activate_for_tests("TestProfile")
        config_manager.write_config({"hash1": {"deckId": 1}})
        ok_remove = MagicMock(status_code=200, content=b'["hash1"]')

        def _post_and_switch(*_a, **_kw):
            config_manager.activate_for_tests("OtherProfile")
            return ok_remove

        with (
            patch("import_manager.api_client.post_json", side_effect=_post_and_switch),
            patch.object(import_manager.config_manager, "write_config") as write_cfg,
        ):
            remove_nonexistent_decks(silent=True)
        write_cfg.assert_not_called()
        assert config_manager.get_stored_config() == {"hash1": {"deckId": 99}}

    def test_async_start_pull_uses_custom_notify_for_server_errors(self, mw_mock):
        config_manager.write_config({"hash1": {"deckId": 0}})
        notified = []
        resp = MagicMock(status_code=503)
        with (
            patch("import_manager.api_client.post_json", return_value=resp),
            patch("import_manager.remove_nonexistent_decks"),
            patch.object(import_manager.aqt.utils, "tooltip") as tooltip,
        ):
            result = async_start_pull(None, silent=True, notify=notified.append)

        assert result == (None, None, True)
        assert len(notified) == 1
        tooltip.assert_not_called()

    def test_manual_pull_without_subscriptions_notifies_up_to_date(self, mw_mock):
        config_manager.write_config({"settings": {"pull_on_startup": True}})
        notified = []
        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json") as post,
        ):
            result = async_start_pull(None, silent=False, notify=notified.append)

        assert result is None
        post.assert_not_called()
        assert len(notified) == 1
        assert "up to date" in notified[0].lower()

    def test_pending_subscription_with_zero_deck_id_uses_install_update_on_general_pull(
        self, mw_mock
    ):
        config_manager.write_config(
            {"pending_hash": {"deckId": 0, "timestamp": "2022-12-31 23:59:59"}}
        )
        sub = {"deck_hash": "pending_hash", "deck": {"name": "PendingDeck"}}
        with (
            patch("import_manager.is_collection_available", return_value=True),
            patch("import_manager.create_backup"),
            patch("import_manager.install_update") as install_mock,
            patch("import_manager.show_changelog_popup") as changelog_mock,
            patch("import_manager.update_stats"),
        ):
            import_webresult(([sub], None, True))

        install_mock.assert_called_once_with(sub, "pending_hash", silent=True)
        changelog_mock.assert_not_called()

    def test_silent_on_deck_installed_suppresses_rating_prompt_and_registers_zero_deck_id(
        self, mw_mock
    ):
        config_manager.write_config(
            {
                "pending_hash": {"deckId": 0, "timestamp": "2022-12-31 23:59:59"},
                "existing_hash": {"deckId": 10, "timestamp": "2025-01-01 00:00:00"},
            }
        )
        mw_mock.col.decks.id.return_value = 77
        deck = MagicMock()
        deck.anki_dict = {"name": "InstalledDeck"}

        with (
            patch("import_manager.is_collection_available", return_value=True),
            patch("import_manager.ask_for_rating") as rate_mock,
            patch("import_manager._handle_stats_sharing_after_import"),
        ):
            # Silent update of existing deck must NOT trigger ask_for_rating
            import_manager._on_deck_installed(
                (1, {"success": True}),
                deck,
                {
                    "deck_hash": "existing_hash",
                    "stats_enabled": False,
                    "deleted_notes": [],
                },
                input_hash=None,
                silent=True,
            )
            rate_mock.assert_not_called()

            # General pull (input_hash=None) of a pending deck (deckId == 0) registers deckId
            import_manager._on_deck_installed(
                (1, {"success": True}),
                deck,
                {
                    "deck_hash": "pending_hash",
                    "stats_enabled": True,
                    "deleted_notes": [],
                },
                input_hash=None,
                silent=True,
            )
            rate_mock.assert_not_called()

        stored = config_manager.get_stored_config()
        assert stored["pending_hash"]["deckId"] == 77
        assert stored["pending_hash"]["stats_enabled"] is True
