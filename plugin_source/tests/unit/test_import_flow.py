"""Mutation-driven tests for import_manager's import entry/flow functions.

Covers:
- ``import_webresult``: None / no-collection / empty / backup-failure /
  new-deck vs update paths.
- ``get_home_deck`` / ``get_new_notes_home_deck``: deck resolution and
  new-notes-home-deck fallback.
"""

from unittest.mock import MagicMock, patch

import pytest

import import_manager
from utils import BackupFailedError

# ──────────────────────────────────────────────────────────────────────
# import_webresult
# ──────────────────────────────────────────────────────────────────────


class TestImportWebresult:
    def test_none_data_returns_quietly(self, mw_mock):
        assert import_manager.import_webresult(None) is None

    def test_none_webresult_returns(self, mw_mock):
        assert import_manager.import_webresult((None, None, False)) is None

    def test_no_collection_available(self, mw_mock, mw_no_col):
        with patch("import_manager.aqt.utils.showWarning") as m_warn:
            import_manager.import_webresult(([{"deck_hash": "h1"}], None, False))
        m_warn.assert_called_once()

    def test_no_collection_available_silent_skips_warning(self, mw_mock, mw_no_col):
        with patch("import_manager.aqt.utils.showWarning") as m_warn:
            import_manager.import_webresult(([{"deck_hash": "h1"}], None, True))
        m_warn.assert_not_called()

    def test_empty_webresult_silent_is_quiet(self, mw_mock):
        with (
            patch("import_manager.aqt.utils.tooltip") as m_tip,
            patch("import_manager.update_stats") as m_stats,
        ):
            import_manager.import_webresult(([], None, True))
        m_tip.assert_not_called()
        m_stats.assert_called_once_with(silent=True)

    def test_empty_webresult_non_silent(self, mw_mock):
        with (
            patch("import_manager.aqt.utils.tooltip") as m_tip,
            patch("import_manager.update_stats") as m_stats,
        ):
            import_manager.import_webresult(([], None, False))
        assert "up to date" in m_tip.call_args.args[0]
        m_stats.assert_called_once_with(silent=False)

    def test_backup_failure_aborts(self, mw_mock):
        with (
            patch("import_manager.create_backup", side_effect=BackupFailedError("x")),
            patch("import_manager.aqt.utils.showWarning") as m_warn,
            patch("import_manager.install_update") as m_install,
        ):
            import_manager.import_webresult(([{"deck_hash": "h1"}], None, False))
        m_warn.assert_called_once()
        m_install.assert_not_called()

    def test_new_deck_uses_install_update(self, mw_mock):
        sub = {"deck_hash": "h1", "deck": {"name": "D"}}
        with (
            patch("import_manager.create_backup") as m_backup,
            patch("import_manager.install_update") as m_install,
            patch("import_manager.show_changelog_popup") as m_changelog,
            patch("import_manager.update_stats"),
        ):
            import_manager.import_webresult(([sub], "input_hash", False))
        m_install.assert_called_once()
        m_changelog.assert_not_called()

    def test_update_uses_changelog(self, mw_mock):
        sub = {"deck_hash": "h1", "deck": {"name": "D"}}
        with (
            patch("import_manager.create_backup"),
            patch("import_manager.install_update") as m_install,
            patch("import_manager.show_changelog_popup") as m_changelog,
            patch("import_manager.update_stats"),
        ):
            import_manager.import_webresult(([sub], None, False))
        m_changelog.assert_called_once()
        m_install.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# get_home_deck / get_new_notes_home_deck
# ──────────────────────────────────────────────────────────────────────


class TestHomeDecks:
    def test_get_home_deck_returns_deck_name(self, mw_mock):
        mw_mock.col.decks.name_if_exists.return_value = "MyHome"
        with patch("import_manager.DeckManager") as m_dm:
            m_dm.return_value.get_by_hash.return_value = {"deckId": 5}
            assert import_manager.get_home_deck("hash1") == "MyHome"

    def test_get_home_deck_unregistered(self, mw_mock):
        with patch("import_manager.DeckManager") as m_dm:
            m_dm.return_value.get_by_hash.return_value = {"deckId": 0}
            assert import_manager.get_home_deck("hash1") is None

    def test_get_new_notes_home_deck_uses_new_notes_deck(self, mw_mock):
        mw_mock.col.decks.name_if_exists.side_effect = lambda did: (
            "NewNotes" if did == 9 else "Home"
        )
        with patch("import_manager.DeckManager") as m_dm:
            m_dm.return_value.get_by_hash.return_value = {
                "deckId": 5,
                "new_notes_home_deck": 9,
            }
            assert import_manager.get_new_notes_home_deck("hash1") == "NewNotes"

    def test_get_new_notes_home_deck_falls_back_to_home(self, mw_mock):
        mw_mock.col.decks.name_if_exists.side_effect = lambda did: (
            "Home" if did == 5 else None
        )
        with patch("import_manager.DeckManager") as m_dm:
            m_dm.return_value.get_by_hash.return_value = {
                "deckId": 5,
                "new_notes_home_deck": 0,
            }
            assert import_manager.get_new_notes_home_deck("hash1") == "Home"
