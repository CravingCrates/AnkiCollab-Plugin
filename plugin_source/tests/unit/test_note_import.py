"""Tests for Note — import config handling, protected fields, tags, field mapping.

These verify the critical paths that ensure:
- Protected fields (both maintainer and user) are preserved during import
- Field mapping is applied correctly when notetype structure changes
- Tags (protected, optional, default) are handled correctly
- Note creation from JSON and update flows work
- Edge cases like field count mismatches are handled gracefully
"""

import copy
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from collections import defaultdict

from tests.conftest import make_notetype, make_note_dict, MockAnkiNote

from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel
from crowd_anki.utils.constants import UUID_FIELD_NAME

from utils import (
    get_personal_tags,
)

# ──────────────────────────────────────────────────────────────────────
# Note construction
# ──────────────────────────────────────────────────────────────────────


class TestNoteConstruction:
    def test_from_json(self):
        note_dict = make_note_dict(guid="abc123", fields=["Hello", "World"])
        note = Note.from_json(note_dict)
        assert note.get_uuid() == "abc123"
        assert note.note_model_uuid == "model-uuid-001"
        assert note.anki_object_dict["fields"] == ["Hello", "World"]

    def test_from_json_preserves_tags(self):
        note_dict = make_note_dict(tags=["tag1", "tag2", "tag3"])
        note = Note.from_json(note_dict)
        assert note.anki_object_dict["tags"] == ["tag1", "tag2", "tag3"]

    def test_from_json_custom_model_uuid(self):
        note_dict = make_note_dict(note_model_uuid="custom-uuid-xyz")
        note = Note.from_json(note_dict)
        assert note.note_model_uuid == "custom-uuid-xyz"


# ──────────────────────────────────────────────────────────────────────
# handle_import_config_changes — maintainer protected fields
# ──────────────────────────────────────────────────────────────────────


class TestMaintainerProtectedFields:
    """Test that fields marked as personal by the maintainer are preserved."""

    def _setup_note_for_import(
        self, old_fields, new_fields, notetype, personal_fields=None, field_mapping=None
    ):
        """Helper to set up a note for import testing.

        Args:
            old_fields: The user's current local fields
            new_fields: The new fields coming from remote
            notetype: The notetype dict
            personal_fields: List of (model_name, field_name) pairs
            field_mapping: Field index mapping (new→old)
        """
        note_dict = make_note_dict(fields=new_fields)
        note = Note.from_json(note_dict)

        # Set up mock config
        config = MagicMock()
        if personal_fields:

            def is_personal(model_name, field_name):
                return (model_name, field_name) in personal_fields

            config.is_personal_field = MagicMock(side_effect=is_personal)
        else:
            config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        note.config = config

        # Set up existing anki_object (simulating the user's local note)
        mock_note = MockAnkiNote()
        mock_note.fields = list(old_fields)
        mock_note.tags = []
        mock_note.guid = note_dict["guid"]
        note.anki_object = mock_note

        nm = NoteModel(notetype)

        if field_mapping is None:
            field_mapping = list(range(len(notetype["flds"])))

        return note, nm, config, field_mapping

    def test_no_protected_fields(self):
        """Without protected fields, all fields should be updated."""
        nt = make_notetype(fields=["Front", "Back"])
        note, nm, config, fm = self._setup_note_for_import(
            old_fields=["old front", "old back"],
            new_fields=["new front", "new back"],
            notetype=nt,
        )
        note.handle_import_config_changes(config, nm, fm)
        assert note.anki_object_dict["fields"] == ["new front", "new back"]

    def test_protected_field_preserves_old_content(self):
        """A protected field should retain the user's local value."""
        nt = make_notetype(fields=["Front", "Back"])
        note, nm, config, fm = self._setup_note_for_import(
            old_fields=["my personal front", "old back"],
            new_fields=["new front from remote", "new back"],
            notetype=nt,
            personal_fields=[("Basic", "Front")],
        )
        note.handle_import_config_changes(config, nm, fm)
        # Front should be preserved (protected), Back should be updated
        assert note.anki_object_dict["fields"][0] == "my personal front"
        assert note.anki_object_dict["fields"][1] == "new back"

    def test_multiple_protected_fields(self):
        nt = make_notetype(fields=["Term", "Definition", "Notes", "Source"])
        note, nm, config, fm = self._setup_note_for_import(
            old_fields=["my term", "my def", "my notes", "my source"],
            new_fields=["new term", "new def", "new notes", "new source"],
            notetype=nt,
            personal_fields=[("Basic", "Notes"), ("Basic", "Source")],
        )
        note.handle_import_config_changes(config, nm, fm)
        assert note.anki_object_dict["fields"][0] == "new term"
        assert note.anki_object_dict["fields"][1] == "new def"
        assert note.anki_object_dict["fields"][2] == "my notes"  # protected
        assert note.anki_object_dict["fields"][3] == "my source"  # protected

    def test_protected_field_with_field_mapping(self):
        """Protected fields should work correctly even when fields are reordered."""
        nt = make_notetype(fields=["Back", "Front"])  # swapped order
        note, nm, config, fm = self._setup_note_for_import(
            old_fields=["old front", "old back"],
            new_fields=["new back", "new front"],
            notetype=nt,
            personal_fields=[("Basic", "Front")],
            field_mapping=[1, 0],  # new[0]=Back was old[1], new[1]=Front was old[0]
        )
        note.handle_import_config_changes(config, nm, fm)
        # Front (now at position 1) should be preserved
        assert note.anki_object_dict["fields"][1] == "old front"
        assert note.anki_object_dict["fields"][0] == "new back"


# ──────────────────────────────────────────────────────────────────────
# handle_import_config_changes — user protected fields (tags)
# ──────────────────────────────────────────────────────────────────────


class TestUserProtectedFields:
    """Test that fields users protect via AnkiCollab_Protect tags are preserved."""

    def _setup_protected_note(
        self, old_fields, new_fields, notetype, protect_tags, field_mapping=None
    ):
        note_dict = make_note_dict(fields=new_fields, tags=["some_tag"])
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        note.config = config

        mock_note = MockAnkiNote()
        mock_note.fields = list(old_fields)
        mock_note.tags = list(protect_tags)
        mock_note.guid = note_dict["guid"]
        note.anki_object = mock_note

        nm = NoteModel(notetype)
        if field_mapping is None:
            field_mapping = list(range(len(notetype["flds"])))
        return note, nm, config, field_mapping

    def test_user_protect_single_field(self):
        nt = make_notetype(fields=["Front", "Back"])
        note, nm, config, fm = self._setup_protected_note(
            old_fields=["my front", "my back"],
            new_fields=["new front", "new back"],
            notetype=nt,
            protect_tags=["AnkiCollab_Protect::Front"],
        )
        note.handle_import_config_changes(config, nm, fm)
        assert note.anki_object_dict["fields"][0] == "my front"
        assert note.anki_object_dict["fields"][1] == "new back"

    def test_user_protect_all_fields(self):
        nt = make_notetype(fields=["Front", "Back"])
        note, nm, config, fm = self._setup_protected_note(
            old_fields=["my front", "my back"],
            new_fields=["new front", "new back"],
            notetype=nt,
            protect_tags=["AnkiCollab_Protect::All"],
        )
        note.handle_import_config_changes(config, nm, fm)
        assert note.anki_object_dict["fields"][0] == "my front"
        assert note.anki_object_dict["fields"][1] == "my back"

    def test_user_protect_tags_field(self):
        nt = make_notetype(fields=["Front", "Back"])
        note, nm, config, fm = self._setup_protected_note(
            old_fields=["front", "back"],
            new_fields=["new front", "new back"],
            notetype=nt,
            protect_tags=["AnkiCollab_Protect::Tags"],
        )
        # Tags should be preserved from local note
        note.handle_import_config_changes(config, nm, fm)
        assert "AnkiCollab_Protect::Tags" in note.anki_object_dict["tags"]

    def test_user_protect_with_field_mapping(self):
        """User protect tags work correctly with field remapping."""
        nt = make_notetype(fields=["Back", "Front"])  # swapped
        note, nm, config, fm = self._setup_protected_note(
            old_fields=["old front", "old back"],
            new_fields=["new back", "new front"],
            notetype=nt,
            protect_tags=["AnkiCollab_Protect::Front"],
            field_mapping=[1, 0],
        )
        note.handle_import_config_changes(config, nm, fm)
        # Front is now at index 1 in new structure, should be preserved
        assert note.anki_object_dict["fields"][1] == "old front"


# ──────────────────────────────────────────────────────────────────────
# Default protected tags (leech, marked, missing-media)
# ──────────────────────────────────────────────────────────────────────


class TestDefaultProtectedTags:
    def test_leech_tag_preserved(self):
        nt = make_notetype(fields=["Front", "Back"])
        note_dict = make_note_dict(fields=["new f", "new b"], tags=["test"])
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        config.personal_tags = get_personal_tags("xx")

        mock_note = MockAnkiNote()
        mock_note.fields = ["old f", "old b"]
        mock_note.tags = ["leech", "test"]
        note.anki_object = mock_note

        nm = NoteModel(nt)
        fm = [0, 1]
        note.handle_import_config_changes(config, nm, fm)
        assert "leech" in note.anki_object_dict["tags"]

    def test_marked_tag_preserved(self):
        nt = make_notetype(fields=["Front", "Back"])
        note_dict = make_note_dict(fields=["new f", "new b"], tags=["test"])
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        config.personal_tags = get_personal_tags("xx")

        mock_note = MockAnkiNote()
        mock_note.fields = ["old f", "old b"]
        mock_note.tags = ["marked", "test"]
        note.anki_object = mock_note

        nm = NoteModel(nt)
        fm = [0, 1]
        note.handle_import_config_changes(config, nm, fm)
        assert "marked" in note.anki_object_dict["tags"]

    def test_personal_tag_preserved(self):
        nt = make_notetype(fields=["Front", "Back"])
        note_dict = make_note_dict(fields=["new f", "new b"], tags=["test"])
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        config.personal_tags = get_personal_tags("xx")

        mock_note = MockAnkiNote()
        mock_note.fields = ["old f", "old b"]
        mock_note.tags = ["test", "AnkiCollab_Personal::MyTag"]
        note.anki_object = mock_note

        nm = NoteModel(nt)
        fm = [0, 1]
        note.handle_import_config_changes(config, nm, fm)
        assert "AnkiCollab_Personal::MyTag" in note.anki_object_dict["tags"]


# ──────────────────────────────────────────────────────────────────────
# Optional tags handling
# ──────────────────────────────────────────────────────────────────────


class TestOptionalTags:
    def test_optional_tags_filtered(self):
        nt = make_notetype(fields=["Front", "Back"])
        note_dict = make_note_dict(
            fields=["f", "b"],
            tags=[
                "AnkiCollab_Optional::Pathology",
                "AnkiCollab_Optional::Cardiology",
                "regular_tag",
            ],
        )
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = True
        config.optional_tags = ["Pathology"]  # Only subscribed to Pathology

        mock_note = MockAnkiNote()
        mock_note.fields = ["f", "b"]
        mock_note.tags = []
        note.anki_object = mock_note

        nm = NoteModel(nt)
        fm = [0, 1]
        note.handle_import_config_changes(config, nm, fm)
        assert "AnkiCollab_Optional::Pathology" in note.anki_object_dict["tags"]
        assert "AnkiCollab_Optional::Cardiology" not in note.anki_object_dict["tags"]
        assert "regular_tag" in note.anki_object_dict["tags"]


# ──────────────────────────────────────────────────────────────────────
# Field count mismatch handling
# ──────────────────────────────────────────────────────────────────────


class TestFieldCountMismatch:
    def test_extra_fields_truncated(self):
        """Remote note has more fields than notetype → truncate."""
        nt = make_notetype(fields=["Front", "Back"])
        note_dict = make_note_dict(fields=["f", "b", "extra1", "extra2"])
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False

        mock_note = MockAnkiNote()
        mock_note.fields = ["old f", "old b"]
        mock_note.tags = []
        note.anki_object = mock_note

        nm = NoteModel(nt)
        fm = [0, 1]
        note.handle_import_config_changes(config, nm, fm)
        assert len(note.anki_object_dict["fields"]) == 2

    def test_missing_fields_padded(self):
        """Remote note has fewer fields than notetype → pad with empty."""
        nt = make_notetype(fields=["Front", "Back", "Extra"])
        note_dict = make_note_dict(fields=["f"])  # only 1 field
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False

        mock_note = MockAnkiNote()
        mock_note.fields = ["old f", "old b", "old extra"]
        mock_note.tags = []
        note.anki_object = mock_note

        nm = NoteModel(nt)
        fm = [0, 1, 2]
        note.handle_import_config_changes(config, nm, fm)
        assert len(note.anki_object_dict["fields"]) == 3

    def test_local_only_fields_appended(self):
        """When user has local-only fields, they should be appended."""
        nt = make_notetype(fields=["Front", "Back", "LocalField"])
        note_dict = make_note_dict(fields=["new f", "new b"])
        note = Note.from_json(note_dict)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False

        mock_note = MockAnkiNote()
        mock_note.fields = ["old f", "old b", "my local stuff"]
        mock_note.tags = []
        note.anki_object = mock_note

        nm = NoteModel(nt)
        # field_mapping: 0→0, 1→1, 2→2 (local field mapped to itself)
        fm = [0, 1, 2]
        note.handle_import_config_changes(config, nm, fm)
        # Should have 3 fields after padding
        assert len(note.anki_object_dict["fields"]) == 3


# ──────────────────────────────────────────────────────────────────────
# Tag removal (for export)
# ──────────────────────────────────────────────────────────────────────


class TestTagRemoval:
    def test_remove_exact_tag(self):
        note = Note()
        note.anki_object = MockAnkiNote()
        note.anki_object.tags = ["keep_me", "remove_me", "also_keep"]
        note.remove_tags(["remove_me"])
        assert "remove_me" not in note.anki_object.tags
        assert "keep_me" in note.anki_object.tags

    def test_remove_hierarchical_tag(self):
        note = Note()
        note.anki_object = MockAnkiNote()
        note.anki_object.tags = ["mytag", "mytag::subtag", "mytag::other", "unrelated"]
        note.remove_tags(["mytag"])
        assert "mytag" not in note.anki_object.tags
        assert "mytag::subtag" not in note.anki_object.tags
        assert "unrelated" in note.anki_object.tags

    def test_remove_whitespace_tags(self):
        note = Note()
        note.anki_object = MockAnkiNote()
        note.anki_object.tags = ["good", "  ", "", "also_good"]
        note.remove_tags(["dummy"])
        assert "  " not in note.anki_object.tags
        assert "" not in note.anki_object.tags

    def test_remove_no_anki_object(self):
        note = Note()
        note.anki_object = None
        # Should not raise
        note.remove_tags(["anything"])


# ──────────────────────────────────────────────────────────────────────
# Bulk operations
# ──────────────────────────────────────────────────────────────────────


class TestBulkAddNotes:
    @patch("crowd_anki.representation.note.ANKI_INT_VERSION", 231100)
    def test_bulk_add_uses_add_notes(self):
        col = MagicMock()
        notes = []
        for i in range(5):
            n = Note()
            n.anki_object = MockAnkiNote()
            n.anki_object.id = i
            notes.append(n)

        import_config = MagicMock()
        import_config.suspend_new_cards = False

        Note.bulk_add_notes(col, notes, 1, import_config)
        col.add_notes.assert_called()

    @patch("crowd_anki.representation.note.ANKI_INT_VERSION", 231100)
    def test_bulk_add_suspends_when_configured(self):
        col = MagicMock()
        n = Note()
        n.anki_object = MockAnkiNote()
        n.anki_object.id = 1
        n.anki_object._card_ids = [100, 101]

        import_config = MagicMock()
        import_config.suspend_new_cards = True

        Note.bulk_add_notes(col, [n], 1, import_config)
        col.sched.suspend_cards.assert_called()

    @patch("crowd_anki.representation.note.ANKI_INT_VERSION", 231100)
    def test_bulk_add_chunks_collection_requests_and_suspends_all_cards(self):
        col = MagicMock()
        notes = []
        for index in range(1001):
            note = Note()
            note.anki_object = MockAnkiNote()
            note.anki_object._card_ids = [index]
            notes.append(note)

        import_config = MagicMock(suspend_new_cards=True)

        with patch("crowd_anki.representation.note.AddNoteRequest") as add_request:
            Note.bulk_add_notes(col, notes, 7, import_config)

        assert [len(call.args[0]) for call in col.add_notes.call_args_list] == [
            1000,
            1,
        ]
        assert [
            len(call.args[0]) for call in col.sched.suspend_cards.call_args_list
        ] == [
            1000,
            1,
        ]
        assert all(call.kwargs["deck_id"] == 7 for call in add_request.call_args_list)
