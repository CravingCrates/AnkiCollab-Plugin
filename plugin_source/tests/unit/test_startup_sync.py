"""Tests for startup_sync.py and hooks.py startup/manual update integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys

import pytest
import requests

import config_manager
import hooks
import startup_sync
from api_client import ApiConnectionError
from aqt.qt import QWidget


class _FakeAnkiHook:
    """Mimics Anki's generated hook objects: non-iterable, stores callbacks in _hooks."""

    def __init__(self) -> None:
        self._hooks: list = []

    def append(self, fn) -> None:
        self._hooks.append(fn)

    def remove(self, fn) -> None:
        self._hooks.remove(fn)

    def __call__(self, *args, **kwargs) -> None:
        for fn in list(self._hooks):
            fn(*args, **kwargs)

    def __iter__(self):
        raise TypeError("Anki Hook objects are not iterable")


class _SyncQueryOp:
    """Fake QueryOp recording chain calls and running op + success/failure on demand."""

    last_instance = None

    def __init__(self, *, parent=None, op=None, success=None) -> None:
        self.parent = parent
        self.op = op
        self.success = success
        self.failure_cb = None
        self.with_progress_calls: list[str] = []
        self.without_collection_called = False
        self.run_called = False
        _SyncQueryOp.last_instance = self

    def with_progress(self, label: str):
        self.with_progress_calls.append(label)
        return self

    def without_collection(self):
        self.without_collection_called = True
        return self

    def failure(self, cb):
        self.failure_cb = cb
        return self

    def run_in_background(self) -> None:
        self.run_called = True


def _setup_ready_profile(
    mw_mock,
    *,
    profile_name: str = "TestProfile",
    pull_on_startup: bool = True,
    has_token: bool = True,
    subscribed: bool = True,
    sync_auth: object = "sync-auth-token",
    auto_sync: bool = True,
    can_auto_sync: bool = True,
    state: str = "deckBrowser",
) -> None:
    if config_manager.current_profile_name() != profile_name:
        config_manager.activate_for_tests(profile_name)
    cfg: dict = {"settings": {"pull_on_startup": pull_on_startup}}
    if subscribed:
        cfg["deck_hash_1"] = {"deckId": 1, "timestamp": "2025-01-01 00:00:00"}
    config_manager.write_config(cfg)
    if has_token:
        config_manager.write_auth({"token": "tok-123", "refresh_token": "ref-123"})
    else:
        config_manager.clear_auth()
    # Reload auth_manager state cleanly for the test profile
    startup_sync.auth_manager._load_auth_data()

    mw_mock.pm.sync_auth.return_value = sync_auth
    mw_mock.pm.auto_syncing_enabled.return_value = auto_sync
    mw_mock.can_auto_sync.return_value = can_auto_sync
    mw_mock.safeMode = False
    mw_mock.restoring_backup = False
    mw_mock.state = state


def _make_gui_hooks():
    return SimpleNamespace(
        sync_will_start=_FakeAnkiHook(),
        sync_did_finish=_FakeAnkiHook(),
        collection_will_temporarily_close=_FakeAnkiHook(),
        collection_did_temporarily_close=_FakeAnkiHook(),
    )


class TestHookRegistration:
    def test_register_hooks_is_reload_safe_on_non_iterable_anki_hooks(self, mw_mock):
        fake_hooks = _make_gui_hooks()
        # MagicMock attributes are truthy by default; marker check must require `is True`.
        other_cb = MagicMock()
        fake_hooks.sync_will_start.append(other_cb)
        fake_hooks.sync_did_finish.append(other_cb)

        with patch.object(startup_sync, "gui_hooks", fake_hooks):
            startup_sync.register_hooks()
            startup_sync.register_hooks()

        # Other addon/Anki callbacks are preserved, and ours appears exactly once per hook.
        assert fake_hooks.sync_will_start._hooks[0] is other_cb
        assert fake_hooks.sync_did_finish._hooks[0] is other_cb
        assert len(fake_hooks.sync_will_start._hooks) == 2
        assert len(fake_hooks.sync_did_finish._hooks) == 2
        assert len(fake_hooks.collection_will_temporarily_close._hooks) == 1
        assert len(fake_hooks.collection_did_temporarily_close._hooks) == 1

        # Firing hooks updates _ankiweb_sync_active accurately.
        fake_hooks.sync_will_start()
        assert startup_sync._ankiweb_sync_active is True
        fake_hooks.sync_did_finish()
        assert startup_sync._ankiweb_sync_active is False

    def test_register_hooks_works_with_plain_list_hooks(self, mw_mock):
        plain_hooks = SimpleNamespace(sync_will_start=[], sync_did_finish=[])
        with patch.object(startup_sync, "gui_hooks", plain_hooks):
            startup_sync.register_hooks()
            startup_sync.register_hooks()

        assert len(plain_hooks.sync_will_start) == 1
        assert len(plain_hooks.sync_did_finish) == 1


class TestMainThreadPreconditions:
    def test_zero_network_and_no_get_token_on_main_thread(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
            patch.object(startup_sync.auth_manager, "get_token") as get_token,
            patch.object(startup_sync.auth_manager, "refresh_token") as refresh_token,
            patch.object(startup_sync.auth_manager, "is_logged_in") as is_logged_in,
            patch.object(startup_sync.api_client, "post_empty") as post_empty,
            patch("requests.post") as req_post,
            patch("requests.get") as req_get,
        ):
            startup_sync.on_profile_opened()

        get_token.assert_not_called()
        refresh_token.assert_not_called()
        is_logged_in.assert_not_called()
        post_empty.assert_not_called()
        req_post.assert_not_called()
        req_get.assert_not_called()
        single_shot.assert_called_once()
        assert single_shot.call_args.args[0] == 0

    def test_skips_in_safe_mode(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        mw_mock.safeMode = True
        with patch.object(startup_sync.QTimer, "singleShot") as single_shot:
            startup_sync.on_profile_opened()
        single_shot.assert_not_called()

    def test_skips_when_restoring_backup(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        mw_mock.restoring_backup = True
        with patch.object(startup_sync.QTimer, "singleShot") as single_shot:
            startup_sync.on_profile_opened()
        single_shot.assert_not_called()

    @pytest.mark.parametrize(
        "cfg",
        [
            {"settings": {"pull_on_startup": False}, "h1": {"deckId": 1}},
            {"settings": {}, "h1": {"deckId": 1}},
            {"settings": "invalid", "h1": {"deckId": 1}},
        ],
    )
    def test_skips_when_pull_on_startup_disabled_or_invalid(self, mw_mock, cfg):
        _setup_ready_profile(mw_mock, sync_auth=None)
        config_manager.write_config(cfg)
        with patch.object(startup_sync.QTimer, "singleShot") as single_shot:
            startup_sync.on_profile_opened()
        single_shot.assert_not_called()

    def test_skips_when_not_logged_in_without_tooltip_or_network(self, mw_mock):
        _setup_ready_profile(mw_mock, has_token=False, sync_auth=None)
        with (
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
            patch.object(startup_sync.auth_manager, "get_token") as get_token,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            startup_sync.on_profile_opened()

        single_shot.assert_not_called()
        get_token.assert_not_called()
        tooltip.assert_not_called()

    def test_skips_when_zero_subscriptions(self, mw_mock):
        _setup_ready_profile(mw_mock, subscribed=False, sync_auth=None)
        with (
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            startup_sync.on_profile_opened()

        single_shot.assert_not_called()
        tooltip.assert_not_called()

    def test_skips_when_no_profile_or_no_collection(self, mw_mock, mw_no_col):
        with (
            patch.object(
                startup_sync.config_manager, "current_profile_name", return_value=None
            ),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
        ):
            startup_sync.on_profile_opened()
        single_shot.assert_not_called()


class TestAnkiWebSyncPolicy:
    def test_case_a_ankiweb_configured_but_autosync_disabled_skips_completely(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=False)
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
            patch.object(startup_sync, "QueryOp") as query_op,
        ):
            startup_sync.on_profile_opened()

        assert fake_hooks.sync_did_finish._hooks == []
        single_shot.assert_not_called()
        query_op.assert_not_called()

    def test_case_b_can_auto_sync_false_skips_without_attaching_orphan_hook(
        self, mw_mock
    ):
        _setup_ready_profile(
            mw_mock, sync_auth="auth", auto_sync=True, can_auto_sync=False
        )
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
        ):
            startup_sync.on_profile_opened()

        assert fake_hooks.sync_did_finish._hooks == []
        single_shot.assert_not_called()

    def test_case_b_arms_one_shot_hook_and_runs_only_once_after_sync_did_finish(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()

            # Not dispatched yet while AnkiWeb sync hasn't finished; no blind fallback timer!
            single_shot.assert_not_called()
            assert len(fake_hooks.sync_did_finish._hooks) == 2

            # First sync_did_finish triggers and immediately detaches the one-shot hook.
            fake_hooks.sync_did_finish()
            assert len(fake_hooks.sync_did_finish._hooks) == 1
            single_shot.assert_called_once()
            assert single_shot.call_args.args[0] == 0

            # Subsequent manual syncs in the same session do not re-trigger startup sync.
            fake_hooks.sync_did_finish()
            single_shot.assert_called_once()

    def test_case_c_local_only_defers_to_next_tick_without_sync_status_check(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, sync_auth=None, auto_sync=False)
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ) as pull,
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
        ):
            startup_sync.on_profile_opened()
            op = _SyncQueryOp.last_instance
            assert op is not None
            assert op.with_progress_calls == []
            assert op.without_collection_called is True
            assert op.run_called is True

            res = op.op(mw_mock.col)
            mw_mock.col.sync_status.assert_not_called()
            op.success(res)

        pull.assert_called_once()
        assert pull.call_args.args == (None,)
        assert pull.call_args.kwargs["silent"] is True
        assert callable(pull.call_args.kwargs["notify"])
        imp.assert_called_once_with(([], None, True))


class TestBackgroundVerificationAndLifecycle:
    def _launch_case_b(self, mw_mock, fake_hooks):
        _SyncQueryOp.last_instance = None
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()
        return _SyncQueryOp.last_instance

    @pytest.mark.parametrize("required_code", [2, None, "unexpected", True])
    def test_skips_silently_when_sync_status_requires_sync(
        self, mw_mock, required_code
    ):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        mw_mock.col.sync_status.return_value = MagicMock(required=required_code)
        op = self._launch_case_b(mw_mock, fake_hooks)

        with (
            patch.object(startup_sync.api_client, "post_empty") as post_empty,
            patch.object(startup_sync.import_manager, "async_start_pull") as async_pull,
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        assert res is startup_sync._SKIPPED
        post_empty.assert_not_called()
        async_pull.assert_not_called()
        imp.assert_not_called()
        tooltip.assert_not_called()
        assert startup_sync.is_pull_in_progress() is False

    @pytest.mark.parametrize("required_code", [0, 1])
    def test_proceeds_when_sync_status_is_no_changes_or_normal_sync(
        self, mw_mock, required_code
    ):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        mw_mock.col.sync_status.return_value = MagicMock(required=required_code)
        op = self._launch_case_b(mw_mock, fake_hooks)

        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ) as post_empty,
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ) as async_pull,
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        post_empty.assert_called_once()
        async_pull.assert_called_once()
        imp.assert_called_once_with(([], None, True))

    def test_skips_silently_when_sync_status_raises_offline(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        mw_mock.col.sync_status.side_effect = RuntimeError("offline")
        op = self._launch_case_b(mw_mock, fake_hooks)

        with (
            patch.object(startup_sync.api_client, "post_empty") as post_empty,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
            patch.object(startup_sync.sentry_sdk, "capture_exception") as sentry,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        assert res is startup_sync._SKIPPED
        post_empty.assert_not_called()
        tooltip.assert_not_called()
        sentry.assert_not_called()

    def test_skips_when_sync_auth_cleared_before_start(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
            patch.object(startup_sync, "QueryOp") as query_op,
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()
            deferred = single_shot.call_args.args[1]
            # Simulate Anki clearing sync_auth on SyncErrorKind.AUTH
            mw_mock.pm.sync_auth.return_value = None
            deferred()

        query_op.assert_not_called()
        assert startup_sync.is_pull_in_progress() is False

    def test_sync_auth_is_read_on_main_thread_not_in_bg_op(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth-obj", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        op = self._launch_case_b(mw_mock, fake_hooks)

        mw_mock.pm.sync_auth.reset_mock()
        mw_mock.col.sync_status.return_value = MagicMock(required=0)
        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ),
        ):
            op.op(mw_mock.col)

        mw_mock.pm.sync_auth.assert_not_called()
        mw_mock.col.sync_status.assert_called_once_with("auth-obj")

    def test_anki_sync_allowed_statuses_safe_against_magicmock_and_missing_module(
        self,
    ):
        assert startup_sync._anki_sync_allowed_statuses() == (0, 1)

        fake_sync = SimpleNamespace(
            SyncStatus=SimpleNamespace(NO_CHANGES=0, NORMAL_SYNC=1)
        )
        with patch.dict(sys.modules, {"anki.sync": fake_sync}):
            assert startup_sync._anki_sync_allowed_statuses() == (0, 1)

        custom_sync = SimpleNamespace(
            SyncStatus=SimpleNamespace(NO_CHANGES=3, NORMAL_SYNC=7)
        )
        with patch.dict(sys.modules, {"anki.sync": custom_sync}):
            assert startup_sync._anki_sync_allowed_statuses() == (3, 7)

        bool_sync = SimpleNamespace(
            SyncStatus=SimpleNamespace(NO_CHANGES=True, NORMAL_SYNC=False)
        )
        with patch.dict(sys.modules, {"anki.sync": bool_sync}):
            assert startup_sync._anki_sync_allowed_statuses() == (0, 1)

        mock_sync = MagicMock()
        with patch.dict(sys.modules, {"anki.sync": mock_sync}):
            assert startup_sync._anki_sync_allowed_statuses() == (0, 1)

    def test_profile_will_close_detaches_hook_before_shutdown_sync(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
        ):
            startup_sync.on_profile_opened()
            assert len(fake_hooks.sync_did_finish._hooks) == 1

            startup_sync.on_profile_will_close()
            assert fake_hooks.sync_did_finish._hooks == []

            # Shutdown AnkiWeb sync completes after profile_will_close
            fake_hooks.sync_did_finish()

        single_shot.assert_not_called()

    def test_mid_flight_close_and_profile_switch_discard_late_results(self, mw_mock):
        _setup_ready_profile(mw_mock, profile_name="ProfileA", sync_auth=None)
        _SyncQueryOp.last_instance = None

        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()

        op_a = _SyncQueryOp.last_instance
        assert startup_sync.is_pull_in_progress() is True

        # User closes ProfileA and opens ProfileB while op_a is still running
        startup_sync.on_profile_will_close()
        _setup_ready_profile(mw_mock, profile_name="ProfileB", sync_auth=None)

        with (
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op_a.success(([{"deck_hash": "h"}], None, True))
            op_a.failure(RuntimeError("late failure"))

        assert startup_sync.is_pull_in_progress() is False
        imp.assert_not_called()
        tooltip.assert_not_called()

    def test_once_per_profile_per_session_multi_profile_behavior(self, mw_mock):
        fake_hooks = _make_gui_hooks()
        _setup_ready_profile(mw_mock, profile_name="ProfileA", sync_auth=None)

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as shot_a,
        ):
            startup_sync.on_profile_opened()
            shot_a.assert_called_once()

        # Switch to ProfileB -> runs once for ProfileB
        startup_sync.on_profile_will_close()
        _setup_ready_profile(mw_mock, profile_name="ProfileB", sync_auth=None)
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as shot_b,
        ):
            startup_sync.on_profile_opened()
            shot_b.assert_called_once()

        # Switch back to ProfileA in same session -> skipped
        startup_sync.on_profile_will_close()
        _setup_ready_profile(mw_mock, profile_name="ProfileA", sync_auth=None)
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as shot_a2,
        ):
            startup_sync.on_profile_opened()
            shot_a2.assert_not_called()


class TestTokenAndResultHandling:
    def _run_local_startup(self, mw_mock, **kwargs):
        _setup_ready_profile(mw_mock, sync_auth=None, **kwargs)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
        return _SyncQueryOp.last_instance

    def test_5xx_during_check_user_token_preserves_credentials_and_shows_tooltip(
        self, mw_mock
    ):
        op = self._run_local_startup(mw_mock)
        resp_503 = MagicMock(status_code=503, text="error")

        with (
            patch.object(startup_sync.api_client, "post_empty", return_value=resp_503),
            patch.object(
                startup_sync.auth_manager, "handle_auth_failure"
            ) as handle_fail,
            patch.object(startup_sync.import_manager, "async_start_pull") as async_pull,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        assert res is startup_sync._SKIPPED
        handle_fail.assert_not_called()
        async_pull.assert_not_called()
        tooltip.assert_called_once()
        assert startup_sync.auth_manager.has_stored_token() is True

    def test_5xx_tooltip_suppressed_if_profile_closed_mid_check(self, mw_mock):
        op = self._run_local_startup(mw_mock)
        resp_503 = MagicMock(status_code=503, text="error")

        def _post_then_close(*_args, **_kwargs):
            startup_sync.on_profile_will_close()
            return resp_503

        with (
            patch.object(
                startup_sync.api_client, "post_empty", side_effect=_post_then_close
            ),
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        assert res is startup_sync._SKIPPED
        tooltip.assert_not_called()

    def test_offline_during_check_user_token_is_silent(self, mw_mock):
        op = self._run_local_startup(mw_mock)
        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                side_effect=ApiConnectionError("offline"),
            ),
            patch.object(
                startup_sync.auth_manager, "handle_auth_failure"
            ) as handle_fail,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
            patch.object(startup_sync.sentry_sdk, "capture_exception") as sentry,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        assert res is startup_sync._SKIPPED
        handle_fail.assert_not_called()
        tooltip.assert_not_called()
        sentry.assert_not_called()

    def test_up_to_date_stays_quiet(self, mw_mock):
        op = self._run_local_startup(mw_mock)
        with (
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
            patch.object(startup_sync.import_manager, "update_stats") as stats,
            patch.object(startup_sync.import_manager.impact, "maybe_show_nudge"),
        ):
            op.success(([], None, True))

        tooltip.assert_not_called()
        stats.assert_called_once_with(silent=True)

    @pytest.mark.parametrize("state", ["deckBrowser", "overview"])
    def test_updates_applied_on_deck_browser_or_overview(self, mw_mock, state):
        op = self._run_local_startup(mw_mock, state=state)
        sub = [{"deck_hash": "h1", "deck": {"name": "Deck"}}]
        with (
            patch.object(
                startup_sync.QApplication, "activeModalWidget", return_value=MagicMock()
            ),
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
        ):
            op.success((sub, None, True))

        imp.assert_called_once_with((sub, None, True))

    def test_updates_deferred_with_tooltip_when_reviewing(self, mw_mock):
        op = self._run_local_startup(mw_mock, state="review")
        sub = [{"deck_hash": "h1", "deck": {"name": "Deck"}}]
        with (
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.import_manager, "create_backup") as backup,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op.success((sub, None, True))

        imp.assert_not_called()
        backup.assert_not_called()
        tooltip.assert_called_once()
        assert tooltip.call_args.kwargs["period"] == 5000

    def test_updates_deferred_when_modal_widget_open(self, mw_mock):
        op = self._run_local_startup(mw_mock, state="deckBrowser")
        sub = [{"deck_hash": "h1", "deck": {"name": "Deck"}}]
        modal = QWidget()
        with (
            patch.object(
                startup_sync.QApplication, "activeModalWidget", return_value=modal
            ),
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op.success((sub, None, True))

        imp.assert_not_called()
        tooltip.assert_called_once()

    def test_updates_deferred_when_ankiweb_sync_active(self, mw_mock):
        op = self._run_local_startup(mw_mock, state="overview")
        sub = [{"deck_hash": "h1", "deck": {"name": "Deck"}}]
        startup_sync._on_ankiweb_sync_will_start()
        with (
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op.success((sub, None, True))

        imp.assert_not_called()
        tooltip.assert_called_once()

    def test_on_failure_silent_when_offline_and_shows_tooltip_on_unexpected_error(
        self, mw_mock
    ):
        op = self._run_local_startup(mw_mock)
        with (
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
            patch.object(startup_sync.sentry_sdk, "capture_exception") as sentry,
        ):
            op.failure_cb(requests.exceptions.Timeout("timed out"))

        tooltip.assert_not_called()
        sentry.assert_not_called()
        assert startup_sync.is_pull_in_progress() is False

        op2 = self._run_local_startup(mw_mock, profile_name="Profile2")
        with (
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
            patch.object(startup_sync.sentry_sdk, "capture_exception") as sentry,
        ):
            op2.failure_cb(RuntimeError("unexpected"))

        tooltip.assert_called_once()
        sentry.assert_called_once()
        assert startup_sync.is_pull_in_progress() is False

    def test_aborted_case_b_allows_retry_when_profile_reopened_in_session(
        self, mw_mock
    ):
        _setup_ready_profile(
            mw_mock, profile_name="ProfileRetry", sync_auth="auth", auto_sync=True
        )
        fake_hooks = _make_gui_hooks()

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
        ):
            startup_sync.on_profile_opened()
            # Closed before sync_did_finish fired -> never dispatched
            startup_sync.on_profile_will_close()
            assert fake_hooks.sync_did_finish._hooks == []

            # Reopen same profile in same session -> arms hook and runs on sync_did_finish
            _setup_ready_profile(
                mw_mock, profile_name="ProfileRetry", sync_auth="auth", auto_sync=True
            )
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()
            single_shot.assert_called_once()

    def test_reopening_profile_without_close_replaces_pending_hook(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        with patch.object(startup_sync, "gui_hooks", fake_hooks):
            startup_sync.on_profile_opened()
            startup_sync.on_profile_opened()
        assert len(fake_hooks.sync_did_finish._hooks) == 1

    def test_profile_will_close_before_deferred_tick_aborts_startup_pull(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        with (
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
            patch.object(startup_sync, "QueryOp") as query_op,
        ):
            startup_sync.on_profile_opened()
            deferred = single_shot.call_args.args[1]
            startup_sync.on_profile_will_close()
            deferred()
        query_op.assert_not_called()

    def test_pull_on_startup_disabled_before_sync_finishes_skips_pull(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(startup_sync.QTimer, "singleShot") as single_shot,
        ):
            startup_sync.on_profile_opened()
            cfg = config_manager.get_stored_config()
            cfg["settings"]["pull_on_startup"] = False
            config_manager.write_config(cfg)
            fake_hooks.sync_did_finish()
        single_shot.assert_not_called()

    def test_point_version_below_231000_skips_without_collection(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=230900),
        ):
            startup_sync.on_profile_opened()
        assert _SyncQueryOp.last_instance is not None
        assert _SyncQueryOp.last_instance.without_collection_called is False

    @pytest.mark.parametrize(
        "status_code,text,expect_handle_failure",
        [
            (401, "unauthorized", False),
            (403, "forbidden", True),
            (200, "false", True),
        ],
    )
    def test_token_rejection_codes_during_startup_sync(
        self, mw_mock, status_code, text, expect_handle_failure
    ):
        op = self._run_local_startup(mw_mock)
        resp = MagicMock(status_code=status_code, text=text)
        with (
            patch.object(startup_sync.api_client, "post_empty", return_value=resp),
            patch.object(
                startup_sync.auth_manager, "handle_auth_failure"
            ) as handle_fail,
            patch.object(startup_sync.import_manager, "async_start_pull") as async_pull,
        ):
            res = op.op(mw_mock.col)
            op.success(res)

        assert res is startup_sync._SKIPPED
        async_pull.assert_not_called()
        if expect_handle_failure:
            handle_fail.assert_called_once()
        else:
            handle_fail.assert_not_called()


class TestConcurrencyAndHooksIntegration:
    def test_startup_sync_skips_if_pull_slot_already_held(self, mw_mock):
        assert startup_sync.acquire_pull_slot() is not None
        _setup_ready_profile(mw_mock, sync_auth=None)
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp") as query_op,
        ):
            startup_sync.on_profile_opened()

        query_op.assert_not_called()
        assert startup_sync.is_pull_in_progress() is True

    def test_owner_token_prevents_late_profile_a_from_releasing_profile_b_slot(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, profile_name="ProfileA", sync_auth=None)
        _SyncQueryOp.last_instance = None

        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()

        op_a = _SyncQueryOp.last_instance
        assert startup_sync.is_pull_in_progress() is True

        # ProfileA closes while its pull is still running; slot clears for ProfileB
        startup_sync.on_profile_will_close()
        assert startup_sync.is_pull_in_progress() is False

        # ProfileB starts its own startup pull and holds a new owner token
        _setup_ready_profile(mw_mock, profile_name="ProfileB", sync_auth=None)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()

        op_b = _SyncQueryOp.last_instance
        assert op_b is not None and op_b is not op_a
        assert startup_sync.is_pull_in_progress() is True

        # ProfileA's late _on_done and _on_failure MUST NOT release ProfileB's slot!
        op_a.success(([], None, True))
        assert startup_sync.is_pull_in_progress() is True
        op_a.failure_cb(RuntimeError("late"))
        assert startup_sync.is_pull_in_progress() is True

        # ProfileB's own completion releases its slot cleanly
        op_b.success(([], None, True))
        assert startup_sync.is_pull_in_progress() is False

    def test_case_b_without_can_auto_sync_attribute_proceeds_on_older_anki(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        del mw_mock.can_auto_sync
        fake_hooks = _make_gui_hooks()
        with patch.object(startup_sync, "gui_hooks", fake_hooks):
            startup_sync.on_profile_opened()
        assert len(fake_hooks.sync_did_finish._hooks) == 1

    def test_missing_or_non_callable_col_sync_status_fails_closed(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()

        op = _SyncQueryOp.last_instance
        with (
            patch.object(startup_sync.api_client, "post_empty") as post_empty,
            patch.object(startup_sync.import_manager, "async_start_pull") as pull,
        ):
            assert op.op(None) is startup_sync._SKIPPED
            assert op.op(SimpleNamespace()) is startup_sync._SKIPPED
            assert (
                op.op(SimpleNamespace(sync_status="not-callable"))
                is startup_sync._SKIPPED
            )
        post_empty.assert_not_called()
        pull.assert_not_called()

    def test_mid_flight_bg_op_aborts_and_suppresses_notify_after_close(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()

        op = _SyncQueryOp.last_instance

        # Close before _bg_op begins -> aborts before sync_status
        startup_sync.on_profile_will_close()
        assert op.op(mw_mock.col) is startup_sync._SKIPPED
        mw_mock.col.sync_status.assert_not_called()

        # Re-test close during sync_status (before /CheckUserToken)
        startup_sync.reset_state()
        _setup_ready_profile(
            mw_mock, profile_name="ProfileClose2", sync_auth="auth", auto_sync=True
        )
        _SyncQueryOp.last_instance = None
        fake_hooks2 = _make_gui_hooks()
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks2),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks2.sync_did_finish()

        op2 = _SyncQueryOp.last_instance

        def _sync_status_then_close(_auth):
            startup_sync.on_profile_will_close()
            return MagicMock(required=0)

        mw_mock.col.sync_status.side_effect = _sync_status_then_close
        with patch.object(startup_sync.api_client, "post_empty") as post_empty:
            assert op2.op(mw_mock.col) is startup_sync._SKIPPED
        post_empty.assert_not_called()

        # Re-test close during /pullChanges with session-guarded notify callback
        startup_sync.reset_state()
        mw_mock.col.sync_status.side_effect = None
        mw_mock.col.sync_status.return_value = MagicMock(required=0)
        _setup_ready_profile(mw_mock, profile_name="ProfileClose3", sync_auth=None)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
        op3 = _SyncQueryOp.last_instance

        def _pull_close_and_notify(_input_hash, silent=False, notify=None, **_kwargs):
            startup_sync.on_profile_will_close()
            assert callable(notify)
            notify("stale 5xx from closed profile")
            return (None, None, silent)

        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                side_effect=_pull_close_and_notify,
            ),
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op3.op(mw_mock.col)
        tooltip.assert_not_called()

    def test_launch_error_releases_pull_slot(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp") as query_op,
        ):
            op_inst = query_op.return_value
            op_inst.failure.return_value = op_inst
            op_inst.without_collection.return_value = op_inst
            op_inst.run_in_background.side_effect = RuntimeError("qt error")
            startup_sync.on_profile_opened()

        assert startup_sync.is_pull_in_progress() is False

    def test_hooks_delegation_and_non_blocking_manual_update(self, mw_mock):
        with patch.object(startup_sync, "on_profile_opened") as opened:
            hooks.autoUpdate()
        opened.assert_called_once_with()

        call_order = []
        with (
            patch.object(
                startup_sync,
                "on_profile_will_close",
                side_effect=lambda: call_order.append("startup_sync"),
            ),
            patch("main.media_manager") as mm,
        ):

            async def _close():
                call_order.append("media_manager")

            mm.close = _close
            hooks.onProfileWillClose()
        assert call_order == ["startup_sync", "media_manager"]

        with patch.object(startup_sync, "register_hooks") as reg:
            hooks.hooks_init()
        reg.assert_called_once_with()

        # Logged out: no network, tooltip only when not silent
        with (
            patch.object(hooks.auth_manager, "has_stored_token", return_value=False),
            patch.object(hooks, "handle_pull") as hp,
            patch.object(hooks.aqt.utils, "tooltip") as tooltip,
        ):
            hooks.async_update(False)
            tooltip.assert_called_once()
            hooks.async_update(True)
            tooltip.assert_called_once()
        hp.assert_not_called()

        # Logged in: delegates to handle_pull with validate_token=True, zero main-thread CheckUserToken
        with (
            patch.object(hooks.auth_manager, "has_stored_token", return_value=True),
            patch.object(hooks, "handle_pull") as hp,
            patch("api_client.api_client.post_empty") as post_empty,
        ):
            hooks.async_update(False)
        post_empty.assert_not_called()
        hp.assert_called_once_with(None, False, validate_token=True)


def _model_dict(
    uuid: str = "u1",
    name: str = "Basic",
    fields: tuple[str, ...] = ("Front", "Back"),
    tmpls: tuple[tuple[str, str], ...] = (("Card 1", "{{Front}}"),),
    css: str = ".card {}",
) -> dict:
    return {
        "crowdanki_uuid": uuid,
        "name": name,
        "flds": [{"name": f} for f in fields],
        "tmpls": [{"name": t_name, "qfmt": qfmt, "afmt": ""} for t_name, qfmt in tmpls],
        "css": css,
    }


def _sub_payload(
    models: list[dict], *, children: list[dict] | None = None
) -> list[dict]:
    return [
        {
            "deck_hash": "h1",
            "deck": {
                "name": "Root",
                "note_models": models,
                "children": children or [],
            },
        }
    ]


class TestSchemaBumpFullSyncGate:
    def test_updates_require_full_sync_returns_false_for_safe_changes(self, mw_mock):
        local = _model_dict(
            fields=("Front", "Back", "LocalOnly"),
            tmpls=(("Card 1", "{{Front}}"), ("Card 2", "{{Back}}")),
        )
        # Bare MagicMock col.models.all() must not false-positive
        assert (
            startup_sync._updates_require_full_sync(
                _sub_payload([_model_dict(fields=("Front", "Back", "New"))]),
                mw_mock.col,
            )
            is False
        )

        mw_mock.col.models.all.return_value = [local]
        safe_cases = [
            # CSS-only & template-HTML-only changes
            _sub_payload(
                [
                    _model_dict(
                        fields=("Front", "Back"),
                        tmpls=(("Card 1", "{{Front}}!"), ("Card 2", "{{Back}}!")),
                        css=".card { color: red; }",
                    )
                ]
            ),
            # Brand-new notetype
            _sub_payload([_model_dict(uuid="brand-new", fields=("A", "B", "C"))]),
            # ProjektAnki templates preserved on update
            _sub_payload(
                [
                    _model_dict(
                        uuid="u-proj",
                        name="ProjektAnki Basic",
                        fields=("Front", "Back"),
                        tmpls=(("Card 1", ""), ("Card 2", ""), ("Card 3", "")),
                    )
                ]
            ),
        ]
        mw_mock.col.models.all.return_value = [
            local,
            _model_dict(uuid="u-proj", name="ProjektAnki Basic"),
        ]
        for webresult in safe_cases:
            assert (
                startup_sync._updates_require_full_sync(webresult, mw_mock.col) is False
            )

    @pytest.mark.parametrize(
        "remote_models,children",
        [
            # Field added
            ([_model_dict(fields=("Front", "Back", "Hint"))], None),
            # Field reordered (with local-only trailing field present)
            ([_model_dict(fields=("Back", "Front"))], None),
            # Template added
            (
                [
                    _model_dict(
                        fields=("Front", "Back"),
                        tmpls=(
                            ("Card 1", "{{Front}}"),
                            ("Card 2", "{{Back}}"),
                            ("Card 3", "{{Front}}"),
                        ),
                    )
                ],
                None,
            ),
            # Template removed
            (
                [
                    _model_dict(
                        fields=("Front", "Back"),
                        tmpls=(("Card 1", "{{Front}}"),),
                    )
                ],
                None,
            ),
            # Template reordered
            (
                [
                    _model_dict(
                        fields=("Front", "Back"),
                        tmpls=(("Card 2", "{{Back}}"), ("Card 1", "{{Front}}")),
                    )
                ],
                None,
            ),
            # Nested child-deck note_models
            (
                [],
                [
                    {
                        "name": "Sub",
                        "note_models": [],
                        "children": [
                            {
                                "name": "Leaf",
                                "note_models": [
                                    _model_dict(fields=("Front", "Back", "Extra"))
                                ],
                            }
                        ],
                    }
                ],
            ),
        ],
    )
    def test_updates_require_full_sync_returns_true_for_schema_bumps(
        self, mw_mock, remote_models, children
    ):
        mw_mock.col.models.all.return_value = [
            _model_dict(
                fields=("Front", "Back", "LocalOnly"),
                tmpls=(("Card 1", "{{Front}}"), ("Card 2", "{{Back}}")),
            )
        ]
        webresult = _sub_payload(remote_models, children=children)
        assert startup_sync._updates_require_full_sync(webresult, mw_mock.col) is True

    def test_silent_startup_defers_schema_bump_for_ankiweb_and_allows_local(
        self, mw_mock
    ):
        mw_mock.col.models.all.return_value = [_model_dict(fields=("Front", "Back"))]
        webresult = _sub_payload([_model_dict(fields=("Front", "Back", "Hint"))])

        # AnkiWeb configured -> deferred with tooltip, no backup/import
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()
        op = _SyncQueryOp.last_instance

        with (
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.import_manager, "create_backup") as backup,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op.success((webresult, None, True))

        imp.assert_not_called()
        backup.assert_not_called()
        tooltip.assert_called_once()
        assert tooltip.call_args.kwargs["period"] == 6000

        # Local-only user -> proceeds normally
        startup_sync.reset_state()
        _setup_ready_profile(
            mw_mock, profile_name="LocalOnly", sync_auth=None, auto_sync=False
        )
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
        op_local = _SyncQueryOp.last_instance

        with (
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op_local.success((webresult, None, True))

        imp.assert_called_once_with((webresult, None, True))
        tooltip.assert_not_called()


class TestCollectionReadinessAndEndpointAndMediaSync:
    def test_col_db_none_blocks_readiness_and_guarded_paths(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth=None)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
        op = _SyncQueryOp.last_instance

        mw_mock.col.db = None
        assert startup_sync._is_collection_ready() is False
        assert startup_sync.can_apply_startup_updates_now() is False

        with (
            patch.object(startup_sync.import_manager, "import_webresult") as imp,
            patch.object(startup_sync.aqt.utils, "tooltip") as tooltip,
        ):
            op.success(([{"deck_hash": "h", "deck": {}}], None, True))
            op.failure_cb(RuntimeError("late"))
            startup_sync._show_guarded_tooltip("hi")

        imp.assert_not_called()
        tooltip.assert_not_called()

        with patch.object(startup_sync.QTimer, "singleShot") as single_shot:
            startup_sync.on_profile_opened()
        single_shot.assert_not_called()

    def test_temporary_close_hooks_toggle_readiness_and_reset_on_profile_close(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, sync_auth=None)
        fake_hooks = _make_gui_hooks()
        with patch.object(startup_sync, "gui_hooks", fake_hooks):
            startup_sync.register_hooks()

        assert startup_sync._is_collection_ready() is True
        fake_hooks.collection_will_temporarily_close("arg")
        assert startup_sync._is_collection_ready() is False
        assert startup_sync.can_apply_startup_updates_now() is False

        fake_hooks.collection_did_temporarily_close("arg")
        assert startup_sync._is_collection_ready() is True
        assert startup_sync.can_apply_startup_updates_now() is True

        fake_hooks.collection_will_temporarily_close()
        assert startup_sync._is_collection_ready() is False
        startup_sync.on_profile_will_close()
        assert startup_sync._is_collection_ready() is True

    def test_sync_status_new_endpoint_propagated_on_main_thread_only(self, mw_mock):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None
        queued_main_callbacks = []
        mw_mock.taskman.run_on_main.side_effect = queued_main_callbacks.append

        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()

        op = _SyncQueryOp.last_instance
        mw_mock.col.sync_status.return_value = SimpleNamespace(
            required=0, new_endpoint="https://sync2.ankiweb.net/"
        )
        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ),
        ):
            op.op(mw_mock.col)

        # Must not call mw.pm directly on background thread
        mw_mock.pm.set_current_sync_url.assert_not_called()
        assert len(queued_main_callbacks) == 1
        queued_main_callbacks[0]()
        mw_mock.pm.set_current_sync_url.assert_called_once_with(
            "https://sync2.ankiweb.net/"
        )

        # If session closes before queued callback executes, ignore it
        mw_mock.pm.set_current_sync_url.reset_mock()
        queued_main_callbacks.clear()
        startup_sync.reset_state()
        _setup_ready_profile(
            mw_mock, profile_name="EpClose", sync_auth="auth", auto_sync=True
        )
        _SyncQueryOp.last_instance = None
        fake_hooks2 = _make_gui_hooks()
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks2),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks2.sync_did_finish()

        op2 = _SyncQueryOp.last_instance
        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ),
        ):
            op2.op(mw_mock.col)
        startup_sync.on_profile_will_close()
        queued_main_callbacks[0]()
        mw_mock.pm.set_current_sync_url.assert_not_called()

    @pytest.mark.parametrize("bad_endpoint", [None, "", MagicMock()])
    def test_non_string_or_empty_new_endpoint_ignored(self, mw_mock, bad_endpoint):
        _setup_ready_profile(mw_mock, sync_auth="auth", auto_sync=True)
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None
        with (
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
            fake_hooks.sync_did_finish()

        op = _SyncQueryOp.last_instance
        mw_mock.col.sync_status.return_value = SimpleNamespace(
            required=0, new_endpoint=bad_endpoint
        )
        with (
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ),
        ):
            op.op(mw_mock.col)

        mw_mock.pm.set_current_sync_url.assert_not_called()

    @pytest.mark.parametrize("media_syncing", [True, False])
    def test_start_startup_pull_passes_media_syncing_to_async_start_pull(
        self, mw_mock, media_syncing
    ):
        _setup_ready_profile(mw_mock, sync_auth=None)
        mw_mock.media_syncer.is_syncing.return_value = media_syncing
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
            patch.object(
                startup_sync.api_client,
                "post_empty",
                return_value=MagicMock(status_code=200, text="true"),
            ),
            patch.object(
                startup_sync.import_manager,
                "async_start_pull",
                return_value=([], None, True),
            ) as pull,
        ):
            startup_sync.on_profile_opened()
            _SyncQueryOp.last_instance.op(mw_mock.col)

        assert pull.call_args.kwargs["defer_cache_bootstrap"] is media_syncing

    def test_on_done_holds_pull_slot_during_import_and_skips_empty_when_syncing(
        self, mw_mock
    ):
        _setup_ready_profile(mw_mock, sync_auth=None)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
        op = _SyncQueryOp.last_instance

        slot_states_during_import = []

        def _spy_import(_data):
            slot_states_during_import.append(startup_sync.is_pull_in_progress())

        with (
            patch.object(
                startup_sync.QApplication, "activeModalWidget", return_value=None
            ),
            patch.object(
                startup_sync.import_manager, "import_webresult", side_effect=_spy_import
            ),
        ):
            op.success(([{"deck_hash": "h1", "deck": {"name": "D"}}], None, True))

        assert slot_states_during_import == [True]
        assert startup_sync.is_pull_in_progress() is False

        # Empty webresult while AnkiWeb sync is active must NOT trigger import_webresult/update_stats
        startup_sync.reset_state()
        _setup_ready_profile(mw_mock, profile_name="SyncActiveEmpty", sync_auth=None)
        _SyncQueryOp.last_instance = None
        with (
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: fn(),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.on_profile_opened()
        op2 = _SyncQueryOp.last_instance
        startup_sync._on_ankiweb_sync_will_start()
        with patch.object(startup_sync.import_manager, "import_webresult") as imp:
            op2.success(([], None, True))
        imp.assert_not_called()
        assert startup_sync.is_pull_in_progress() is False

    def test_updates_require_full_sync_detects_note_model_migration_and_type_change(
        self, mw_mock
    ):
        local = _model_dict(uuid="u1", name="Basic", fields=("Front", "Back"))
        local["id"] = 100
        local["type"] = 0
        mw_mock.col.models.all.return_value = [local]

        # Notetype type change (standard 0 -> cloze 1) requires full sync
        remote_cloze = _model_dict(uuid="u1", name="Basic", fields=("Front", "Back"))
        remote_cloze["type"] = 1
        assert (
            startup_sync._updates_require_full_sync(
                _sub_payload([remote_cloze]), mw_mock.col
            )
            is True
        )

        # Existing note migrating to a different note_model_uuid requires full sync
        mw_mock.col.db.all.return_value = [("note-guid-1", 100)]
        webresult = [
            {
                "deck_hash": "h1",
                "deck": {
                    "name": "Root",
                    "note_models": [
                        _model_dict(uuid="u-new", fields=("Front", "Back"))
                    ],
                    "notes": [{"guid": "note-guid-1", "note_model_uuid": "u-new"}],
                    "children": [],
                },
            }
        ]
        assert startup_sync._updates_require_full_sync(webresult, mw_mock.col) is True

        # Existing note keeping the same note_model_uuid does NOT require full sync
        webresult_same = [
            {
                "deck_hash": "h1",
                "deck": {
                    "name": "Root",
                    "note_models": [_model_dict(uuid="u1", fields=("Front", "Back"))],
                    "notes": [{"guid": "note-guid-1", "note_model_uuid": "u1"}],
                    "children": [],
                },
            }
        ]
        assert (
            startup_sync._updates_require_full_sync(webresult_same, mw_mock.col)
            is False
        )


class _FakeSignal:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, fn) -> None:
        self._slots.append(fn)

    def emit(self, *args) -> None:
        for fn in list(self._slots):
            fn(*args)


class _FakeAddonHarness:
    """Simulates Anki's ``aqt.addons`` and ``mw.check_for_addon_updates`` flow."""

    def __init__(self, mw_mock) -> None:
        self.mw_mock = mw_mock
        self.top_level_widgets: list = []
        self.last_dialog = None
        self.pending_on_updates_done = None
        self.pending_download_finish = None
        self.orig_on_done_calls: list = []
        self.on_updates_installed_calls: list = []
        self.addon_update_enabled: dict[str, bool] = {}

        harness = self

        class InstallOk:
            pass

        class DownloadError:
            pass

        class ChooseAddonsToUpdateDialog:
            def __init__(self, parent, mgr, updated_addons) -> None:
                self.parent = parent
                self.mgr = mgr
                self.updated_addons = updated_addons
                self.finished = _FakeSignal()
                self.destroyed = _FakeSignal()
                self._visible = False
                self._on_done = None
                harness.last_dialog = self

            def ask(self, on_done) -> None:
                self._on_done = on_done
                self._visible = True
                if self not in harness.top_level_widgets:
                    harness.top_level_widgets.append(self)

            def isVisible(self) -> bool:
                return self._visible

            def accept_with_ids(self, ids: list[int]) -> None:
                if self._on_done is not None:
                    self._on_done(ids)
                self._visible = False
                self.finished.emit(1)

            def reject(self) -> None:
                self._visible = False
                self.finished.emit(0)

        def prompt_to_update(
            parent,
            mgr,
            updated_addons,
            on_done,
            requested_by_user: bool = True,
        ) -> None:
            if not requested_by_user:
                if not any(
                    harness.addon_update_enabled.get(str(a.id), True)
                    for a in updated_addons
                ):
                    return

            def after_choosing(ids: list[int]) -> None:
                if ids:
                    harness.pending_download_finish = lambda log: on_done(log)

            ChooseAddonsToUpdateDialog(parent, mgr, updated_addons).ask(after_choosing)

        def check_for_addon_updates(by_user: bool, on_done=None) -> None:
            def wrap_on_updates_installed(log) -> None:
                if log:
                    harness.on_updates_installed_calls.append(log)
                if on_done is not None:
                    on_done(log)

            harness.pending_on_updates_done = wrap_on_updates_installed

        self.InstallOk = InstallOk
        self.DownloadError = DownloadError
        self.ChooseAddonsToUpdateDialog = ChooseAddonsToUpdateDialog
        self.prompt_to_update = prompt_to_update
        self.check_for_addon_updates = check_for_addon_updates
        self.addons_module = SimpleNamespace(
            InstallOk=InstallOk,
            DownloadError=DownloadError,
            ChooseAddonsToUpdateDialog=ChooseAddonsToUpdateDialog,
            prompt_to_update=prompt_to_update,
        )
        mw_mock.check_for_addon_updates = check_for_addon_updates


class TestAddonUpdateDialogCoordination:
    def _run_with_harness(
        self, mw_mock, *, profile_name="TestProfile", sync_auth=None, auto_sync=False
    ):
        harness = _FakeAddonHarness(mw_mock)
        _setup_ready_profile(
            mw_mock,
            profile_name=profile_name,
            sync_auth=sync_auth,
            auto_sync=auto_sync,
        )
        fake_hooks = _make_gui_hooks()
        _SyncQueryOp.last_instance = None
        return harness, fake_hooks

    def test_no_addon_updates_waits_for_check_then_runs_startup_pull(self, mw_mock):
        for sync_auth, auto_sync in [(None, False), ("auth", True)]:
            startup_sync.reset_state()
            harness, fake_hooks = self._run_with_harness(
                mw_mock, sync_auth=sync_auth, auto_sync=auto_sync
            )
            scheduled: list = []

            with (
                patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
                patch.object(startup_sync, "gui_hooks", fake_hooks),
                patch.object(
                    startup_sync.QTimer,
                    "singleShot",
                    side_effect=lambda _ms, fn: scheduled.append(fn),
                ),
                patch.object(startup_sync, "QueryOp", _SyncQueryOp),
                patch.object(startup_sync, "point_version", return_value=240000),
            ):
                startup_sync.register_hooks()
                startup_sync.on_profile_opened()
                if auto_sync:
                    fake_hooks.sync_did_finish()

                # Simulate Anki starting its background add-on update check right after
                mw_mock.check_for_addon_updates(
                    by_user=False,
                    on_done=lambda log: harness.orig_on_done_calls.append(log),
                )

                # Event loop fires the startup pull tick while add-on check is in flight
                while scheduled:
                    scheduled.pop(0)()
                assert _SyncQueryOp.last_instance is None
                assert startup_sync.can_apply_startup_updates_now() is False

                # Anki finishes add-on check with no updates -> on_done([]) unblocks pull
                harness.pending_on_updates_done([])
                assert harness.orig_on_done_calls == [[]]
                assert len(scheduled) == 1
                scheduled.pop(0)()

            assert _SyncQueryOp.last_instance is not None
            assert _SyncQueryOp.last_instance.run_called is True

    def test_updates_all_disabled_prompt_returns_early_and_unblocks_pull(self, mw_mock):
        harness, fake_hooks = self._run_with_harness(mw_mock)
        scheduled: list = []
        harness.addon_update_enabled["999"] = False

        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(by_user=False, on_done=lambda _log: None)

            while scheduled:
                scheduled.pop(0)()
            assert _SyncQueryOp.last_instance is None

            # Anki calls prompt_to_update(requested_by_user=False), which returns early
            # without creating a dialog and without invoking on_done.
            harness.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=999)],
                harness.pending_on_updates_done,
                requested_by_user=False,
            )
            assert harness.last_dialog is None
            assert len(scheduled) == 1
            scheduled.pop(0)()

        assert _SyncQueryOp.last_instance is not None
        assert _SyncQueryOp.last_instance.run_called is True

    def test_dialog_shown_and_cancelled_defers_until_reject(self, mw_mock):
        harness, fake_hooks = self._run_with_harness(mw_mock)
        scheduled: list = []

        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(by_user=False, on_done=lambda _log: None)
            harness.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=999)],
                harness.pending_on_updates_done,
                requested_by_user=False,
            )

            assert harness.last_dialog is not None
            while scheduled:
                scheduled.pop(0)()
            assert _SyncQueryOp.last_instance is None
            assert startup_sync.can_apply_startup_updates_now() is False

            # User clicks Cancel -> dialog closes -> deferred startup pull starts
            harness.last_dialog.reject()
            assert len(scheduled) == 1
            scheduled.pop(0)()

        assert _SyncQueryOp.last_instance is not None
        assert _SyncQueryOp.last_instance.run_called is True

    def test_dialog_accepted_with_empty_selection_unblocks_pull(self, mw_mock):
        harness, fake_hooks = self._run_with_harness(mw_mock)
        scheduled: list = []

        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(by_user=False, on_done=lambda _log: None)
            harness.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=999)],
                harness.pending_on_updates_done,
                requested_by_user=False,
            )
            while scheduled:
                scheduled.pop(0)()
            assert _SyncQueryOp.last_instance is None

            harness.last_dialog.accept_with_ids([])
            assert harness.pending_download_finish is None
            assert len(scheduled) == 1
            scheduled.pop(0)()

        assert _SyncQueryOp.last_instance is not None
        assert _SyncQueryOp.last_instance.run_called is True

    def test_installing_other_addon_keeps_pull_deferred_until_download_finishes(
        self, mw_mock
    ):
        harness, fake_hooks = self._run_with_harness(mw_mock)
        scheduled: list = []

        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(
                by_user=False,
                on_done=lambda log: harness.orig_on_done_calls.append(log),
            )
            harness.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=999)],
                harness.pending_on_updates_done,
                requested_by_user=False,
            )
            while scheduled:
                scheduled.pop(0)()
            assert _SyncQueryOp.last_instance is None

            # User clicks OK with another add-on selected -> dialog closes, download starts
            harness.last_dialog.accept_with_ids([999])
            assert callable(harness.pending_download_finish)
            assert scheduled == []
            assert _SyncQueryOp.last_instance is None
            assert startup_sync.can_apply_startup_updates_now() is False

            # Download finishes and user dismisses restart prompt -> pull now starts
            log = [(999, harness.InstallOk())]
            harness.pending_download_finish(log)
            assert harness.orig_on_done_calls == [log]
            assert len(scheduled) == 1
            scheduled.pop(0)()

        assert _SyncQueryOp.last_instance is not None
        assert _SyncQueryOp.last_instance.run_called is True

    def test_installing_self_addon_skips_pull_unless_download_failed(self, mw_mock):
        self_id = startup_sync._current_addon_id()
        assert self_id is not None

        # InstallOk for AnkiCollab itself -> skips startup pull
        harness, fake_hooks = self._run_with_harness(mw_mock)
        scheduled: list = []
        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(by_user=False, on_done=lambda _log: None)
            harness.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=self_id)],
                harness.pending_on_updates_done,
                requested_by_user=False,
            )
            while scheduled:
                scheduled.pop(0)()
            harness.last_dialog.accept_with_ids([self_id])
            harness.pending_download_finish([(self_id, harness.InstallOk())])

        assert scheduled == []
        assert _SyncQueryOp.last_instance is None

        # DownloadError for AnkiCollab itself -> still runs startup pull
        startup_sync.reset_state()
        harness2, fake_hooks2 = self._run_with_harness(mw_mock, profile_name="RetryErr")
        scheduled2: list = []
        with (
            patch.dict(sys.modules, {"aqt.addons": harness2.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks2),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled2.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(by_user=False, on_done=lambda _log: None)
            harness2.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=self_id)],
                harness2.pending_on_updates_done,
                requested_by_user=False,
            )
            while scheduled2:
                scheduled2.pop(0)()
            harness2.last_dialog.accept_with_ids([self_id])
            harness2.pending_download_finish([(self_id, harness2.DownloadError())])
            assert len(scheduled2) == 1
            scheduled2.pop(0)()

        assert _SyncQueryOp.last_instance is not None
        assert _SyncQueryOp.last_instance.run_called is True

    def test_profile_close_discards_pending_post_addon_pull(self, mw_mock):
        harness, fake_hooks = self._run_with_harness(mw_mock)
        scheduled: list = []

        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
            patch.object(
                startup_sync.QTimer,
                "singleShot",
                side_effect=lambda _ms, fn: scheduled.append(fn),
            ),
            patch.object(startup_sync, "QueryOp", _SyncQueryOp),
            patch.object(startup_sync, "point_version", return_value=240000),
        ):
            startup_sync.register_hooks()
            startup_sync.on_profile_opened()
            mw_mock.check_for_addon_updates(by_user=False, on_done=lambda _log: None)
            harness.addons_module.prompt_to_update(
                mw_mock,
                mw_mock.addonManager,
                [SimpleNamespace(id=999)],
                harness.pending_on_updates_done,
                requested_by_user=False,
            )
            while scheduled:
                scheduled.pop(0)()
            assert startup_sync._pending_post_addon_update_pull is not None

            startup_sync.on_profile_will_close()
            assert startup_sync._pending_post_addon_update_pull is None

            harness.last_dialog.reject()

        assert scheduled == []
        assert _SyncQueryOp.last_instance is None

    def test_reload_safe_hooks_and_check_exception_cleanup(self, mw_mock):
        harness, fake_hooks = self._run_with_harness(mw_mock)
        orig_check = mw_mock.check_for_addon_updates
        orig_prompt = harness.addons_module.prompt_to_update
        orig_ask = harness.ChooseAddonsToUpdateDialog.ask

        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(startup_sync, "gui_hooks", fake_hooks),
        ):
            startup_sync.register_hooks()
            wrapped_check = mw_mock.check_for_addon_updates
            wrapped_prompt = harness.addons_module.prompt_to_update
            wrapped_ask = harness.ChooseAddonsToUpdateDialog.ask
            assert wrapped_check is not orig_check
            assert wrapped_prompt is not orig_prompt
            assert wrapped_ask is not orig_ask

            # Second call does not stack wrappers
            startup_sync.register_hooks()
            assert mw_mock.check_for_addon_updates is wrapped_check
            assert harness.addons_module.prompt_to_update is wrapped_prompt
            assert harness.ChooseAddonsToUpdateDialog.ask is wrapped_ask

            # Synchronous failure in check_for_addon_updates cleans up blocking state
            def _boom(*_args, **_kwargs):
                raise RuntimeError("boom")

            startup_sync.reset_state()
            mw_mock.check_for_addon_updates = _boom
            startup_sync.register_hooks()
            with pytest.raises(RuntimeError, match="boom"):
                mw_mock.check_for_addon_updates(by_user=False)
            assert startup_sync._is_addon_update_blocking() is False

            startup_sync.reset_state()
            assert mw_mock.check_for_addon_updates is _boom
            assert harness.addons_module.prompt_to_update is orig_prompt
            assert harness.ChooseAddonsToUpdateDialog.ask is orig_ask

    def test_can_apply_startup_updates_now_guards_visible_dialog_and_progress_busy(
        self, mw_mock
    ):
        harness, _fake_hooks = self._run_with_harness(mw_mock)
        with (
            patch.dict(sys.modules, {"aqt.addons": harness.addons_module}),
            patch.object(
                startup_sync.QApplication, "activeModalWidget", return_value=None
            ),
            patch.object(
                startup_sync.QApplication,
                "topLevelWidgets",
                return_value=harness.top_level_widgets,
            ),
        ):
            # Default MagicMock progress.busy() does NOT false-positive
            assert startup_sync.can_apply_startup_updates_now() is True

            mw_mock.progress.busy.return_value = 0
            assert startup_sync.can_apply_startup_updates_now() is True

            mw_mock.progress.busy.return_value = 1
            assert startup_sync.can_apply_startup_updates_now() is False
            mw_mock.progress.busy.return_value = 0

            # Visible ChooseAddonsToUpdateDialog blocks even without startup wrapper
            dlg = harness.ChooseAddonsToUpdateDialog(
                mw_mock, mw_mock.addonManager, [SimpleNamespace(id=999)]
            )
            dlg._visible = True
            harness.top_level_widgets.append(dlg)
            assert startup_sync.can_apply_startup_updates_now() is False

            dlg._visible = False
            assert startup_sync.can_apply_startup_updates_now() is True
