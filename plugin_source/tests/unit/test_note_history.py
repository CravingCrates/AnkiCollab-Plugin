from unittest.mock import MagicMock, patch
from plugin_source.note_history import open_note_history_for_note_id


class TestNoteHistory:
    def test_open_note_history_missing_note(self):
        with patch("plugin_source.note_history.mw") as mock_mw, \
             patch("plugin_source.note_history.aqt.utils.showInfo") as mock_showinfo:
            mock_mw.col = MagicMock()
            mock_mw.col.get_note.return_value = None
            open_note_history_for_note_id(999)
            mock_showinfo.assert_called_once_with("Could not find note.", parent=mock_mw)

    def test_open_note_history_success(self):
        mock_note = MagicMock()
        mock_card = MagicMock()
        mock_note.cards.return_value = [mock_card]

        with patch("plugin_source.note_history.mw") as mock_mw, \
             patch("plugin_source.hooks.get_deck_hash_from_card", return_value=("hash_deck_123", None)), \
             patch("plugin_source.hooks.get_note_guid_from_id", return_value="guid_456"), \
             patch("plugin_source.note_history.show_note_history_dialog") as mock_show_dialog:
            mock_mw.col = MagicMock()
            mock_mw.col.get_note.return_value = mock_note

            open_note_history_for_note_id(42)
            mock_show_dialog.assert_called_once_with(
                deck_hash="hash_deck_123",
                guid="guid_456",
                parent=mock_mw,
            )
