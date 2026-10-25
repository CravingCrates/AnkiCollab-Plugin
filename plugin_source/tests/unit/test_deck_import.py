"""Tests for Deck — the core import orchestration logic.

These verify the critical paths in deck import:
- Deck structure creation (parent/child hierarchy, home deck mapping)
- Notetype handling (duplicates, compatibility, rename conflicts)
- Note collection and mapping to correct subdecks
- Bulk import flow (new notes, updates, temp deck lifecycle)
- Deck name collision handling
- Metadata loading from JSON
"""

import copy
import pytest
from unittest.mock import MagicMock, patch, call
from collections import namedtuple

from tests.conftest import (
    make_notetype,
    make_note_dict,
    make_deck_json,
    create_mock_collection,
    MockAnkiNote,
)

from crowd_anki.representation.deck import Deck, DeckMetadata
from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel
from crowd_anki.representation.deck_config import DeckConfig
from crowd_anki.representation import deck_initializer
from crowd_anki.utils.constants import UUID_FIELD_NAME

# ──────────────────────────────────────────────────────────────────────
# Deck from JSON (deck_initializer.from_json)
# ──────────────────────────────────────────────────────────────────────


class TestDeckFromJson:
    def test_simple_deck_loads(self):
        nt = make_notetype(
            name="Basic", fields=["Front", "Back"], model_uuid="model-001"
        )
        note = make_note_dict(
            guid="note1", fields=["f", "b"], note_model_uuid="model-001"
        )
        deck_json = make_deck_json(
            name="TestDeck",
            notes=[note],
            note_models=[nt],
        )
        deck = deck_initializer.from_json(deck_json)
        assert deck.anki_dict["name"] == "TestDeck"
        assert len(deck.notes) == 1
        assert deck.notes[0].get_uuid() == "note1"

    def test_deck_with_children(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        child_json = make_deck_json(
            name="ChildDeck",
            notes=[make_note_dict(guid="c1", note_model_uuid="m-001")],
        )
        parent_json = make_deck_json(
            name="ParentDeck",
            children=[child_json],
            note_models=[nt],
        )
        deck = deck_initializer.from_json(parent_json)
        assert len(deck.children) == 1
        assert deck.children[0].anki_dict["name"] == "ChildDeck"

    def test_nested_children(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        grandchild = make_deck_json(
            name="GrandChild",
            notes=[make_note_dict(guid="gc1", note_model_uuid="m-001")],
        )
        child = make_deck_json(name="Child", children=[grandchild])
        parent = make_deck_json(name="Top", note_models=[nt], children=[child])
        deck = deck_initializer.from_json(parent)
        assert len(deck.children) == 1
        assert len(deck.children[0].children) == 1
        assert deck.children[0].children[0].anki_dict["name"] == "GrandChild"

    def test_metadata_loaded_from_root(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        deck_json = make_deck_json(name="Root", note_models=[nt])
        deck = deck_initializer.from_json(deck_json)
        assert deck.metadata is not None
        assert "m-001" in deck.metadata.models

    def test_note_models_aggregated_from_children(self):
        """Notetypes scattered across children should all be collected."""
        nt1 = make_notetype(name="Type1", model_uuid="m-001", model_id=1)
        nt2 = make_notetype(name="Type2", model_uuid="m-002", model_id=2)
        child = make_deck_json(name="Child", note_models=[nt2])
        parent = make_deck_json(name="Parent", note_models=[nt1], children=[child])
        deck = deck_initializer.from_json(parent)
        assert "m-001" in deck.metadata.models
        assert "m-002" in deck.metadata.models

    def test_empty_deck(self):
        deck_json = make_deck_json(name="EmptyDeck")
        deck = deck_initializer.from_json(deck_json)
        assert len(deck.notes) == 0
        assert len(deck.children) == 0

    def test_get_note_count(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        child = make_deck_json(
            name="Child",
            notes=[
                make_note_dict(guid=f"c{i}", note_model_uuid="m-001") for i in range(3)
            ],
        )
        parent = make_deck_json(
            name="Parent",
            notes=[
                make_note_dict(guid=f"p{i}", note_model_uuid="m-001") for i in range(2)
            ],
            children=[child],
            note_models=[nt],
        )
        deck = deck_initializer.from_json(parent)
        assert deck.get_note_count() == 5


# ──────────────────────────────────────────────────────────────────────
# Deck name hierarchy
# ──────────────────────────────────────────────────────────────────────


class TestCollectAllNotesDeduplication:
    def test_same_note_in_two_decks_is_collected_once(self):
        """A card-level deck split must not turn into two imported notes."""
        root = Deck(
            MagicMock(),
            {"name": "Root", "crowdanki_uuid": "root", "id": 1},
        )
        child = Deck(
            MagicMock(),
            {"name": "Root::Child", "crowdanki_uuid": "child", "id": 2},
            is_child=True,
        )
        shared = MagicMock()
        shared.get_uuid.return_value = "shared-guid"
        root.notes = [shared]
        child.notes = [shared]
        root.children = [child]

        all_notes = []
        mapping = {}
        root._collect_all_notes(all_notes, mapping, "", None)

        assert all_notes == [shared]
        assert mapping == {"shared-guid": "Root"}


class TestDeckNameHierarchy:
    def _make_deck(self, name, home_deck=None, is_child=False):
        deck = Deck(
            MagicMock(),
            {"name": name, "crowdanki_uuid": "x", "id": 1},
            is_child=is_child,
        )
        return deck

    def test_get_full_deck_name_root_with_home_deck(self):
        deck = self._make_deck("ServerDeckName")
        full_name = deck._get_full_deck_name("", "MyStudy")
        assert full_name == "MyStudy"

    def test_get_full_deck_name_root_without_home_deck(self):
        deck = self._make_deck("TopLevel")
        full_name = deck._get_full_deck_name("", None)
        assert full_name == "TopLevel"

    def test_get_full_deck_name_child(self):
        deck = self._make_deck("SubDeck", is_child=True)
        full_name = deck._get_full_deck_name("Parent", None)
        assert full_name == "Parent::SubDeck"

    def test_get_full_deck_name_nested_under_home(self):
        deck = self._make_deck("SubA")
        full_name = deck._get_full_deck_name("MyStudy", "MyStudy", "ServerRoot")
        assert full_name == "MyStudy::SubA"


# ──────────────────────────────────────────────────────────────────────
# Note collection / mapping
# ──────────────────────────────────────────────────────────────────────


class TestNoteCollection:
    def test_collect_all_notes_flat(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        notes = [
            make_note_dict(guid=f"n{i}", note_model_uuid="m-001") for i in range(3)
        ]
        deck_json = make_deck_json(name="Flat", notes=notes, note_models=[nt])
        deck = deck_initializer.from_json(deck_json)

        all_notes = []
        note_to_deck = {}
        deck._collect_all_notes(all_notes, note_to_deck, "", "MyDeck")
        assert len(all_notes) == 3
        for n in all_notes:
            assert note_to_deck[n.get_uuid()] == "MyDeck"

    def test_collect_all_notes_with_children(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        child_notes = [
            make_note_dict(guid=f"cn{i}", note_model_uuid="m-001") for i in range(2)
        ]
        child_json = make_deck_json(name="Child", notes=child_notes)
        parent_json = make_deck_json(
            name="Parent",
            notes=[make_note_dict(guid="pn0", note_model_uuid="m-001")],
            children=[child_json],
            note_models=[nt],
        )
        deck = deck_initializer.from_json(parent_json)

        all_notes = []
        note_to_deck = {}
        deck._collect_all_notes(all_notes, note_to_deck, "", "HomeDeck")
        assert len(all_notes) == 3
        # Parent note goes to HomeDeck
        assert note_to_deck["pn0"] == "HomeDeck"
        # Child notes go to HomeDeck::Child
        for i in range(2):
            assert note_to_deck[f"cn{i}"] == "HomeDeck::Child"

    def test_deep_nesting_mapping(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        gc = make_deck_json(
            name="GC", notes=[make_note_dict(guid="gcn", note_model_uuid="m-001")]
        )
        child = make_deck_json(name="Child", children=[gc])
        root = make_deck_json(name="Root", children=[child], note_models=[nt])
        deck = deck_initializer.from_json(root)

        all_notes = []
        note_to_deck = {}
        deck._collect_all_notes(all_notes, note_to_deck, "", "Study")
        assert note_to_deck["gcn"] == "Study::Child::GC"


# ──────────────────────────────────────────────────────────────────────
# Deck structure creation
# ──────────────────────────────────────────────────────────────────────


class TestDeckStructureCreation:
    def test_create_root_deck(self):
        col = create_mock_collection()
        nt = make_notetype(name="Basic", model_uuid="m-001")
        deck_json = make_deck_json(name="MyDeck", note_models=[nt])
        deck = deck_initializer.from_json(deck_json)
        deck.collection = col

        root_name = deck._create_deck_structure(col, "", "UserDeck")
        assert root_name == "UserDeck"
        # Deck was created in collection
        assert col.decks.id.called

    def test_create_deck_with_children(self):
        col = create_mock_collection()
        nt = make_notetype(name="Basic", model_uuid="m-001")
        child = make_deck_json(name="SubA")
        deck_json = make_deck_json(name="Root", children=[child], note_models=[nt])
        deck = deck_initializer.from_json(deck_json)
        deck.collection = col

        root_name = deck._create_deck_structure(col, "", "HomeDeck")
        # Both root and child should be created
        assert col.decks.id.call_count >= 2


# ──────────────────────────────────────────────────────────────────────
# Deck rename on conflict
# ──────────────────────────────────────────────────────────────────────


class TestDeckRename:
    def test_rename_appends_ankicollab(self):
        col = create_mock_collection()
        # Create a deck that already exists
        col.decks._store[1] = {"id": 1, "name": "Existing", "crowdanki_uuid": "old"}
        new_name = Deck._rename_deck("Existing", col)
        assert "AnkiCollab" in new_name

    def test_rename_increments_on_further_conflict(self):
        col = create_mock_collection()
        col.decks._store[1] = {"id": 1, "name": "Existing", "crowdanki_uuid": "old"}
        col.decks._store[2] = {
            "id": 2,
            "name": "Existing (AnkiCollab)",
            "crowdanki_uuid": "old2",
        }
        new_name = Deck._rename_deck("Existing", col)
        assert new_name.startswith("Existing (AnkiCollab)")
        assert new_name != "Existing (AnkiCollab)"


# ──────────────────────────────────────────────────────────────────────
# Notetype handling during import
# ──────────────────────────────────────────────────────────────────────


class TestNotetypeHandling:
    def test_are_notetypes_compatible_identical(self):
        nt = make_notetype(name="Basic", fields=["Front", "Back"])
        col = create_mock_collection()
        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        deck.collection = col
        nm = NoteModel(nt)
        assert deck._are_notetypes_compatible(nm, nt) is True

    def test_are_notetypes_compatible_different_fields(self):
        nt1 = make_notetype(name="Basic", fields=["Front", "Back"])
        nt2 = make_notetype(name="Basic", fields=["Question", "Answer"])
        col = create_mock_collection()
        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        deck.collection = col
        nm1 = NoteModel(nt1)
        assert deck._are_notetypes_compatible(nm1, nt2) is False

    def test_check_fields_compatible_superset(self):
        """Local notetype with extra fields is compatible (user added custom fields)."""
        nt_remote = make_notetype(name="ProjektAnki Test", fields=["A", "B"])
        nt_local = make_notetype(name="ProjektAnki Test", fields=["A", "B", "MyCustom"])

        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        assert deck._check_fields_compatible(nt_remote, nt_local) is True

    def test_check_fields_compatible_missing(self):
        """Local notetype missing remote fields is NOT compatible."""
        nt_remote = make_notetype(name="ProjektAnki Test", fields=["A", "B", "C"])
        nt_local = make_notetype(name="ProjektAnki Test", fields=["A", "B"])

        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        assert deck._check_fields_compatible(nt_remote, nt_local) is False


# ──────────────────────────────────────────────────────────────────────
# Risk assessment for notetype changes
# ──────────────────────────────────────────────────────────────────────


class TestChangeRiskAssessment:
    def test_no_risk_identical(self):
        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        old = make_notetype(fields=["A", "B"])
        new = make_notetype(fields=["A", "B"])
        assert deck._assess_change_risk(old, new) is False

    def test_risk_removed_fields(self):
        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        old = make_notetype(fields=["A", "B", "C"])
        new = make_notetype(fields=["A", "B"])
        assert deck._assess_change_risk(old, new) is True

    def test_risk_significant_reorder(self):
        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        old = make_notetype(fields=["A", "B", "C", "D"])
        new = make_notetype(fields=["D", "C", "B", "A"])  # fully reversed
        assert deck._assess_change_risk(old, new) is True


# ──────────────────────────────────────────────────────────────────────
# Metadata loading
# ──────────────────────────────────────────────────────────────────────


class TestMetadataLoading:
    def test_load_metadata_from_json_no_configs(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        deck_json = make_deck_json(name="Test", note_models=[nt])
        deck = deck_initializer.from_json(deck_json)
        assert deck.metadata is not None
        assert len(deck.metadata.models) == 1

    def test_load_metadata_with_deck_config(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        config = {
            "id": 10,
            "name": "MyConfig",
            "crowdanki_uuid": "cfg-001",
        }
        deck_json = make_deck_json(
            name="Test", note_models=[nt], deck_configurations=[config]
        )
        deck = deck_initializer.from_json(deck_json)
        assert len(deck.metadata.deck_configs) == 1


# ──────────────────────────────────────────────────────────────────────
# Deck flatten (for export)
# ──────────────────────────────────────────────────────────────────────


class TestDeckFlatten:
    def test_child_deck_uses_leaf_name(self):
        deck = Deck(
            MagicMock(),
            {"name": "Parent::Child::Grandchild", "crowdanki_uuid": "x", "id": 1},
            is_child=True,
        )
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        result = deck.flatten()
        assert result["name"] == "Grandchild"

    def test_root_deck_uses_full_name(self):
        deck = Deck(
            MagicMock(),
            {"name": "TopLevel", "crowdanki_uuid": "x", "id": 1},
            is_child=False,
        )
        # flatten() accesses metadata, so set a minimal DeckMetadata
        deck.metadata = DeckMetadata(models={}, deck_configs={})
        result = deck.flatten()
        assert result["name"] == "TopLevel"


# ──────────────────────────────────────────────────────────────────────
# Temp deck cleanup
# ──────────────────────────────────────────────────────────────────────


class TestTempDeckCleanup:
    def test_cleanup_empty_temp_deck(self):
        col = create_mock_collection()
        col.decks._store[99] = {
            "id": 99,
            "name": "_ankicollab_import_abc",
            "crowdanki_uuid": "",
        }

        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        deck.collection = col
        deck._cleanup_temp_deck(col, 99, "_ankicollab_import_abc", "test")
        assert 99 not in col.decks._store

    def test_cleanup_nonexistent_deck(self):
        col = create_mock_collection()
        col.decks.get = MagicMock(return_value=None)
        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        # Should not raise
        deck._cleanup_temp_deck(col, 999, "nonexistent", "test")

    def test_cleanup_deck_with_cards_not_deleted(self):
        col = create_mock_collection()
        col.decks._store[99] = {
            "id": 99,
            "name": "_ankicollab_import_abc",
            "crowdanki_uuid": "",
        }
        col.decks.card_count = MagicMock(return_value=5)

        deck = Deck(MagicMock(), {"name": "D", "crowdanki_uuid": "x", "id": 1})
        deck._cleanup_temp_deck(col, 99, "_ankicollab_import_abc", "test")
        # Deck should NOT be removed since it still has cards
        assert 99 in col.decks._store


# ──────────────────────────────────────────────────────────────────────
# Deck initializer helpers
# ──────────────────────────────────────────────────────────────────────


class TestDeckInitializerHelpers:
    def test_remove_unchanged_notes(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        notes = [
            make_note_dict(guid=f"n{i}", note_model_uuid="m-001") for i in range(3)
        ]
        deck_json = make_deck_json(name="Test", notes=notes, note_models=[nt])
        deck = deck_initializer.from_json(deck_json)

        # Simulate mod timestamps: only n0 was modified after timestamp
        for i, note in enumerate(deck.notes):
            mock_obj = MockAnkiNote()
            mock_obj.mod = 100 + i * 10  # 100, 110, 120
            note.anki_object = mock_obj

        # timestamp2 must also exclude old notes (not be 0, since all mod > 0)
        deck_initializer.remove_unchanged_notes(deck, 115, 115)
        # Only note with mod=120 should remain
        assert len(deck.notes) == 1

    def test_remove_tags_from_notes(self):
        nt = make_notetype(name="Basic", model_uuid="m-001")
        notes = [
            make_note_dict(
                guid="n1", note_model_uuid="m-001", tags=["keep", "remove_me"]
            )
        ]
        deck_json = make_deck_json(name="Test", notes=notes, note_models=[nt])
        deck = deck_initializer.from_json(deck_json)

        for note in deck.notes:
            mock_obj = MockAnkiNote()
            mock_obj.tags = list(note.anki_object_dict["tags"])
            note.anki_object = mock_obj

        deck_initializer.remove_tags_from_notes(deck, ["remove_me"])
        assert "remove_me" not in deck.notes[0].anki_object.tags

    def test_trim_empty_children(self):
        child_empty = make_deck_json(name="Empty")
        child_with_notes = make_deck_json(
            name="HasNotes", notes=[make_note_dict(guid="n1", note_model_uuid="m-001")]
        )
        nt = make_notetype(name="Basic", model_uuid="m-001")
        root = make_deck_json(
            name="Root", children=[child_empty, child_with_notes], note_models=[nt]
        )
        deck = deck_initializer.from_json(root)
        deck_initializer.trim_empty_children(deck)
        assert len(deck.children) == 1
        assert deck.children[0].anki_dict["name"] == "HasNotes"
