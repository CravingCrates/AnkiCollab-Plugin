"""Mutation-driven tests for deck.py media helpers and misc accessors.

Covers the survived mutants in the before-run for:
- ``_load_deck_config`` / ``_load_metadata_from_json`` / ``_add_models_from_children``
- ``get_media_file_list`` / ``get_media_file_note_map``
- ``serialization_dict`` / ``calculate_total_work`` / ``_get_all_notes_recursive``
- ``refresh_notes`` / ``_update_db`` / ``__init__``
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import create_mock_collection, make_notetype

from crowd_anki.representation import deck as deck_module
from crowd_anki.representation.deck import Deck, DeckMetadata
from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel
from crowd_anki.utils.constants import UUID_FIELD_NAME


def _patch_logger(method="info"):
    return patch.object(deck_module.logger, method)


def _deck(anki_dict=None, is_child=False):
    return Deck(
        MagicMock(),
        anki_dict or {"name": "Root", "crowdanki_uuid": "d1", "id": 1},
        is_child=is_child,
    )


def _note(anki_object, note_model_uuid="m1", uuid="n1"):
    note = Note(MagicMock())
    note.anki_object = anki_object
    note.note_model_uuid = note_model_uuid
    note._uuid = uuid
    note.get_uuid = lambda: uuid
    return note


class _MediaAnkiObject:
    """Minimal anki note exposing fields + joined_fields for media scans."""

    def __init__(self, fields, mid=1):
        self.fields = fields
        self.mid = mid
        self.id = 42

    def joined_fields(self):
        return "\x1f".join(self.fields)


# ──────────────────────────────────────────────────────────────────────
# __init__
# ──────────────────────────────────────────────────────────────────────


class TestDeckInit:
    def test_root_defaults(self):
        deck = _deck()
        assert deck.is_child is False
        assert deck.notes == []
        assert deck.children == []
        assert deck.metadata is None
        assert deck.root_deck_id is None
        assert deck.keep_empty_subdecks is False

    def test_child_flag(self):
        deck = _deck(is_child=True)
        assert deck.is_child is True


# ──────────────────────────────────────────────────────────────────────
# _load_deck_config
# ──────────────────────────────────────────────────────────────────────


class TestLoadDeckConfig:
    def test_with_config_id_loads_config(self):
        col = create_mock_collection()
        deck = _deck({"name": "D", "crowdanki_uuid": "d1", "id": 1, "conf": 1})
        deck.collection = col
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        deck._load_deck_config()
        assert "cfg-uuid-001" in deck.metadata.deck_configs

    def test_without_config_id_warns(self):
        col = create_mock_collection()
        deck = _deck({"name": "D", "crowdanki_uuid": "d1", "id": 1})
        deck.collection = col
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        with _patch_logger("warning") as mock_warn:
            deck._load_deck_config()
        assert mock_warn.called
        assert deck.metadata.deck_configs == {}


# ──────────────────────────────────────────────────────────────────────
# _load_metadata_from_json / _add_models_from_children
# ──────────────────────────────────────────────────────────────────────


class TestLoadMetadataFromJson:
    def test_loads_models_and_configs(self):
        deck = _deck()
        nt = make_notetype(name="Basic", model_uuid="m-uuid", model_id=1)
        config = {"id": 10, "name": "Conf", "crowdanki_uuid": "c-uuid"}
        deck._load_metadata_from_json(
            {"note_models": [nt], "deck_configurations": [config]}
        )
        assert "m-uuid" in deck.metadata.models
        assert "c-uuid" in deck.metadata.deck_configs

    def test_loads_models_from_children(self):
        deck = _deck()
        nt = make_notetype(name="Basic", model_uuid="child-uuid", model_id=2)
        deck._load_metadata_from_json(
            {"note_models": [], "children": [{"note_models": [nt], "children": []}]}
        )
        assert "child-uuid" in deck.metadata.models

    def test_empty_json(self):
        deck = _deck()
        deck._load_metadata_from_json({})
        assert deck.metadata is not None
        assert deck.metadata.models == {}
        assert deck.metadata.deck_configs == {}


# ──────────────────────────────────────────────────────────────────────
# serialization_dict
# ──────────────────────────────────────────────────────────────────────


class TestSerializationDict:
    def test_root_includes_models_and_configs(self):
        deck = _deck()
        deck.metadata = DeckMetadata(
            models={"m": MagicMock()}, deck_configs={"c": MagicMock()}
        )
        result = deck.serialization_dict()
        assert "note_models" in result
        assert "deck_configurations" in result

    def test_child_omits_models_and_configs(self):
        deck = _deck(is_child=True)
        deck.metadata = DeckMetadata(
            models={"m": MagicMock()}, deck_configs={"c": MagicMock()}
        )
        result = deck.serialization_dict()
        assert "note_models" not in result
        assert "deck_configurations" not in result


# ──────────────────────────────────────────────────────────────────────
# calculate_total_work / _get_all_notes_recursive
# ──────────────────────────────────────────────────────────────────────


class TestCounts:
    def test_calculate_total_work_includes_children(self):
        parent = _deck()
        parent.notes = [_note(_MediaAnkiObject(["x"]))]
        child = _deck(is_child=True)
        child.notes = [_note(_MediaAnkiObject(["x"])), _note(_MediaAnkiObject(["x"]))]
        parent.children = [child]
        assert parent.calculate_total_work() == 3

    def test_calculate_total_work_ignores_children_when_disabled(self):
        parent = _deck()
        parent.notes = [_note(_MediaAnkiObject(["x"]))]
        child = _deck(is_child=True)
        child.notes = [_note(_MediaAnkiObject(["x"]))]
        parent.children = [child]
        assert parent.calculate_total_work(include_children=False) == 1

    def test_get_all_notes_recursive(self):
        parent = _deck()
        n1 = _note(_MediaAnkiObject(["x"]))
        parent.notes = [n1]
        child = _deck(is_child=True)
        n2 = _note(_MediaAnkiObject(["x"]))
        child.notes = [n2]
        parent.children = [child]
        assert parent._get_all_notes_recursive() == [n1, n2]


# ──────────────────────────────────────────────────────────────────────
# get_media_file_list
# ──────────────────────────────────────────────────────────────────────


class TestGetMediaFileList:
    def test_media_from_notes(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        deck.collection.media.files_in_str.return_value = ["a.mp3", "b.png"]
        deck.notes = [_note(_MediaAnkiObject(["x"]))]
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        with patch.object(Deck, "_get_media_from_models", return_value=set()):
            result = deck.get_media_file_list(data_from_models=False)
        assert result == {"a.mp3", "b.png"}

    def test_none_anki_object_skipped(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        deck.collection.media.files_in_str.return_value = ["a.mp3"]
        n = _note(_MediaAnkiObject(["x"]))
        n.anki_object = None
        deck.notes = [n]
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        with patch.object(Deck, "_get_media_from_models", return_value=set()):
            result = deck.get_media_file_list(data_from_models=False)
        assert result == set()

    def test_includes_children(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        deck.collection.media.files_in_str.return_value = ["parent.mp3"]
        deck.notes = [_note(_MediaAnkiObject(["x"]))]
        child = _deck(is_child=True)
        child.collection = create_mock_collection()
        child.collection.media.files_in_str.return_value = ["child.mp3"]
        child.notes = [_note(_MediaAnkiObject(["x"]))]
        deck.children = [child]
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        with patch.object(Deck, "_get_media_from_models", return_value=set()):
            result = deck.get_media_file_list(data_from_models=False)
        assert result == {"parent.mp3", "child.mp3"}

    def test_data_from_models_union(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        deck.collection.media.files_in_str.return_value = []
        deck.notes = []
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        with patch.object(Deck, "_get_media_from_models", return_value={"tmpl.png"}):
            result = deck.get_media_file_list(data_from_models=True)
        assert result == {"tmpl.png"}


# ──────────────────────────────────────────────────────────────────────
# get_media_file_note_map
# ──────────────────────────────────────────────────────────────────────


class TestGetMediaFileNoteMap:
    def _setup(self, model_name="Basic"):
        deck = _deck()
        deck.collection = create_mock_collection()
        model = make_notetype(name=model_name, model_uuid="m1", model_id=1)
        deck.metadata = DeckMetadata(models={"m1": NoteModel(model)}, deck_configs={})
        return deck

    def test_returns_pairs_skipping_protected(self):
        deck = self._setup()
        deck.collection.media.files_in_str.side_effect = lambda mid, field: {
            "front": ["a.mp3"],
            "back": ["b.mp3"],
        }[field]
        n = _note(_MediaAnkiObject(["front", "back"]), "m1", "n1")
        deck.notes = [n]
        protected = {"model_name_to_indices": {"Basic": [0]}}  # protect field 0
        result = deck.get_media_file_note_map(protected, include_children=False)
        assert result == [("b.mp3", "n1")]

    def test_skips_subdir_files(self):
        deck = self._setup()
        deck.collection.media.files_in_str.return_value = ["sub/a.mp3", "b.mp3"]
        n = _note(_MediaAnkiObject(["front", "back"]), "m1", "n1")
        deck.notes = [n]
        result = deck.get_media_file_note_map(
            {"model_name_to_indices": {}}, include_children=False
        )
        # sub/a.mp3 is not a basename -> skipped.
        assert ("sub/a.mp3", "n1") not in result
        assert ("b.mp3", "n1") in result

    def test_missing_model_skipped_with_warning(self):
        deck = self._setup()
        deck.collection.media.files_in_str.return_value = ["a.mp3"]
        n = _note(_MediaAnkiObject(["front"]), "missing-model", "n1")
        deck.notes = [n]
        with _patch_logger("warning") as mock_warn:
            result = deck.get_media_file_note_map(
                {"model_name_to_indices": {}}, include_children=False
            )
        assert result == []
        assert mock_warn.called

    def test_none_anki_object_skipped(self):
        deck = self._setup()
        n = _note(_MediaAnkiObject(["front"]), "m1", "n1")
        n.anki_object = None
        deck.notes = [n]
        result = deck.get_media_file_note_map(
            {"model_name_to_indices": {}}, include_children=False
        )
        assert result == []


# ──────────────────────────────────────────────────────────────────────
# refresh_notes / _update_db
# ──────────────────────────────────────────────────────────────────────


class TestRefreshNotes:
    def test_refreshes_matching_notes(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        refreshed = MagicMock()
        deck.collection.get_note.return_value = refreshed
        n1 = _note(_MediaAnkiObject(["x"]), "m1", "n1")
        n2 = _note(_MediaAnkiObject(["x"]), "m1", "n2")
        n1.anki_object = MagicMock()
        n1.anki_object.id = 1
        n2.anki_object = MagicMock()
        n2.anki_object.id = 2
        deck.notes = [n1, n2]
        deck.refresh_notes([("a.mp3", "n2")])
        assert n2.anki_object is refreshed
        assert n1.anki_object.id == 1  # untouched

    def test_refreshes_children(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        deck.notes = []
        child = _deck(is_child=True)
        child.collection = deck.collection
        child.collection.get_note.return_value = MagicMock()
        child.notes = []
        child.refresh_notes = MagicMock()
        deck.children = [child]
        deck.refresh_notes([])
        child.refresh_notes.assert_called_once_with([])


class TestUpdateDb:
    def test_adds_uuid_column(self):
        deck = _deck()
        deck.collection = create_mock_collection()
        with patch.object(deck_module.utils, "add_column") as mock_add:
            deck._update_db()
        mock_add.assert_called_once_with(deck.collection.db, "notes", UUID_FIELD_NAME)
