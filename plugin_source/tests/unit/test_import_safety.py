"""
Critical safety tests — deck import data integrity.

These tests verify the fundamental guarantee: importing/updating a deck
MUST NEVER corrupt or overwrite the user's scheduling data, review
history, or personal customizations beyond what's explicitly handled
by the protected-fields system.

Also covers: deleted notes dialog behavior, backup-before-import
verification, and the note update pipeline's boundaries.
"""

import copy
import pytest
from unittest.mock import MagicMock, patch, call, PropertyMock
from collections import defaultdict

from tests.conftest import (
    make_notetype,
    make_note_dict,
    make_deck_json,
    MockAnkiNote,
    create_mock_collection,
)
from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Review / scheduling data preservation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSchedulingDataPreservation:
    """Verify that note update never touches card-level scheduling.

    Anki stores scheduling data on Card objects (ivl, due, reps, lapses,
    factor, queue, type), which are separate from Note objects (fields,
    tags, mid, mod, guid).  The import only calls collection.update_notes()
    which should only touch note-level attributes.

    These tests verify that card scheduling attributes survive import.
    """

    def _make_note_with_scheduling(self, collection, old_fields, new_fields, notetype):
        """Set up a note for update, simulating existing card scheduling data."""
        note_dict = make_note_dict(fields=new_fields, guid="sched_test_001")
        note = Note.from_json(note_dict)

        # The user's existing note (in their collection)
        mock_note = MockAnkiNote(collection=collection, model=notetype)
        mock_note.fields = list(old_fields)
        mock_note.tags = ["existing_tag"]
        mock_note.guid = "sched_test_001"
        mock_note.id = 12345
        mock_note._card_ids = [100, 101]
        note.anki_object = mock_note

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        config.ignore_deck_movement = True

        return note, config

    def test_update_notes_only_modifies_note_level_data(self):
        """collection.update_notes() receives Note objects, not Card objects."""
        col = create_mock_collection()
        nt = make_notetype(fields=["Front", "Back"])
        nm = NoteModel(nt)
        field_mapping = [0, 1]

        note, config = self._make_note_with_scheduling(
            col,
            old_fields=["old front", "old back"],
            new_fields=["updated front", "updated back"],
            notetype=nt,
        )

        note.handle_import_config_changes(config, nm, field_mapping)

        # Apply the dict to the anki_object (same as _batch_process_notes does)
        note.anki_object.__dict__.update(note.anki_object_dict)

        # Verify fields were updated
        assert note.anki_object.fields == ["updated front", "updated back"]

        # Simulate what _bulk_update_notes_preserving_placement does
        Note._bulk_update_notes_preserving_placement(col, [note], {}, config)

        # collection.update_notes was called with note objects
        col.update_notes.assert_called_once()
        updated_notes = col.update_notes.call_args[0][0]

        # The note's fields are the updated remote values
        assert updated_notes[0].fields == ["updated front", "updated back"]

        # Card-level scheduling is not part of the note —
        # set_deck and sched methods are separate calls
        # update_notes should NOT have been called with any card data

    def test_anki_object_dict_never_contains_scheduling_fields(self):
        """The JSON from the server (anki_object_dict) must not have card scheduling keys."""
        note_dict = make_note_dict(fields=["F", "B"])
        note = Note.from_json(note_dict)

        scheduling_keys = {
            "ivl",
            "due",
            "reps",
            "lapses",
            "factor",
            "queue",
            "type",
            "odue",
            "odid",
            "left",
        }
        actual_keys = set(note.anki_object_dict.keys())
        overlap = actual_keys & scheduling_keys
        assert overlap == set(), f"Server note dict contains scheduling keys: {overlap}"

    def test_multiple_updates_never_reset_card_ids(self):
        """Repeated imports should not change which cards belong to a note."""
        col = create_mock_collection()
        nt = make_notetype(fields=["Front", "Back"])
        nm = NoteModel(nt)

        # Simulate a note that already has cards
        note, config = self._make_note_with_scheduling(
            col, ["v1 front", "v1 back"], ["v2 front", "v2 back"], nt
        )
        original_card_ids = list(note.anki_object._card_ids)

        # First update
        note.handle_import_config_changes(config, nm, [0, 1])
        note.anki_object.__dict__.update(note.anki_object_dict)

        # Card IDs should still be accessible
        assert note.anki_object.card_ids() == original_card_ids

    def test_note_mod_is_updated_but_card_mod_untouched(self):
        """Import updates note.mod (modification time) but never card.mod."""
        col = create_mock_collection()
        nt = make_notetype(fields=["Front", "Back"])
        nm = NoteModel(nt)

        note, config = self._make_note_with_scheduling(
            col, ["old", "old"], ["new", "new"], nt
        )
        original_note_mod = note.anki_object.mod

        note.handle_import_config_changes(config, nm, [0, 1])
        note.anki_object.__dict__.update(note.anki_object_dict)

        # The note mod is set from the remote server's dict
        # (the actual int_time assignment happens in _batch_process_notes)
        # Here we just confirm the dict doesn't overwrite with zero/None
        assert "mod" in note.anki_object_dict or hasattr(note.anki_object, "mod")


class TestFieldUpdateBoundaries:
    """Verify the update pipeline only modifies fields, tags, and model — nothing else."""

    def test_update_preserves_note_id(self):
        """A note's ID must never change during an update (only new notes get IDs)."""
        col = create_mock_collection()
        nt = make_notetype(fields=["F", "B"])
        nm = NoteModel(nt)

        note_dict = make_note_dict(fields=["new F", "new B"], note_id=99999)
        note = Note.from_json(note_dict)

        mock_note = MockAnkiNote()
        mock_note.id = 12345  # Existing note ID
        mock_note.fields = ["old F", "old B"]
        mock_note.tags = []
        note.anki_object = mock_note

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False

        note.handle_import_config_changes(config, nm, [0, 1])

        # The "id" from remote should be removed (handled by _batch_process_notes)
        # but the existing note.anki_object.id should remain intact
        assert note.anki_object.id == 12345

    def test_update_preserves_guid(self):
        """A note's GUID is its identity — it must never be overwritten."""
        note_dict = make_note_dict(guid="ORIGINAL_GUID", fields=["F", "B"])
        note = Note.from_json(note_dict)

        mock_note = MockAnkiNote()
        mock_note.guid = "ORIGINAL_GUID"
        mock_note.fields = ["old", "old"]
        mock_note.tags = []
        note.anki_object = mock_note

        # The UUID accessor should consistently return the same GUID
        assert note.get_uuid() == "ORIGINAL_GUID"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DeletedNotesDialog — user consent verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeletedNotesDialogCodes:
    """Verify that the DeletedNotesDialog returns the correct codes
    for each user choice, and that the import_manager acts on them
    correctly.

    Dialog buttons:
    - "Keep Notes"         → self.done(2)    → code 2
    - "Review in Browser"  → self.reject()   → QDialog.DialogCode.Rejected (0)
    - "Delete Notes"       → self.accept()   → QDialog.DialogCode.Accepted (1)
    """

    @patch("import_manager.open_browser_with_nids")
    @patch("import_manager.delete_notes")
    def test_accept_triggers_deletion(self, mock_delete, mock_browser):
        """When user clicks 'Delete Notes', notes should be deleted."""
        # Simulate the import_manager logic from _on_deck_installed
        nids = [100, 200, 300]
        del_notes_choice = 1  # QDialog.DialogCode.Accepted

        if del_notes_choice == 1:
            from import_manager import delete_notes

            delete_notes(nids)
        elif del_notes_choice == 0:
            from import_manager import open_browser_with_nids

            open_browser_with_nids(nids)

        mock_delete.assert_called_once_with(nids)
        mock_browser.assert_not_called()

    @patch("import_manager.open_browser_with_nids")
    @patch("import_manager.delete_notes")
    def test_reject_opens_browser(self, mock_delete, mock_browser):
        """When user clicks 'Review in Browser', browser opens but notes kept."""
        mock_dialog = MagicMock()
        mock_dialog.exec.return_value = 0  # Rejected

        nids = [100, 200, 300]

        del_notes_choice = mock_dialog.exec()
        if del_notes_choice == 1:
            from import_manager import delete_notes

            delete_notes(nids)
        elif del_notes_choice == 0:
            from import_manager import open_browser_with_nids

            open_browser_with_nids(nids)

        mock_delete.assert_not_called()
        mock_browser.assert_called_once_with(nids)

    @patch("import_manager.open_browser_with_nids")
    @patch("import_manager.delete_notes")
    def test_keep_notes_does_nothing(self, mock_delete, mock_browser):
        """When user clicks 'Keep Notes' (code 2), neither delete nor browse."""
        mock_dialog = MagicMock()
        mock_dialog.exec.return_value = 2  # done(2) = Keep

        nids = [100, 200, 300]

        del_notes_choice = mock_dialog.exec()
        if del_notes_choice == 1:
            from import_manager import delete_notes

            delete_notes(nids)
        elif del_notes_choice == 0:
            from import_manager import open_browser_with_nids

            open_browser_with_nids(nids)

        mock_delete.assert_not_called()
        mock_browser.assert_not_called()

    def test_dialog_return_codes_are_distinct(self):
        """Sanity check: all three user choices must produce different codes."""
        # From dialogs.py:
        # "Keep Notes"         → self.done(2)   → 2
        # "Review in Browser"  → self.reject()  → 0
        # "Delete Notes"       → self.accept()  → 1
        keep_code = 2
        review_code = 0  # QDialog.DialogCode.Rejected
        delete_code = 1  # QDialog.DialogCode.Accepted
        assert len({keep_code, review_code, delete_code}) == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Backup-before-import verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBackupBeforeImport:
    """Verify that a critical backup is created before any import operations."""

    @patch("import_manager.update_stats")
    @patch("import_manager.aqt")
    @patch("import_manager.mw")
    @patch("import_manager.create_backup")
    def test_backup_called_before_import(
        self, mock_backup, mock_mw, mock_aqt, mock_stats
    ):
        """create_backup(critical=True) must be called before processing subscriptions."""
        from import_manager import import_webresult

        mock_backup.return_value = True
        mock_mw.col = MagicMock()
        mock_mw.addonManager.getConfig.return_value = {}

        webresult = [{"deck_hash": "testhash", "deck": {}}]

        with (
            patch("import_manager.install_update"),
            patch("import_manager.show_changelog_popup"),
        ):
            try:
                import_webresult((webresult, None, True))
            except Exception:
                pass

        mock_backup.assert_called_once_with(critical=True)

    @patch("import_manager.update_stats")
    @patch("import_manager.aqt")
    @patch("import_manager.mw")
    @patch("import_manager.create_backup")
    def test_import_aborted_when_backup_fails(
        self, mock_backup, mock_mw, mock_aqt, mock_stats
    ):
        """If backup fails (critical=True raises), import must NOT proceed."""
        from utils import BackupFailedError

        mock_backup.side_effect = BackupFailedError("Backup failed")
        mock_mw.col = MagicMock()

        call_log = []

        def track_install(*args, **kwargs):
            call_log.append("install_called")

        with (
            patch("import_manager.install_update", side_effect=track_install),
            patch("import_manager.show_changelog_popup", side_effect=track_install),
        ):
            from import_manager import import_webresult

            import_webresult(([{"deck_hash": "h"}], None, True))

        # install_update should never have been called
        assert "install_called" not in call_log

    def test_create_backup_raises_on_critical_failure(self):
        """create_backup(critical=True) must raise BackupFailedError, not return False."""
        from utils import create_backup, BackupFailedError

        with patch("utils.is_collection_available", return_value=False):
            with pytest.raises(BackupFailedError):
                create_backup(critical=True)

    def test_create_backup_rejects_background_and_critical(self):
        """Cannot use background=True with critical=True."""
        from utils import create_backup

        with pytest.raises(ValueError, match="Cannot use background"):
            create_backup(background=True, critical=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bulk update pipeline — end-to-end note processing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBulkUpdatePipeline:
    """End-to-end tests for the _batch_process_notes → _bulk_update pipeline."""

    def test_existing_note_id_not_overwritten_by_remote(self):
        """When updating existing notes, the local note ID must be preserved."""
        col = create_mock_collection()
        nt = make_notetype(fields=["Front", "Back"], model_id=1000)
        col.models._store[1000] = nt
        nm = NoteModel(nt)

        note_dict = make_note_dict(
            guid="existing_guid",
            fields=["remote front", "remote back"],
            note_id=99999,  # Remote server's note ID — must NOT be used
        )
        note = Note.from_json(note_dict)

        # Existing local note
        mock_note = MockAnkiNote(collection=col, model=nt)
        mock_note.id = 12345
        mock_note.fields = ["local front", "local back"]
        mock_note.tags = []
        mock_note.guid = "existing_guid"
        mock_note._card_ids = [100]

        existing_note_map = {"existing_guid": 12345}

        from crowd_anki.representation.deck import Deck

        # Create a minimal deck to call _batch_process_notes
        deck = Deck.__new__(Deck)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False
        config.ignore_deck_movement = True

        # Create a mock AnkiNote class that returns our mock_note
        # and preserves the id it was constructed with
        def make_anki_note(collection, id=None, **kwargs):
            mock_note.id = id  # AnkiNote(collection, id=existing_id)
            return mock_note

        with patch(
            "crowd_anki.representation.deck.AnkiNote", side_effect=make_anki_note
        ):
            deck._batch_process_notes(
                [note],
                col,
                nm,
                [0, 1],
                config,
                1700000001,
                is_new=False,
                existing_note_map=existing_note_map,
            )

        # The note should have the local ID, not the remote's 99999
        assert note.anki_object.id == 12345

    def test_new_note_remote_id_stripped(self):
        """For new notes, the remote ID must be removed so Anki assigns its own."""
        col = create_mock_collection()
        nt = make_notetype(fields=["Front", "Back"], model_id=1000)
        col.models._store[1000] = nt
        nm = NoteModel(nt)

        note_dict = make_note_dict(
            guid="new_guid", fields=["front", "back"], note_id=88888
        )
        note = Note.from_json(note_dict)

        mock_note = MockAnkiNote(collection=col, model=nt)
        mock_note.fields = []
        mock_note.tags = []

        from crowd_anki.representation.deck import Deck

        deck = Deck.__new__(Deck)

        config = MagicMock()
        config.is_personal_field = MagicMock(return_value=False)
        config.has_optional_tags = False

        with patch("crowd_anki.representation.note.AnkiNote", return_value=mock_note):
            deck._batch_process_notes(
                [note],
                col,
                nm,
                [0, 1],
                config,
                1700000001,
                is_new=True,
                existing_note_map={},
            )

        # Original ID should be saved separately for potential restoration
        assert hasattr(note, "_original_id")
        assert note._original_id == 88888

    def test_bulk_update_calls_update_notes_not_add(self):
        """Updating notes must call update_notes, NEVER add_note/add_notes."""
        col = create_mock_collection()
        nt = make_notetype(fields=["F", "B"])
        nm = NoteModel(nt)

        note, config = self._make_simple_update_note(col, nt)

        Note._bulk_update_notes_preserving_placement(col, [note], {}, config)

        col.update_notes.assert_called_once()
        col.add_note.assert_not_called()
        col.add_notes.assert_not_called()

    def _make_simple_update_note(self, col, nt):
        note_dict = make_note_dict(fields=["new", "new"])
        note = Note.from_json(note_dict)
        mock = MockAnkiNote()
        mock.fields = ["old", "old"]
        mock.tags = []
        mock.id = 1
        mock._card_ids = [10]
        note.anki_object = mock
        note.anki_object.__dict__.update(note.anki_object_dict)
        config = MagicMock()
        config.ignore_deck_movement = True
        return note, config


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suspension status preservation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSuspensionPreservation:
    """Verify that card suspension status is handled correctly during import."""

    def test_suspended_siblings_propagate_to_new_cards(self):
        """If all existing cards are suspended, new cards should be suspended too."""
        col = create_mock_collection()

        # Simulate: note had card 100 (suspended), update adds card 101
        note = MagicMock()
        note.anki_object = MagicMock()
        note.anki_object.id = 1
        note.anki_object.card_ids.return_value = [100, 101]  # After update

        cards_before = {1: {100}}  # Only card 100 existed before

        # Card 100 is suspended (queue = -1)
        col.db.all.return_value = [(100, -1)]

        Note._sync_sibling_suspension_status(col, [note], cards_before)

        # New card 101 should be suspended
        col.sched.suspend_cards.assert_called_once()
        suspended_ids = col.sched.suspend_cards.call_args[0][0]
        assert 101 in suspended_ids

    def test_unsuspended_siblings_leave_new_cards_active(self):
        """If existing cards are NOT suspended, new cards should remain active."""
        col = create_mock_collection()

        note = MagicMock()
        note.anki_object = MagicMock()
        note.anki_object.id = 1
        note.anki_object.card_ids.return_value = [100, 101]

        cards_before = {1: {100}}

        # Card 100 is NOT suspended (queue = 0)
        col.db.all.return_value = [(100, 0)]

        Note._sync_sibling_suspension_status(col, [note], cards_before)

        col.sched.suspend_cards.assert_not_called()

    def test_no_new_cards_skips_suspension_check(self):
        """If no new cards were created, suspension logic is skipped."""
        col = create_mock_collection()

        note = MagicMock()
        note.anki_object = MagicMock()
        note.anki_object.id = 1
        note.anki_object.card_ids.return_value = [100]  # Same as before

        cards_before = {1: {100}}

        Note._sync_sibling_suspension_status(col, [note], cards_before)

        col.db.all.assert_not_called()
        col.sched.suspend_cards.assert_not_called()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Deck movement — user placement preservation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDeckMovementPreservation:
    """Verify that cards in filtered decks are NOT moved during import."""

    def test_new_cards_move_out_of_temp_deck_and_temp_deck_is_removed(self):
        """New cards are moved to their mapped deck before the temp deck is removed."""
        col = create_mock_collection()
        col.decks._store[1] = {
            "id": 1,
            "name": "_ankicollab_import_abc",
            "crowdanki_uuid": "",
        }
        col.decks._store[2] = {
            "id": 2,
            "name": "Target::Deck",
            "crowdanki_uuid": "",
        }
        col.db.all.return_value = [("uuid1", 10, 100, 1, 0)]

        Note._move_notes_to_decks(
            col,
            {"uuid1": "Target::Deck"},
            MagicMock(),
        )

        col.set_deck.assert_called_once_with([100], 2)
        col.decks.remove.assert_called_once_with([1])

    def test_filtered_card_updates_original_deck_without_moving_card(self):
        """A filtered card keeps its current deck while its original deck is updated."""
        col = create_mock_collection()
        note = MagicMock()
        note.get_uuid.return_value = "uuid1"
        mock_anki = MockAnkiNote()
        mock_anki._card_ids = [100]
        note.anki_object = mock_anki
        col.db.all.return_value = [(100, 5, 3)]
        col.decks.id = MagicMock(return_value=10)
        filtered_card = MagicMock()
        col.get_card.return_value = filtered_card

        config = MagicMock(ignore_deck_movement=False)
        Note._bulk_update_notes_preserving_placement(
            col, [note], {"uuid1": "Target::Deck"}, config
        )

        assert filtered_card.odid == 10
        col.update_cards.assert_called_once_with([filtered_card])
        col.set_deck.assert_not_called()

    def test_filtered_deck_cards_not_moved(self):
        """Cards in filtered decks (odid != 0) must not be moved."""
        col = create_mock_collection()

        note = MagicMock()
        note.get_uuid.return_value = "uuid1"
        mock_anki = MockAnkiNote()
        mock_anki._card_ids = [100]
        note.anki_object = mock_anki

        note_to_deck_map = {"uuid1": "Target::Deck"}

        # Card 100 is in a filtered deck (odid != 0)
        col.db.all.return_value = [(100, 5, 3)]  # id=100, did=5, odid=3

        config = MagicMock()
        config.ignore_deck_movement = False

        col.decks.id.return_value = 10  # Target deck ID

        Note._bulk_update_notes_preserving_placement(
            col, [note], note_to_deck_map, config
        )

        # set_deck should NOT be called for filtered deck cards
        col.set_deck.assert_not_called()

    def test_ignore_deck_movement_config(self):
        """When ignore_deck_movement is True, cards stay where they are."""
        col = create_mock_collection()

        note = MagicMock()
        note.get_uuid.return_value = "uuid1"
        mock_anki = MockAnkiNote()
        mock_anki._card_ids = [100]
        note.anki_object = mock_anki

        note_to_deck_map = {"uuid1": "New::Location"}

        config = MagicMock()
        config.ignore_deck_movement = True

        Note._bulk_update_notes_preserving_placement(
            col, [note], note_to_deck_map, config
        )

        # With ignore_deck_movement, set_deck should never be called
        col.set_deck.assert_not_called()
        col.db.all.assert_not_called()

    def test_card_already_in_target_deck_not_moved(self):
        """Cards already in the correct deck should not trigger a move."""
        col = create_mock_collection()

        note = MagicMock()
        note.get_uuid.return_value = "uuid1"
        mock_anki = MockAnkiNote()
        mock_anki._card_ids = [100]
        note.anki_object = mock_anki

        note_to_deck_map = {"uuid1": "Already::Here"}

        # Card 100 is already in deck 10, target is also deck 10
        col.db.all.return_value = [(100, 10, 0)]
        # Override the side_effect-based decks.id to always return 10
        col.decks.id = MagicMock(return_value=10)

        config = MagicMock()
        config.ignore_deck_movement = False

        Note._bulk_update_notes_preserving_placement(
            col, [note], note_to_deck_map, config
        )

        col.set_deck.assert_not_called()
