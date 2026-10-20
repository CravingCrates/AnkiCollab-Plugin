"""Tests for menu.py — deck-hash validation and default-config seeding.

Only functions that do not touch Qt rendering are tested here.  These are the
entry-point guards that run for every user: a deck hash that is accepted or
rejected wrongly, or defaults that overwrite a user's settings, would be a
user-visible regression with no UI to catch it.
"""

import re

import pytest

import config_manager
from menu import validate_deck_hash, store_default_config
from tests.mocks import spy_config_writes

_ALL_DEFAULTS = {
    "pull_on_startup": False,
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
        assert stored["settings"]["pull_on_startup"] is False
        assert "auth" not in stored
