"""Tests for menu.py — deck-hash validation and default-config seeding.

Only functions that do not touch Qt rendering are tested here.  These are the
entry-point guards that run for every user: a deck hash that is accepted or
rejected wrongly, or defaults that overwrite a user's settings, would be a
user-visible regression with no UI to catch it.
"""

import re
from unittest.mock import MagicMock

import pytest

import config_manager
import consent
from menu import (
    add_to_table,
    delete_subscription,
    on_login_manager_btn,
    store_default_config,
    update_ui_for_login_state,
    validate_deck_hash,
)
from tests.mocks import spy_config_writes

_ALL_DEFAULTS = {
    "pull_on_startup": True,
    "suspend_new_cards": False,
    "auto_move_cards": False,
    "keep_empty_subdecks": False,
    "rated_addon": False,
    "last_ratepls": None,  # time-dependent — checked separately
    "pull_counter": 0,
    "push_counter": 0,
    "remember_suggest_state_between_sessions": False,
    "suggest_on_ankicollab_last_state": False,
    "error_reporting_enabled": False,
    "impact_nudge_enabled": True,
    "last_impact_nudge": "",
}


class TestValidateDeckHash:
    """validate_deck_hash(): 3/5/6 alphabetic words are valid; anything else
    is rejected.  This function gates a potentially-malicious download, so
    rejections matter as much as acceptances."""

    @pytest.mark.parametrize(
        "deck_hash",
        [
            "alpha-beta-gamma",
            "five-word-hash-is-ok",
            "six-words-here-are-ok-too",
            "leech-marked-missing",
            "Aa-Bb-Cc",  # mixed case allowed (legacy)
        ],
    )
    def test_valid_hashes_accepted(self, deck_hash):
        assert validate_deck_hash(deck_hash) is True

    @pytest.mark.parametrize(
        "deck_hash",
        [
            "a-b",  # too few
            "a-b-c-d",  # 4 words — not a valid format
            "a-b-c-d-e-f-g",  # too many
            "alpha-123-gamma",  # non-alphabetic part
            "123-456-789",  # digits only
            "alpha- -gamma",  # empty/whitespace part
            "",  # empty string
            "   ",  # whitespace only
        ],
    )
    def test_invalid_hashes_rejected(self, deck_hash):
        assert validate_deck_hash(deck_hash) is False

    def test_none_rejected(self):
        assert validate_deck_hash(None) is False


class TestStoreDefaultConfig:
    """store_default_config() must seed missing defaults without ever
    overwriting a value the user (or another module) has set, and must not
    write at all when nothing changed."""

    def _seed(self, stored):
        """Overwrite the active profile's stored config."""
        config_manager.write_config(stored)

    def test_none_config_writes_full_defaults(self, mw_mock, monkeypatch):
        self._seed({})
        writes = spy_config_writes(monkeypatch)

        store_default_config()

        assert len(writes) == 1
        written = writes[0]
        settings = written["settings"]
        # only `settings` is ensured; auth now lives in auth.json
        assert "settings" in written
        assert "auth" not in written
        for key, value in _ALL_DEFAULTS.items():
            assert key in settings, f"default key {key} missing"
            if key != "last_ratepls":
                assert settings[key] == value
        # last_ratepls is a current UTC timestamp in the addon's format
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", settings["last_ratepls"]
        )

    def test_partial_config_only_fills_missing(self, mw_mock, monkeypatch):
        existing = {"settings": {"pull_on_startup": True, "rated_addon": True}}
        self._seed(existing)
        writes = spy_config_writes(monkeypatch)

        store_default_config()

        assert len(writes) == 1
        settings = writes[0]["settings"]
        # Existing user values must never be overwritten
        assert settings["pull_on_startup"] is True
        assert settings["rated_addon"] is True
        # Missing keys are filled with defaults
        assert settings["error_reporting_enabled"] is False
        assert settings["push_counter"] == 0
        assert settings["suspend_new_cards"] is False

    def test_complete_config_does_not_write(self, mw_mock, monkeypatch):
        complete_settings = dict(_ALL_DEFAULTS)
        complete_settings["last_ratepls"] = "2025-01-01 00:00:00"
        self._seed({"settings": complete_settings})
        writes = spy_config_writes(monkeypatch)

        store_default_config()

        # The `updated` short-circuit must hold: nothing changed → no write
        assert writes == []

    def test_store_default_config_is_a_no_op_without_a_profile(self, tmp_path, mw_mock):
        config_manager.reset_for_tests(tmp_path / "user_files")

        store_default_config()  # must not raise

        assert config_manager.current_profile_name() is None

    def test_store_default_config_writes_the_defaults_for_an_open_profile(
        self, tmp_path, mw_mock
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("Medicine")

        store_default_config()

        stored = config_manager.get_stored_config()
        assert stored["settings"]["pull_on_startup"] is True
        assert "auth" not in stored

    def test_existing_profile_with_pull_on_startup_false_stays_false(self, mw_mock):
        self._seed({"settings": {"pull_on_startup": False}})

        store_default_config()

        assert (
            config_manager.get_stored_config()["settings"]["pull_on_startup"] is False
        )

    def test_existing_profile_with_pull_on_startup_true_stays_true(self, mw_mock):
        self._seed({"settings": {"pull_on_startup": True}})

        store_default_config()

        assert config_manager.get_stored_config()["settings"]["pull_on_startup"] is True

    def test_existing_profile_with_partial_settings_seeds_pull_on_startup_false(
        self, mw_mock
    ):
        self._seed({"settings": {"rated_addon": True}})

        store_default_config()

        settings = config_manager.get_stored_config()["settings"]
        assert settings["pull_on_startup"] is False
        assert settings["rated_addon"] is True

    def test_existing_profile_with_subscriptions_only_seeds_pull_on_startup_false(
        self, mw_mock
    ):
        self._seed({"alpha-beta-gamma": {"deckId": 1}})

        store_default_config()

        stored = config_manager.get_stored_config()
        assert stored["settings"]["pull_on_startup"] is False
        assert stored["alpha-beta-gamma"] == {"deckId": 1}

    def test_existing_profile_with_empty_config_and_auth_seeds_pull_on_startup_false(
        self, mw_mock
    ):
        self._seed({})
        config_manager.write_auth({"token": "t"})

        store_default_config()

        assert (
            config_manager.get_stored_config()["settings"]["pull_on_startup"] is False
        )

    def test_brand_new_profile_with_empty_config_and_auth_seeds_pull_on_startup_true(
        self, tmp_path, mw_mock
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("BrandNewProfile")

        store_default_config()

        stored = config_manager.get_stored_config()
        assert config_manager.get_auth() == {}
        assert stored["settings"]["pull_on_startup"] is True

    def test_non_dict_settings_is_repaired_preserving_existing_profile_status(
        self, mw_mock
    ):
        self._seed({"settings": "corrupt"})

        store_default_config()

        settings = config_manager.get_stored_config()["settings"]
        assert isinstance(settings, dict)
        assert settings["pull_on_startup"] is False

    def test_store_default_config_ignores_shipped_defaults_and_does_not_bake_them(
        self, tmp_path, mw_mock, monkeypatch
    ):
        config_manager.reset_for_tests(tmp_path / "user_files")
        config_manager.activate_for_tests("FreshWithShippedDefaults")
        monkeypatch.setattr(
            config_manager,
            "_defaults_cache",
            {
                "settings": {"pull_on_startup": False, "shipped_setting": "ignored"},
                "shipped_top_level": True,
            },
        )

        store_default_config()

        stored = config_manager.get_stored_config()
        assert stored["settings"]["pull_on_startup"] is True
        assert "shipped_setting" not in stored["settings"]
        assert "shipped_top_level" not in stored

        # Subsequent call with complete stored settings performs zero writes
        writes = spy_config_writes(monkeypatch)
        store_default_config()
        assert writes == []


class TestAddToTable:
    def test_blocks_new_subscription_when_startup_pull_is_in_progress(
        self, mw_mock, monkeypatch
    ):
        config_manager.write_config({"settings": {}})
        line_edit = MagicMock()
        line_edit.text.return_value = "alpha-beta-gamma"
        table = MagicMock()
        dialog = MagicMock()
        info_calls = []

        monkeypatch.setattr("menu.askUser", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            "menu.showInfo",
            lambda msg, **kwargs: info_calls.append((msg, kwargs)),
        )
        subscribe_mock = MagicMock()
        handle_pull_mock = MagicMock()
        monkeypatch.setattr("menu.subscribe_to_deck", subscribe_mock)
        monkeypatch.setattr("menu.handle_pull", handle_pull_mock)
        monkeypatch.setattr("menu.startup_sync.is_pull_in_progress", lambda: True)

        add_to_table(line_edit, table, dialog)

        assert "alpha-beta-gamma" not in config_manager.get_stored_config()
        subscribe_mock.assert_not_called()
        handle_pull_mock.assert_not_called()
        assert len(info_calls) == 1
        assert "checking for updates" in info_calls[0][0].lower()
        assert info_calls[0][1]["parent"] is dialog


class TestDeleteSubscription:
    def test_cancelled_confirmation_keeps_subscription(self, monkeypatch):
        config_manager.write_config({"alpha-beta-gamma": {"deckId": 1}})
        monkeypatch.setattr("menu.askUser", lambda *args, **kwargs: False)

        assert delete_subscription("alpha-beta-gamma") is False
        assert "alpha-beta-gamma" in config_manager.get_stored_config()

    def test_confirmed_deletion_removes_subscription(self, monkeypatch):
        config_manager.write_config({"alpha-beta-gamma": {"deckId": 1}})
        monkeypatch.setattr("menu.askUser", lambda *args, **kwargs: True)
        monkeypatch.setattr("menu.unsubscribe_from_deck", lambda deck_hash: None)

        assert delete_subscription("alpha-beta-gamma") is True
        assert "alpha-beta-gamma" not in config_manager.get_stored_config()


class TestDeleteSubscriptionReviewData:
    """Unsubscribing must not strand uploaded review data on the server."""

    @pytest.fixture
    def granted(self, monkeypatch):
        config_manager.write_config(
            {
                "settings": {},
                "alpha-beta-gamma": {
                    "deckId": 1,
                    "stats_enabled": True,
                    "review_data_consent": {
                        "granted": True,
                        "wording_version": consent.CURRENT_WORDING_VERSION,
                        "decided_at": "2026-10-29T11:50:41+00:00",
                        "account": "account-1",
                    },
                },
            }
        )
        monkeypatch.setattr(consent, "current_account", lambda: "account-1")
        monkeypatch.setattr("menu.unsubscribe_from_deck", lambda deck_hash: None)
        monkeypatch.setattr("menu._erase_review_data", lambda deck_hash: None)

    def test_consent_holder_is_asked_about_their_review_data(
        self, granted, monkeypatch
    ):
        messages = []
        answers = iter([True, False])  # delete the subscription, keep the data

        def ask(message, **kwargs):
            messages.append(message)
            return next(answers)

        monkeypatch.setattr("menu.askUser", ask)

        delete_subscription("alpha-beta-gamma")

        assert len(messages) == 2
        assert "review statistics" in messages[1]

    def test_declining_erasure_leaves_the_queue_empty(self, granted, monkeypatch):
        answers = iter([True, False])  # delete the subscription, keep the data
        monkeypatch.setattr("menu.askUser", lambda *a, **k: next(answers))

        delete_subscription("alpha-beta-gamma")

        assert consent.pending_erasures() == []

    def test_accepting_erasure_queues_before_the_entry_is_dropped(
        self, granted, monkeypatch
    ):
        # The queue is account-level, so it must survive the subscription pop.
        monkeypatch.setattr("menu.askUser", lambda *a, **k: True)

        delete_subscription("alpha-beta-gamma")

        stored = config_manager.get_stored_config()
        assert "alpha-beta-gamma" not in stored
        assert consent.pending_erasures() == ["alpha-beta-gamma"]

    def test_no_question_without_consent(self, monkeypatch):
        config_manager.write_config(
            {
                "settings": {},
                "alpha-beta-gamma": {
                    "deckId": 1,
                    "stats_enabled": True,
                    "review_data_consent": {
                        "granted": False,
                        "wording_version": consent.CURRENT_WORDING_VERSION,
                        "decided_at": "2026-10-29T11:50:41+00:00",
                        "account": "account-1",
                    },
                },
            }
        )
        monkeypatch.setattr(consent, "current_account", lambda: "account-1")
        monkeypatch.setattr("menu.unsubscribe_from_deck", lambda deck_hash: None)
        monkeypatch.setattr("menu._erase_review_data", lambda deck_hash: None)
        questions = []
        monkeypatch.setattr(
            "menu.askUser", lambda message, **kwargs: questions.append(message) or True
        )

        delete_subscription("alpha-beta-gamma")

        assert len(questions) == 1
        assert consent.pending_erasures() == []


class TestMainThreadLoginUiSafety:
    """UI login state updates and the Login/Logout button must never perform
    synchronous network token refresh on the main thread."""

    def test_update_ui_for_login_state_uses_has_stored_token_without_network(
        self, mw_mock, monkeypatch
    ):
        config_manager.write_auth(
            {
                "token": "expired_tok",
                "refresh_token": "valid_ref",
                "expires_timestamp": 1.0,
            }
        )
        monkeypatch.setattr("menu.refresh_notifications", lambda: None)
        get_token_mock = MagicMock()
        monkeypatch.setattr("menu.auth_manager.get_token", get_token_mock)
        monkeypatch.setattr("menu.auth_manager._loaded", False)

        update_ui_for_login_state()

        get_token_mock.assert_not_called()

    def test_login_button_with_stored_token_logs_out_without_refreshing_first(
        self, mw_mock, monkeypatch
    ):
        config_manager.write_auth(
            {
                "token": "expired_tok",
                "refresh_token": "valid_ref",
                "expires_timestamp": 1.0,
            }
        )
        monkeypatch.setattr("menu.auth_manager._loaded", False)
        get_token_mock = MagicMock()
        logout_mock = MagicMock()
        monkeypatch.setattr("menu.auth_manager.get_token", get_token_mock)
        monkeypatch.setattr("menu.force_logout", logout_mock)
        monkeypatch.setattr("menu.showInfo", lambda *_a, **_kw: None)

        on_login_manager_btn()

        get_token_mock.assert_not_called()
        logout_mock.assert_called_once_with(False)
