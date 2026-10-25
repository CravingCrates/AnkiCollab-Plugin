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
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import create_mock_collection, make_notetype

from crowd_anki.representation import deck as deck_module
from crowd_anki.representation.deck import Deck, DeckMetadata
from crowd_anki.representation.note_model import NoteModel, NotetypeSaveError
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

    def test_model_with_empty_uuid_is_repaired_instead_of_skipped(self):
        col = create_mock_collection()
        model = _model()
        model.anki_dict[UUID_FIELD_NAME] = ""
        deck = _deck_with_models({"m": model})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert model.anki_dict[UUID_FIELD_NAME]
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

    def test_similar_prefix_name_is_not_a_duplicate(self):
        col = create_mock_collection()
        local = make_notetype(
            name="BasicExtra", fields=["A", "B"], model_id=50, model_uuid="local-uuid"
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 100
        assert "original_name" not in remote.anki_dict
        col.models.update_dict.assert_not_called()

    def test_anki_duplicate_suffix_is_considered_a_duplicate(self):
        col = create_mock_collection()
        local = make_notetype(
            name="Basic+", fields=["A", "B"], model_id=50, model_uuid="local-uuid"
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 50
        assert col.models.get(50)[UUID_FIELD_NAME] == "remote-uuid"
        col.models.update_dict.assert_called()

    def test_anki_random_suffix_is_considered_a_duplicate(self):
        col = create_mock_collection()
        local = make_notetype(
            name="Basic-908e4",
            fields=["A", "B"],
            model_id=50,
            model_uuid="local-uuid",
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 50
        assert col.models.get(50)[UUID_FIELD_NAME] == "remote-uuid"

    def test_anki_random_suffix_with_collision_plus_is_considered_a_duplicate(self):
        col = create_mock_collection()
        local = make_notetype(
            name="Basic-908e4+",
            fields=["A", "B"],
            model_id=50,
            model_uuid="local-uuid",
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 50
        assert col.models.get(50)[UUID_FIELD_NAME] == "remote-uuid"

    def test_case_variant_name_is_considered_a_duplicate(self):
        """Anki makes notetype names unique case-insensitively."""
        col = create_mock_collection()
        local = make_notetype(
            name="basic", fields=["A", "B"], model_id=50, model_uuid="local-uuid"
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 50
        assert col.models.get(50)[UUID_FIELD_NAME] == "remote-uuid"

    def test_plugin_renamed_incompatible_type_is_not_considered_a_duplicate(self):
        col = create_mock_collection()
        local = make_notetype(
            name="Basic (AnkiCollab)",
            fields=["A", "B"],
            model_id=50,
            model_uuid="local-uuid",
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 100
        col.models.update_dict.assert_not_called()

    def test_unrecognized_suffix_is_not_considered_a_duplicate(self):
        col = create_mock_collection()
        local = make_notetype(
            name="Basic-custom",
            fields=["A", "B"],
            model_id=50,
            model_uuid="local-uuid",
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 100
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

    def test_existing_uuid_target_is_not_replaced_by_a_same_name_copy(self):
        """A local copy must never steal the UUID of the canonical notetype.

        Both "Basic-908e4" and "Basic" are compatible and share the base name.
        The one carrying the remote UUID is the target; adopting the copy would
        leave two local notetypes with the same UUID and could update the wrong
        one on a later import.
        """
        col = create_mock_collection()
        # Insertion order matters: the copy is seen first by the scan.
        copy = make_notetype(
            name="Basic-908e4",
            fields=["A", "B"],
            model_id=51,
            model_uuid="copy-uuid",
        )
        canonical = make_notetype(
            name="Basic",
            fields=["A", "B"],
            model_id=50,
            model_uuid="remote-uuid",
        )
        col.models.add(copy)
        col.models.add(canonical)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 100  # untouched; save resolves by UUID
        assert col.models.get(51)[UUID_FIELD_NAME] == "copy-uuid"
        assert col.models.get(50)[UUID_FIELD_NAME] == "remote-uuid"
        col.models.update_dict.assert_not_called()

    def test_existing_uuid_with_taken_server_name_keeps_the_local_name(self):
        """Re-importing an incompatible type must not rename it on every run.

        First import renamed it to "Basic (AnkiCollab)" because the user's
        "Basic" existed. On the next import the UUID points at that local type;
        forcing the server name back would collide and Anki would append a
        random suffix.
        """
        col = create_mock_collection()
        local_other = make_notetype(
            name="Basic", fields=["X", "Y"], model_id=50, model_uuid="other-uuid"
        )
        local_target = make_notetype(
            name="Basic (AnkiCollab)",
            fields=["A", "B"],
            model_id=51,
            model_uuid="remote-uuid",
        )
        col.models.add(local_other)
        col.models.add(local_target)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 100
        assert remote.anki_dict["name"] == "Basic (AnkiCollab)"
        assert remote.anki_dict["original_name"] == "Basic"

    def test_existing_uuid_with_free_server_name_keeps_the_server_name(self):
        col = create_mock_collection()
        local = make_notetype(
            name="Old Local Name",
            fields=["A", "B"],
            model_id=51,
            model_uuid="remote-uuid",
        )
        col.models.add(local)
        remote = _model(name="Basic", model_uuid="remote-uuid", model_id=100)
        deck = _deck_with_models({"m": remote})

        assert deck._handle_notetype_duplicates(col, []) is True

        assert remote.anki_dict["id"] == 100
        assert remote.anki_dict["name"] == "Basic"
        assert "original_name" not in remote.anki_dict

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
    def test_missing_saved_model_id_is_fatal(self):
        col = create_mock_collection()
        model = _model(model_id=None)
        model.save_to_collection = MagicMock(return_value=(None, None))
        deck = _deck_with_models({"m": model})
        model.anki_dict.pop("id", None)
        failures = []

        with patch.object(deck_module.UuidFetcher, "get_model", return_value=None):
            with pytest.raises(NotetypeSaveError):
                deck._create_and_update_notetypes(col, failures)

    def test_a_save_failure_aborts_instead_of_continuing(self):
        """Without a field mapping the import must not continue."""
        col = create_mock_collection()
        model = _model()
        model.save_to_collection = MagicMock(
            side_effect=NotetypeSaveError("mapping lost")
        )
        deck = _deck_with_models({"m": model})

        with pytest.raises(NotetypeSaveError):
            deck._create_and_update_notetypes(col, [])

    def test_no_metadata_returns_true(self):
        col = create_mock_collection()
        deck = Deck(MagicMock(), {"name": "R", "crowdanki_uuid": "d1", "id": 1})
        deck.metadata = None
        assert deck._create_and_update_notetypes(col, []) is True

    def test_collection_none_returns_false(self):
        deck = _deck_with_models({"m": _model()})
        failed = []
        assert deck._create_and_update_notetypes(cast(Any, None), failed) is False
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

    def test_empty_uuid_is_replaced_too(self):
        col = create_mock_collection()
        model = _model(name="EmptyUuid", model_uuid="")
        assert model.anki_dict[UUID_FIELD_NAME] == ""
        deck = _deck_with_models({"m": model})

        assert deck._create_and_update_notetypes(col, []) is True

        assert model.get_uuid()
        assert model.anki_dict[UUID_FIELD_NAME] != ""

    def test_existing_notetype_is_updated_in_place(self):
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
        # The local notetype now carries the remote fields and the local-only
        # fields are known to the model used for note processing.
        assert [f["name"] for f in model.anki_dict["flds"]] == ["A", "B", "C"]
        assert model.local_only_field_names == []

    def test_id_fallback_fetches_by_uuid_when_the_save_reports_no_change(self):
        col = create_mock_collection()
        existing = make_notetype(
            name="Basic", fields=["A", "B"], model_id=80, model_uuid="fb-uuid"
        )
        col.models.add(existing)
        model = _model(
            name="Basic", fields=["A", "B"], model_uuid="fb-uuid", model_id=80
        )
        # Simulate a save path that cannot report the local id itself.
        model.anki_dict.pop("id", None)
        model.save_to_collection = MagicMock(return_value=(None, [0, 1]))
        deck = _deck_with_models({"m": model})

        with patch.object(
            deck_module.UuidFetcher, "get_model", return_value={"id": 80}
        ) as get_model:
            assert deck._create_and_update_notetypes(col, []) is True

        assert model.anki_dict["id"] == 80
        get_model.assert_called_with("fb-uuid")

    def test_missing_id_after_save_is_fatal(self):
        col = create_mock_collection()
        model = _model(name="Basic", model_uuid="no-id-uuid", model_id=0)
        model.anki_dict.pop("id", None)
        model.save_to_collection = MagicMock(return_value=(None, [0, 1]))
        deck = _deck_with_models({"m": model})
        failures = []

        with patch.object(deck_module.UuidFetcher, "get_model", return_value=None):
            with pytest.raises(NotetypeSaveError):
                deck._create_and_update_notetypes(col, failures)

        assert any("valid ID" in item for item in failures)

    def test_the_payload_id_never_reaches_note_processing(self):
        """No-change path: the sender's id must be replaced by the local id."""
        col = create_mock_collection()
        existing = make_notetype(
            name="Basic", fields=["A", "B"], model_id=70, model_uuid="same-uuid"
        )
        col.models.add(existing)
        model = _model(
            name="Basic", fields=["A", "B"], model_uuid="same-uuid", model_id=999_999
        )
        deck = _deck_with_models({"m": model})

        assert deck._create_and_update_notetypes(col, []) is True
        assert model.anki_dict["id"] == 70


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


class TestSaveMetadataMutationOrdering:
    def test_save_metadata_saves_configs_and_deck_after_notetypes(self):
        deck = _deck_with_models({})
        config = MagicMock()
        deck.metadata = DeckMetadata(deck_configs={"cfg": config}, models={})
        collection = create_mock_collection()
        events = []

        deck.handle_notetype_changes = MagicMock(
            side_effect=lambda col: events.append("notetypes") or True
        )
        config.save_to_collection.side_effect = lambda col: events.append("config")
        deck._save_deck = MagicMock(
            side_effect=lambda col, parent, home, name: events.append("deck")
        )

        deck.save_metadata(collection, home_deck="Home")

        assert events == ["notetypes", "config", "deck"]
        deck._save_deck.assert_called_once_with(collection, "", "Home", "Root")

    def test_save_metadata_aborts_when_notetype_changes_fail(self):
        """A partially migrated schema must not receive imported note values."""
        deck = _deck_with_models({})
        collection = create_mock_collection()
        deck.handle_notetype_changes = MagicMock(return_value=False)
        deck._save_deck = MagicMock()

        with pytest.raises(NotetypeSaveError):
            deck.save_metadata(collection, home_deck="Home")

        deck._save_deck.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Existing-note notetype migration
# ──────────────────────────────────────────────────────────────────────


class TestExistingNoteNotetypeMigration:
    def test_related_migration_keeps_the_old_notetypes_local_fields(self):
        """A "Foo" -> "Foo (AnkiCollab)" migration must not drop custom fields."""
        col = create_mock_collection()
        old = make_notetype(
            name="Foo",
            fields=["A", "B", "LocalOnly"],
            model_id=10,
            model_uuid="old-uuid",
        )
        target = make_notetype(
            name="Foo (AnkiCollab)",
            fields=["A", "B"],
            model_id=20,
            model_uuid="target-uuid",
        )
        col.models.add(old)
        col.models.add(target)
        col.db.all.return_value = [(101, 10, "note-a")]
        note = MagicMock(get_uuid=MagicMock(return_value="note-a"))
        note.note_model_uuid = "target-uuid"
        target_model = NoteModel(target)
        deck = _deck_with_models({"target": target_model})
        deck.notes = [note]

        with patch.object(deck, "_apply_notetype_change_optimized", return_value=True):
            assert deck._change_existing_note_types(col, []) is True

        stored = col.models.get(20)
        assert [field["name"] for field in stored["flds"]] == [
            "A",
            "B",
            "LocalOnly",
        ]
        assert [field["name"] for field in target_model.anki_dict["flds"]] == [
            "A",
            "B",
            "LocalOnly",
        ]
        assert target_model.local_only_field_names == ["LocalOnly"]

    def test_unrelated_migration_does_not_pollute_the_target_notetype(self):
        """A manual note-type switch must not copy unrelated fields over."""
        col = create_mock_collection()
        old = make_notetype(
            name="Cloze",
            fields=["Text", "Extra"],
            model_id=10,
            model_uuid="old-uuid",
        )
        target = make_notetype(
            name="Basic",
            fields=["Front", "Back"],
            model_id=20,
            model_uuid="target-uuid",
        )
        col.models.add(old)
        col.models.add(target)
        col.db.all.return_value = [(101, 10, "note-a")]
        note = MagicMock(get_uuid=MagicMock(return_value="note-a"))
        note.note_model_uuid = "target-uuid"
        deck = _deck_with_models({"target": NoteModel(target)})
        deck.notes = [note]

        with patch.object(deck, "_apply_notetype_change_optimized", return_value=True):
            assert deck._change_existing_note_types(col, []) is True

        stored = col.models.get(20)
        assert [field["name"] for field in stored["flds"]] == ["Front", "Back"]

    def test_batch_query_failure_aborts_migration(self):
        col = create_mock_collection()
        old = make_notetype(
            name="Old", fields=["A", "B"], model_id=10, model_uuid="old-uuid"
        )
        col.models.add(old)
        col.db.all.side_effect = RuntimeError("database is locked")
        note = MagicMock(get_uuid=MagicMock(return_value="note-a"))
        note.note_model_uuid = "target-uuid"
        deck = _deck_with_models({})
        deck.notes = [note]
        failures = []

        with _patch_logger("warning"):
            assert deck._change_existing_note_types(col, failures) is False

        assert any("Note batch" in item for item in failures)

    def test_missing_target_model_reports_migration_failure(self):
        col = create_mock_collection()
        old = make_notetype(
            name="Old", fields=["A", "B"], model_id=10, model_uuid="old-uuid"
        )
        col.models.add(old)
        col.db.all.return_value = [(101, 10, "note-a")]

        note = MagicMock(get_uuid=MagicMock(return_value="note-a"))
        note.note_model_uuid = "missing-target-uuid"
        deck = _deck_with_models({})
        deck.notes = [note]
        failures = []

        assert deck._change_existing_note_types(col, failures) is False
        assert any("target model" in item for item in failures)

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
        query, *params = col.db.all.call_args.args
        assert query.startswith("SELECT id, mid, guid FROM notes WHERE guid IN")
        assert params == ["note-a", "note-b"]

    def test_apply_notetype_change_maps_fields_by_name(self):
        """The request must be mapped against the note's own old notetype."""
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

    def test_apply_notetype_change_uses_minus_one_for_new_fields(self):
        """Anki's protobuf sentinel for 'no source field' is -1, not None."""
        col = create_mock_collection()
        old = make_notetype(
            name="Old", fields=["A", "B"], model_id=10, model_uuid="old-uuid"
        )
        target = make_notetype(
            name="Target", fields=["B", "A", "C"], model_id=20, model_uuid="t-uuid"
        )
        col.models.add(old)
        col.models.add(target)
        deck = _deck_with_models({"target": NoteModel(target)})

        with (
            patch.object(deck_module, "NotetypeId", side_effect=lambda value: value),
            patch.object(deck_module, "ChangeNotetypeRequest") as request_type,
        ):
            assert deck._apply_notetype_change(col, 10, 20, [101]) is True

        assert request_type.call_args.kwargs["new_fields"] == [1, 0, -1]
