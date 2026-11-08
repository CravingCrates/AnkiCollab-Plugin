"""Unit tests for Note History dialog, token diffing, and restoration."""

from unittest.mock import MagicMock, patch
import pytest

from aqt.qt import QApplication, QWidget

# Ensure QApplication exists for widget testing
_app = QApplication.instance() or QApplication([])

from plugin_source import note_history
from plugin_source.note_history import (
    FieldComparisonCard,
    NoteHistoryDialog,
    TagsComparisonCard,
    UnchangedFieldsCollapsible,
    _render_diff_sides,
    _tokenize,
    normalize_html_tags,
    open_note_history_for_note_id,
    show_note_history_dialog,
)


class TestDiffHelpers:
    """Test suite for HTML normalization and token-level diff rendering."""

    def test_normalize_html_tags_void_tags(self):
        text = "Hello<br />World<br/>!<hr />End<hr>"
        normalized = normalize_html_tags(text)
        assert "<br />" not in normalized
        assert "<br/>" not in normalized
        assert normalized == "Hello<br>World<br>!<hr>End<hr>"

    def test_normalize_html_tags_attributes(self):
        text = "<span class='highlight' style='color: red' >Text</span>"
        normalized = normalize_html_tags(text)
        assert 'class="highlight"' in normalized
        assert 'style="color: red"' in normalized
        assert "<span " in normalized
        assert " >" not in normalized

    def test_normalize_html_tags_empty(self):
        assert normalize_html_tags("") == ""
        assert normalize_html_tags(None) == ""

    def test_tokenize(self):
        text = "<b>L'artère</b> fémorale (gauche)"
        tokens = _tokenize(text)
        assert "<b>" in tokens
        assert "</b>" in tokens
        assert "L'artère" in tokens
        assert "fémorale" in tokens
        assert "(" in tokens
        assert "gauche" in tokens
        assert ")" in tokens

    def test_tokenize_empty(self):
        assert _tokenize("") == []
        assert _tokenize(None) == []

    def test_render_diff_sides(self):
        colors = {
            "background": "#1E1E1E",
            "rem_bg": "#2b0c0c",
            "rem_fg": "#f87171",
            "add_bg": "#032117",
            "add_fg": "#34d399",
        }
        old_text = "Dose initiale : 10 mg"
        new_text = "Dose initiale : 20 mg"
        old_h, new_h = _render_diff_sides(old_text, new_text, colors)

        # Deletion in old_side
        assert "10" in old_h
        assert "line-through" in old_h
        # Addition in new_side
        assert "20" in new_h
        assert "font-weight: 600" in new_h
        # Shared text unchanged
        assert "Dose initiale :" in old_h
        assert "Dose initiale :" in new_h


class TestComparisonCards:
    """Test suite for FieldComparisonCard, TagsComparisonCard, and UnchangedFieldsCollapsible."""

    def test_field_comparison_card_init(self):
        restored = []
        card = FieldComparisonCard(
            field_name="Posologie",
            old_val="10 mg",
            new_val="20 mg",
            current_val="20 mg",
            on_restore_old=lambda: restored.append("old"),
            on_restore_new=lambda: restored.append("new"),
        )
        assert card._field_name == "Posologie"
        assert card._old_val == "10 mg"
        assert card._new_val == "20 mg"
        assert card._current_val == "20 mg"

        # Verify render mode toggle
        card.set_render_mode("raw")
        assert card._render_mode == "raw"
        card.set_render_mode("render")
        assert card._render_mode == "render"
        card.set_render_mode("diff")
        assert card._render_mode == "diff"

    def test_tags_comparison_card_init(self):
        restored_tags = []
        card = TagsComparisonCard(
            old_tags=["cardio", "traitement"],
            new_tags=["cardio", "urgence"],
            current_tags=["cardio", "traitement"],
            on_restore_old=lambda: restored_tags.append("old"),
            on_restore_new=lambda: restored_tags.append("new"),
        )
        assert card is not None

    def test_unchanged_fields_collapsible(self):
        restored = {}
        unchanged = {"Source": "Collège 2024", "Rang": "Rang A"}
        current = {"Source": "Collège 2024", "Rang": "Rang A", "Texte": "Modifié"}
        drawer = UnchangedFieldsCollapsible(
            unchanged_fields=unchanged,
            current_fields=current,
            on_restore_field=lambda fn, val: restored.update({fn: val}),
        )
        assert drawer._expanded is False
        drawer._toggle()
        assert drawer._expanded is True
        drawer._toggle()
        assert drawer._expanded is False


class TestNoteHistoryDialog:
    """Test suite for the NoteHistoryDialog workflow and restoration."""

    @pytest.fixture
    def mock_history_data(self):
        return {
            "note_id": 12345,
            "guid": "abcdef123456",
            "deck_name": "Simulation::EDN",
            "groups": [
                {
                    "commit_id": 42,
                    "commit_author": "feufan",
                    "commit_rationale": "Correction posologie",
                    "commit_timestamp": "2026-11-08 10:00:00",
                    "approved": True,
                    "denied": False,
                    "events": [
                        {
                            "id": 1,
                            "version": 2,
                            "event_type": "field_updated",
                            "field_name": "Posologie",
                            "old_human": "10 mg par jour",
                            "new_human": "20 mg par jour",
                            "diff_html": "<del>10</del><ins>20</ins> mg par jour",
                            "created_at": "2026-11-08 10:00:00",
                        },
                        {
                            "id": 2,
                            "version": 2,
                            "event_type": "tag_added",
                            "field_name": None,
                            "old_human": None,
                            "new_human": "#cardiologie",
                            "diff_html": None,
                            "created_at": "2026-11-08 10:00:00",
                        },
                    ],
                },
                {
                    "commit_id": 10,
                    "commit_author": "initial_author",
                    "commit_rationale": "Creation",
                    "commit_timestamp": "2026-11-01 12:00:00",
                    "approved": True,
                    "denied": False,
                    "events": [
                        {
                            "id": 0,
                            "version": 1,
                            "event_type": "note_created",
                            "field_name": None,
                            "old_human": None,
                            "new_human": None,
                            "diff_html": None,
                            "created_at": "2026-11-01 12:00:00",
                        }
                    ],
                },
            ],
        }

    def test_dialog_renders_commits_and_filters(self, mock_history_data):
        with patch.object(note_history, "mw") as mock_mw, patch.object(NoteHistoryDialog, "_load_history"):
            mock_mw.col = None
            dialog = NoteHistoryDialog(
                deck_hash="hash123",
                guid="abcdef123456",
                note_id=12345,
            )
            dialog._render_history_data(mock_history_data)

            # Check that commit groups are stored
            assert len(dialog._commit_groups) == 2
            assert dialog._selected_group["commit_id"] == 42
            assert dialog._selected_group["commit_author"] == "feufan"

            # Check comparison cards were created for changed fields
            assert len(dialog._comparison_cards) == 1
            assert dialog._comparison_cards[0]._field_name == "Posologie"

            # Check filter change
            dialog._filter_field = "__tags__"
            dialog._render_selected_commit()
            assert len(dialog._comparison_cards) == 0

    def test_dialog_field_restoration(self, mock_history_data):
        mock_note = {
            "Posologie": "5 mg",
            "Remarques": "Aucune",
        }

        class MockNote(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.tags = ["initial"]

        note_obj = MockNote(mock_note)

        mock_col = MagicMock()
        mock_col.get_note.return_value = note_obj

        with patch.object(note_history, "mw") as mock_mw, patch.object(NoteHistoryDialog, "_load_history"):
            mock_mw.col = mock_col
            mock_mw.checkpoint = MagicMock()

            dialog = NoteHistoryDialog(
                deck_hash="hash123",
                guid="abcdef123456",
                note_id=12345,
            )
            dialog._current_fields = {"Posologie": "5 mg", "Remarques": "Aucune"}
            dialog._current_tags = ["initial"]
            dialog._render_history_data(mock_history_data)

            # Restore single field
            dialog._restore_field_to_anki("Posologie", "20 mg par jour")
            assert note_obj["Posologie"] == "20 mg par jour"
            assert dialog._current_fields["Posologie"] == "20 mg par jour"
            mock_col.update_note.assert_called_with(note_obj)

            # Restore full commit
            dialog._on_restore_full_commit()
            assert note_obj["Posologie"] == "20 mg par jour"
            mock_col.update_note.assert_called_with(note_obj)


class TestNoteHistoryOpen:
    """Test suite for top-level helper functions opening the dialog."""

    def test_open_note_history_missing_note(self):
        with patch.object(note_history, "mw") as mock_mw, \
             patch("plugin_source.note_history.aqt.utils.showInfo") as mock_showinfo:
            mock_mw.col = MagicMock()
            mock_mw.col.get_note.return_value = None
            open_note_history_for_note_id(999)
            mock_showinfo.assert_called_once_with("Could not find note.", parent=mock_mw)

    def test_open_note_history_success(self):
        mock_note = MagicMock()
        mock_card = MagicMock()
        mock_note.cards.return_value = [mock_card]

        with patch.object(note_history, "mw") as mock_mw, \
             patch("plugin_source.hooks.get_deck_hash_from_card", return_value=("hash_deck_123", None)), \
             patch("plugin_source.hooks.get_note_guid_from_id", return_value="guid_456"), \
             patch("plugin_source.note_history.show_note_history_dialog") as mock_show_dialog:
            mock_mw.col = MagicMock()
            mock_mw.col.get_note.return_value = mock_note

            open_note_history_for_note_id(42)
            mock_show_dialog.assert_called_once_with(
                deck_hash="hash_deck_123",
                guid="guid_456",
                note_id=42,
                editor=None,
                parent=mock_mw,
            )

