"""Import workflow tests (Phase 3.2).

Runs the real import orchestration core — ``Deck._bulk_process_all_notes``
(what ``_install_deck_op``/``save_decks_and_notes_bulk`` call) — against a
realistic in-memory collection, and asserts the **final collection state**:
which notes exist, their fields/tags/guids, idempotence on re-import, and
conflict behavior.

The note-insertion boundary (``Note.bulk_add_notes`` /
``_bulk_update_notes_preserving_placement``) is replaced with a real writer
into the collection store — everything upstream (``from_json`` deserialization,
note classification, field mapping, deck/temp-deck lifecycle) is the real code.
"""

import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.integration

import utils as utils_mod
from crowd_anki.representation import deck_initializer
from crowd_anki.representation.deck import Deck
from crowd_anki.representation.note import Note
from tests.conftest import make_notetype, make_note_dict, make_deck_json


class TestImportWorkflow:
    def _make_deck(self, notes, note_models):
        deck_json = make_deck_json(
            name="Imported", notes=notes, note_models=note_models
        )
        return deck_initializer.from_json(deck_json)

    def _collect(self, deck, config):
        all_notes = []
        note_to_deck_map = {}
        deck._collect_all_notes(all_notes, note_to_deck_map, "", config.home_deck)
        return all_notes, note_to_deck_map

    def _store_added(self, collection, notes, deck_id, import_config):
        for note in notes:
            obj = note.anki_object
            data = obj.__dict__ if obj is not None else (note.anki_object_dict or {})
            collection.add_note_record(
                guid=note.get_uuid(),
                mid=obj.mid if obj is not None else data.get("mid", 0),
                fields=list(data.get("fields", [])),
                tags=list(data.get("tags", [])),
                did=deck_id,
            )

    def _store_updated(self, collection, update_notes, note_to_deck_map, import_config):
        for note in update_notes:
            obj = note.anki_object
            nid = obj.id if obj is not None else None
            if obj is not None and nid in collection._notes:
                data = obj.__dict__
                collection._notes[nid]["fields"] = list(data.get("fields", []))
                collection._notes[nid]["tags"] = list(data.get("tags", []))

    def _run_import(self, col, deck, config, mw_mock):
        all_notes, note_to_deck_map = self._collect(deck, config)
        progress = MagicMock()
        deck.mw_like = mw_mock
        with (
            patch("crowd_anki.representation.deck.mw", mw_mock),
            patch.object(utils_mod, "mw", MagicMock(col=col)),
            patch.object(Note, "bulk_add_notes", side_effect=self._store_added),
            patch.object(
                Note,
                "_bulk_update_notes_preserving_placement",
                side_effect=self._store_updated,
            ),
        ):
            result = deck._bulk_process_all_notes(
                col, all_notes, config, progress, note_to_deck_map
            )
        return result

    def test_empty_collection_full_import(
        self, realistic_collection, mw_mock, fake_anki_note_class
    ):
        col = realistic_collection
        nt = make_notetype(name="Basic", fields=["Front", "Back"], model_uuid="m1")
        note = make_note_dict(
            guid="g1", fields=["Front 1", "Back 1"], tags=["tag1"], note_model_uuid="m1"
        )
        deck = self._make_deck([note], [nt])

        self._run_import(
            col,
            deck,
            MagicMock(
                home_deck=None,
                new_notes_home_deck=None,
                keep_empty_subdecks=False,
                suspend_new_cards=False,
                ignore_deck_movement=True,
                deck_hash="h",
                is_personal_field=MagicMock(return_value=False),
            ),
            mw_mock,
        )

        records = col.note_records()
        assert len(records) == 1
        assert records[0]["guid"] == "g1"
        assert records[0]["fields"] == ["Front 1", "Back 1"]
        assert records[0]["tags"] == ["tag1"]

    def test_reimport_identical_is_idempotent(
        self, realistic_collection, mw_mock, fake_anki_note_class
    ):
        col = realistic_collection
        nt = make_notetype(name="Basic", fields=["Front", "Back"], model_uuid="m1")
        note = make_note_dict(
            guid="g1", fields=["Front 1", "Back 1"], tags=["tag1"], note_model_uuid="m1"
        )
        deck = self._make_deck([note], [nt])
        config = MagicMock(
            home_deck=None,
            new_notes_home_deck=None,
            keep_empty_subdecks=False,
            suspend_new_cards=False,
            ignore_deck_movement=True,
            deck_hash="h",
            is_personal_field=MagicMock(return_value=False),
        )

        # First import inserts the note into the collection store
        self._store_added(col, deck.notes, 1, config)
        # Second import runs the full pipeline: the note now exists → update path
        with (
            patch.object(
                Note, "bulk_add_notes", side_effect=self._store_added
            ) as add_mock,
            patch.object(
                Note,
                "_bulk_update_notes_preserving_placement",
                side_effect=self._store_updated,
            ) as upd_mock,
            patch.object(utils_mod, "mw", MagicMock(col=col)),
            patch("crowd_anki.representation.deck.mw", mw_mock),
        ):
            deck2 = self._make_deck([note], [nt])
            all_notes, note_to_deck_map = self._collect(deck2, config)
            deck2._bulk_process_all_notes(
                col, all_notes, config, MagicMock(), note_to_deck_map
            )

        # No duplicate notes: still exactly one record
        assert len(col.note_records()) == 1
        assert col.note_records()[0]["guid"] == "g1"
        # The identical note was routed to the update path, not re-added
        assert add_mock.call_count == 0

    def test_new_remote_notes_only_inserted(
        self, realistic_collection, mw_mock, fake_anki_note_class
    ):
        """When some notes already exist locally, only genuinely new remote
        notes are inserted."""
        col = realistic_collection
        nt = make_notetype(name="Basic", fields=["Front", "Back"], model_uuid="m1")
        existing = make_note_dict(
            guid="existing", fields=["Old", "Data"], tags=[], note_model_uuid="m1"
        )
        new_remote = make_note_dict(
            guid="brand_new", fields=["New", "Note"], tags=["x"], note_model_uuid="m1"
        )
        # Local collection already has the "existing" note
        col.add_note_record(
            guid="existing", mid=1, fields=["Old", "Data"], tags=[], did=1
        )

        deck = self._make_deck([existing, new_remote], [nt])
        config = MagicMock(
            home_deck=None,
            new_notes_home_deck=None,
            keep_empty_subdecks=False,
            suspend_new_cards=False,
            ignore_deck_movement=True,
            deck_hash="h",
            is_personal_field=MagicMock(return_value=False),
        )

        with (
            patch.object(
                Note, "bulk_add_notes", side_effect=self._store_added
            ) as add_mock,
            patch.object(
                Note,
                "_bulk_update_notes_preserving_placement",
                side_effect=self._store_updated,
            ),
            patch.object(utils_mod, "mw", MagicMock(col=col)),
            patch("crowd_anki.representation.deck.mw", mw_mock),
        ):
            all_notes, note_to_deck_map = self._collect(deck, config)
            deck._bulk_process_all_notes(
                col, all_notes, config, MagicMock(), note_to_deck_map
            )

        guids = {r["guid"] for r in col.note_records()}
        assert "brand_new" in guids  # new note inserted
        assert "existing" in guids
        # Only the genuinely new note went through bulk_add_notes
        assert add_mock.call_count == 1


class TestNotetypeSchemaChanges:
    """Notetype schema changes (field added/removed/reordered, template and
    CSS changed) must map note fields to the final notetype correctly."""

    def _apply_to_note(self, nt_dict, note):
        """Run a note through the real import with a given target notetype."""
        from crowd_anki.representation.note_model import NoteModel

        note_model = NoteModel(nt_dict)
        config = MagicMock(is_personal_field=MagicMock(return_value=False))
        note.handle_import_config_changes(config, note_model)
        return note.anki_object_dict

    def test_field_added_preserves_existing_values(self):
        old_fields = ["Front", "Back"]
        nt = make_notetype(
            name="Basic", fields=["Front", "Back", "Extra"], model_uuid="m1"
        )
        note = Note.from_json(make_note_dict(fields=old_fields, note_model_uuid="m1"))
        note.anki_object = MagicMock(fields=["Front", "Back", ""])
        note.anki_object.note_type.return_value = nt

        self._apply_to_note(nt, note)
        assert note.anki_object_dict["fields"] == ["Front", "Back", ""]

    def test_protected_field_preserves_local_content(self):
        """A maintainer-protected field must keep the user's LOCAL content,
        not be overwritten by the remote value — even when fields are mapped."""
        nt = make_notetype(name="Basic", fields=["Front", "Back"], model_uuid="m1")
        note = Note.from_json(
            make_note_dict(fields=["remote front", "remote back"], note_model_uuid="m1")
        )
        note.anki_object = MagicMock(fields=["local front", "local back"])
        note.anki_object.note_type.return_value = nt

        config = MagicMock()
        config.is_personal_field = MagicMock(
            side_effect=lambda model_name, field_name: field_name == "Front"
        )
        from crowd_anki.representation.note_model import NoteModel

        note.handle_import_config_changes(config, NoteModel(nt))
        # "Front" (index 0) is protected → local value preserved
        assert note.anki_object_dict["fields"][0] == "local front"
        # "Back" is not protected → remote value kept
        assert note.anki_object_dict["fields"][1] == "remote back"

    def test_template_and_css_change_survive_roundtrip(self):
        nt = make_notetype(
            name="Basic",
            fields=["Front", "Back"],
            templates=[
                {
                    "name": "Renamed Card",
                    "qfmt": "{{Front}}!!",
                    "afmt": "{{Back}}",
                    "ord": 0,
                }
            ],
            css=".card { color: green; }",
            model_uuid="m1",
        )
        deck_json = make_deck_json(name="D", note_models=[nt])
        deck = deck_initializer.from_json(deck_json)
        model = deck.metadata.models["m1"]
        assert model.anki_dict["css"] == ".card { color: green; }"
        assert model.anki_dict["tmpls"][0]["name"] == "Renamed Card"
        assert model.anki_dict["tmpls"][0]["qfmt"] == "{{Front}}!!"
