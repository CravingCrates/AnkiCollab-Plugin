"""Mutation-driven tests for the notetype schema-conflict handling in deck.py.

These exercise the externally-supplied-data boundary: server notetypes that
must be merged into (or renamed out of) the user's local collection.

- ``_handle_notetype_duplicates``: same-name merge vs incompatible rename,
  UUID preservation, missing-UUID skipping, no-metadata short-circuit.
- ``_create_and_update_notetypes``: new-model creation, existing-model update,
  field-mapping capture, UUID auto-assignment, ID fallback.
- ``_validate_notetype_operations``: missing / wrong-UUID validation failures.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import create_mock_collection, make_notetype

from crowd_anki.representation import deck as deck_module
from crowd_anki.representation.deck import Deck, DeckMetadata
from crowd_anki.representation.note_model import NoteModel
from crowd_anki.utils.constants import UUID_FIELD_NAME


def _patch_logger(method="info"):
    return patch.object(deck_module.logger, method)


def _deck_with_models(models):
    deck = Deck(MagicMock(), {"name": "Root", "crowdanki_uuid": "d1", "id": 1})
    deck.metadata = DeckMetadata(models=models, deck_configs={})
    return deck


def _model(
    name="Basic", fields=None, model_id=1000, model_uuid="uuid-1", css=".card { }"
):
    return NoteModel(
        make_notetype(
            name=name,
            fields=fields or ["A", "B"],
            model_id=model_id,
            model_uuid=model_uuid,
            css=css,
        )
    )


# ──────────────────────────────────────────────────────────────────────
# _handle_notetype_duplicates
# ──────────────────────────────────────────────────────────────────────


class TestHandleNotetypeDuplicates:
    def test_no_metadata_returns_true(self):
        col = create_mock_collection()
        deck = Deck(MagicMock(), {"name": "R", "crowdanki_uuid": "d1", "id": 1})
        deck.metadata = None
        with _patch_logger("warning") as mock_warn:
            assert deck._handle_notetype_duplicates(col, []) is True
        assert mock_warn.called

    def test_model_without_uuid_skipped(self):
        col = create_mock_collection()
        model = _model()
        model.anki_dict[UUID_FIELD_NAME] = ""
        deck = _deck_with_models({"m": model})
        with _patch_logger("warning") as mock_warn:
            assert deck._handle_notetype_duplicates(col, []) is True
        assert mock_warn.called
        col.models.update_dict.assert_not_called()

    def test_no_local_conflict_untouched(self):
        col = create_mock_collection()
        model = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": model})
        assert deck._handle_notetype_duplicates(col, []) is True
        # No compatible/incompatible local model -> no rename, no update_dict.
        assert model.anki_dict["id"] == 100
        assert "original_name" not in model.anki_dict
        col.models.update_dict.assert_not_called()

    def test_compatible_local_model_merged(self):
        col = create_mock_collection()
        # Local notetype with the same name + structure (compatible).
        local = make_notetype(
            name="Basic", fields=["A", "B"], model_id=50, model_uuid="local-uuid"
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True
        # Remote model now points at the local id and keeps its UUID.
        assert remote.anki_dict["id"] == 50
        # The local model is stamped with the remote UUID for tracking.
        stored = col.models.get(50)
        assert stored[UUID_FIELD_NAME] == "remote-uuid"
        col.models.update_dict.assert_called()

    def test_incompatible_local_model_renamed(self):
        col = create_mock_collection()
        # Local notetype with same name but different fields -> incompatible.
        local = make_notetype(
            name="Basic", fields=["X", "Y"], model_id=50, model_uuid="local-uuid"
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        with _patch_logger("warning") as mock_warn:
            assert deck._handle_notetype_duplicates(col, []) is True
        # Remote renamed to avoid collision; original name preserved.
        assert remote.anki_dict["name"] == "Basic (AnkiCollab)"
        assert remote.anki_dict["original_name"] == "Basic"
        assert mock_warn.called

    def test_exception_returns_false_and_records(self):
        col = create_mock_collection()
        deck = _deck_with_models({"m": _model()})
        # Force an exception inside the loop via a failing collection read.
        col.models.all.side_effect = RuntimeError("boom")
        failed = []
        with _patch_logger("error"):
            result = deck._handle_notetype_duplicates(col, failed)
        assert result is False
        assert any("Duplicate handling" in f for f in failed)


# ──────────────────────────────────────────────────────────────────────
# _create_and_update_notetypes
# ──────────────────────────────────────────────────────────────────────


class TestCreateAndUpdateNotetypes:
    def test_no_metadata_returns_true(self):
        col = create_mock_collection()
        deck = Deck(MagicMock(), {"name": "R", "crowdanki_uuid": "d1", "id": 1})
        deck.metadata = None
        assert deck._create_and_update_notetypes(col, []) is True

    def test_collection_none_returns_false(self):
        deck = _deck_with_models({"m": _model()})
        failed = []
        assert deck._create_and_update_notetypes(None, failed) is False
        assert any("Collection is None" in f for f in failed)

    def test_new_notetype_created(self):
        col = create_mock_collection()
        model = _model(name="Fresh", model_uuid="fresh-uuid", model_id=0)
        deck = _deck_with_models({"m": model})
        assert deck._create_and_update_notetypes(col, []) is True
        # The model must have been added to the collection.
        assert len(col.models.all()) >= 1
        # Its id must be resolved (not 0).
        assert model.anki_dict["id"] != 0

    def test_uuid_auto_assigned_when_missing(self):
        col = create_mock_collection()
        model = _model(name="NoUuid", model_uuid="")
        model.anki_dict.pop(UUID_FIELD_NAME, None)
        deck = _deck_with_models({"m": model})
        assert deck._create_and_update_notetypes(col, []) is True
        assert model.get_uuid()

    def test_existing_notetype_updated_and_mapping_captured(self):
        col = create_mock_collection()
        # Pre-existing local model found by UUID -> update path.
        existing = make_notetype(
            name="Basic", fields=["A", "B"], model_id=70, model_uuid="existing-uuid"
        )
        col.models.add(existing)
        model = _model(
            name="Basic",
            fields=["A", "B", "C"],
            model_uuid="existing-uuid",
            model_id=70,
        )
        deck = _deck_with_models({"m": model})
        assert deck._create_and_update_notetypes(col, []) is True
        # Field mapping captured for the notetype.
        assert "existing-uuid" in deck._field_mappings

    def test_id_fallback_fetches_by_uuid(self):
        col = create_mock_collection()
        # save_to_collection returns None new_notetype_dict when no changes,
        # so the id fallback path is exercised via get_model.
        existing = make_notetype(
            name="Basic", fields=["A", "B"], model_id=80, model_uuid="fb-uuid"
        )
        col.models.add(existing)
        model = _model(
            name="Basic", fields=["A", "B"], model_uuid="fb-uuid", model_id=80
        )
        deck = _deck_with_models({"m": model})
        assert deck._create_and_update_notetypes(col, []) is True
        assert model.anki_dict["id"] == 80


# ──────────────────────────────────────────────────────────────────────
# _validate_notetype_operations
# ──────────────────────────────────────────────────────────────────────


class TestValidateNotetypeOperations:
    def test_no_metadata_returns_true(self):
        col = create_mock_collection()
        deck = Deck(MagicMock(), {"name": "R", "crowdanki_uuid": "d1", "id": 1})
        deck.metadata = None
        assert deck._validate_notetype_operations(col, []) is True

    def test_all_models_found_with_matching_uuid(self):
        col = create_mock_collection()
        col.models.add(
            make_notetype(
                name="Basic", fields=["A", "B"], model_id=1, model_uuid="v-uuid"
            )
        )
        model = _model(name="Basic", model_uuid="v-uuid", model_id=1)
        deck = _deck_with_models({"m": model})
        failed = []
        assert deck._validate_notetype_operations(col, failed) is True
        assert failed == []

    def test_missing_model_fails_validation(self):
        col = create_mock_collection()  # empty model store
        model = _model(name="Basic", model_uuid="v-uuid", model_id=1)
        deck = _deck_with_models({"m": model})
        failed = []
        with _patch_logger("error"):
            assert deck._validate_notetype_operations(col, failed) is False
        assert any("Missing notetypes" in f for f in failed)

    def test_model_found_but_local_missing_uuid_reported_missing(self):
        """A local model without a UUID cannot be matched by UUID lookup, so it
        is reported as missing (the UUID-validation branch is unreachable via
        ``get_model``, which matches on the UUID field itself)."""
        col = create_mock_collection()
        nt = make_notetype(
            name="Basic", fields=["A", "B"], model_id=1, model_uuid="v-uuid"
        )
        nt.pop(UUID_FIELD_NAME, None)
        col.models.add(nt)
        model = _model(name="Basic", model_uuid="v-uuid", model_id=1)
        deck = _deck_with_models({"m": model})
        failed = []
        with _patch_logger("error"):
            assert deck._validate_notetype_operations(col, failed) is False
        assert any("Missing notetypes" in f for f in failed)

    def test_uuid_mismatch_fails_validation(self):
        col = create_mock_collection()
        model = _model(name="Basic", model_uuid="expected-uuid", model_id=1)
        deck = _deck_with_models({"m": model})
        failed = []

        with (
            patch.object(
                deck_module.UuidFetcher,
                "get_model",
                return_value={"id": 1, UUID_FIELD_NAME: "different-uuid"},
            ),
            _patch_logger("error"),
        ):
            assert deck._validate_notetype_operations(col, failed) is False

        assert any("UUID validation failures" in item for item in failed)


# ──────────────────────────────────────────────────────────────────────
# Existing-note notetype migration
# ──────────────────────────────────────────────────────────────────────


class TestExistingNoteNotetypeMigration:
    def test_migration_groups_note_ids_by_old_and_new_notetype(self):
        col = create_mock_collection()
        old = make_notetype(
            name="Old", fields=["A", "B"], model_id=10, model_uuid="old-uuid"
        )
        target = make_notetype(
            name="Target", fields=["A", "B"], model_id=20, model_uuid="target-uuid"
        )
        col.models.add(old)
        col.models.add(target)
        col.db.all.return_value = [
            (101, 10, "note-a"),
            (102, 10, "note-b"),
        ]

        note_a = MagicMock(get_uuid=MagicMock(return_value="note-a"))
        note_a.note_model_uuid = "target-uuid"
        note_b = MagicMock(get_uuid=MagicMock(return_value="note-b"))
        note_b.note_model_uuid = "target-uuid"
        deck = _deck_with_models(
            {"target": NoteModel(target)},
        )
        deck.notes = [note_a, note_b]

        with patch.object(
            deck,
            "_apply_notetype_change_optimized",
            return_value=True,
        ) as apply_change:
            assert deck._change_existing_note_types(col, []) is True

        apply_change.assert_called_once_with(col, 10, 20, [101, 102])

    def test_apply_notetype_change_preserves_captured_field_mapping(self):
        col = create_mock_collection()
        old = make_notetype(
            name="Old", fields=["Front", "Back"], model_id=10, model_uuid="old-uuid"
        )
        target = make_notetype(
            name="Target",
            fields=["Back", "Front"],
            model_id=20,
            model_uuid="target-uuid",
        )
        col.models.add(old)
        col.models.add(target)
        col.db.scalar.return_value = 987
        deck = _deck_with_models({"target": NoteModel(target)})
        deck._field_mappings["target-uuid"] = [1, 0]

        with (
            patch.object(deck_module, "NotetypeId", side_effect=lambda value: value),
            patch.object(deck_module, "ChangeNotetypeRequest") as request_type,
        ):
            assert deck._apply_notetype_change(col, 10, 20, [101]) is True

        request_type.assert_called_once_with(
            note_ids=[101],
            old_notetype_id=10,
            new_notetype_id=20,
            current_schema=987,
            new_fields=[1, 0],
        )
        col.models.change_notetype_of_notes.assert_called_once_with(
            request_type.return_value
        )
