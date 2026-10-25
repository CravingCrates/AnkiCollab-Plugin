"""Export workflow tests (Phase 3.1).

Runs the real export pipeline — ``deck_initializer.from_collection``, which is
what ``export_manager.handle_export``/``suggest_notes`` use to build the deck
representation — against a realistic in-memory collection, and asserts on the
resulting representation: deck hierarchy, note GUIDs, note-model UUIDs, field
values, protected/personal-tag handling, and media references.
"""

import json

import pytest

pytestmark = pytest.mark.integration

from crowd_anki.representation import deck_initializer
from tests.conftest import make_notetype

PERSONAL = "leech"  # from var_defs.DEFAULT_PROTECTED_TAGS
PROTECT_PREFIX = "AnkiCollab_Protect"
PERSONAL_PREFIX = "AnkiCollab_Personal"


class TestExportWorkflow:
    def _seed(self, col):
        """A realistic collection: 2 nested decks, 2 notetypes, mixed notes."""
        basic = make_notetype(
            name="Basic",
            fields=["Front", "Back"],
            model_id=100,
            model_uuid="model-basic",
        )
        col.add_model(basic)
        cloze = make_notetype(
            name="Cloze",
            fields=["Text", "Extra"],
            model_id=101,
            model_uuid="model-cloze",
        )
        col.add_model(cloze)

        parent_did = col.add_deck("Parent", deck_uuid="deck-parent")
        child_did = col.add_deck("Parent::Child", deck_uuid="deck-child")

        col.add_note_record(
            guid="note-a",
            mid=100,
            fields=["Front A", "Back A"],
            tags=["tag1", f"{PROTECT_PREFIX}::Front", PERSONAL],
            did=parent_did,
        )
        col.add_note_record(
            guid="note-b",
            mid=100,
            fields=["Front B", "Back B"],
            tags=["tag2"],
            did=child_did,
        )
        col.add_note_record(
            guid="note-c",
            mid=101,
            fields=["Cloze text", "Extra"],
            tags=[],
            did=child_did,
        )
        return parent_did, child_did

    def test_hierarchy_and_note_guids_survive(
        self, realistic_collection, fake_anki_note_class
    ):
        col = realistic_collection
        self._seed(col)
        deck = deck_initializer.from_collection(col, "Parent")

        assert deck is not None
        assert deck.anki_dict["name"] == "Parent"
        assert len(deck.children) == 1
        # Child's raw deck dict keeps the full path; flatten() reduces it.
        assert deck.children[0].anki_dict["name"] == "Parent::Child"
        assert deck.children[0].flatten()["name"] == "Child"

        parent_guids = {n.get_uuid() for n in deck.notes}
        assert "note-a" in parent_guids
        child_guids = {n.get_uuid() for n in deck.children[0].notes}
        assert {"note-b", "note-c"} <= child_guids

    def test_field_values_preserved(self, realistic_collection, fake_anki_note_class):
        col = realistic_collection
        self._seed(col)
        deck = deck_initializer.from_collection(col, "Parent")

        note_a = next(n for n in deck.notes if n.get_uuid() == "note-a")
        assert note_a.anki_object.fields == ["Front A", "Back A"]

    def test_note_model_uuids_in_metadata(
        self, realistic_collection, fake_anki_note_class
    ):
        col = realistic_collection
        self._seed(col)
        deck = deck_initializer.from_collection(col, "Parent")

        assert "model-basic" in deck.metadata.models
        assert "model-cloze" in deck.metadata.models

    def test_personal_tags_stripped_before_serialization(
        self, realistic_collection, fake_anki_note_class
    ):
        """Personal/protected tags must be excluded from the exported payload."""
        col = realistic_collection
        self._seed(col)
        deck = deck_initializer.from_collection(col, "Parent")

        # The real export step that strips personal tags before sending
        deck_initializer.remove_tags_from_notes(deck, [PERSONAL, PERSONAL_PREFIX])

        for note in deck.notes:
            assert PERSONAL not in note.anki_object.tags
            assert PERSONAL_PREFIX not in note.anki_object.tags
        # Ordinary tags survive
        note_a = next(n for n in deck.notes if n.get_uuid() == "note-a")
        assert "tag1" in note_a.anki_object.tags

    def test_note_with_cards_in_two_decks_is_exported_once(
        self, realistic_collection, fake_anki_note_class
    ):
        """Anki decks are card-level; AnkiCollab notes are note-level.

        A note with a card in the parent and a card in the child is returned by
        the per-deck note query for both decks. The exported tree must collapse
        that to one occurrence, or upload/import duplicates the note and all of
        its cards once per deck.
        """
        col = realistic_collection
        root_did = col.add_deck("Root", deck_uuid="deck-root")
        child_did = col.add_deck("Root::Child", deck_uuid="deck-child")
        col.add_model(
            make_notetype(
                name="Basic", fields=["Front", "Back"], model_id=100, model_uuid="m-1"
            )
        )
        shared_nid = col.add_note_record(
            guid="shared-note",
            mid=100,
            fields=["front", "back"],
            tags=[],
            did=root_did,
        )

        def note_ids_for_deck(did, include_from_dynamic=False):
            return [shared_nid] if did in (root_did, child_did) else []

        col.decks.get_note_ids.side_effect = note_ids_for_deck

        deck = deck_initializer.from_collection(col, "Root")

        all_guids = []

        def collect(current):
            all_guids.extend(n.get_uuid() for n in current.notes)
            for child in current.children:
                collect(child)

        collect(deck)
        assert all_guids == ["shared-note"]
        assert deck.get_note_count() == 1

    def test_serialization_is_json_serializable(
        self, realistic_collection, fake_anki_note_class
    ):
        """The exported Deck must serialize to JSON (what post_gzip sends)."""
        from crowd_anki.representation.deck import Deck

        col = realistic_collection
        self._seed(col)
        deck = deck_initializer.from_collection(col, "Parent")
        deck_initializer.remove_tags_from_notes(deck, [PERSONAL])

        payload = json.dumps(deck, default=Deck.default_json, sort_keys=True)
        parsed = json.loads(payload)
        assert parsed["name"] == "Parent"
        assert parsed["note_models"]
        # Personal tags absent from the serialized notes
        assert "leech" not in payload

    def test_media_references_collected(
        self, realistic_collection, fake_anki_note_class
    ):
        col = realistic_collection
        self._seed(col)
        (col.media_dir / "pic.png").write_bytes(b"x" * 100)
        col.media.files_in_str.return_value = ["pic.png"]

        deck = deck_initializer.from_collection(col, "Parent")
        # get_media_file_note_map returns (filename, note_guid) pairs
        pairs = deck.get_media_file_note_map({"model_name_to_indices": {}})
        assert ("pic.png", "note-a") in pairs
