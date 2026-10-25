"""Export-side note identity against a real Anki collection.

Anki stores deck membership per card; AnkiCollab stores it per note. A note with
cards in two decks is returned by the per-deck note query for both decks. The
export tree must collapse those occurrences before upload, otherwise the server
stores one note per deck and import creates one note (plus all its cards) per
copy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    add_note,
    col,  # noqa: F401  (pytest fixture)
    import_deck_layer,
    make_notetype,
)

from ankicollab_under_test.crowd_anki.representation.note_dedup import (  # noqa: E402
    deduplicate_notes,
)


class _Note:
    def __init__(self, guid):
        self.guid = guid

    def get_uuid(self):
        return self.guid


class _Deck:
    def __init__(self, name, notes=None):
        self.anki_dict = {"name": name}
        self.notes = list(notes or [])
        self.children = []


def _note_ids_in_deck(collection, deck_id):
    card_ids = collection.db.list("select id from cards where did = ?", deck_id)
    if not card_ids:
        return set()
    placeholders = ",".join("?" * len(card_ids))
    return set(
        collection.db.list(
            f"select distinct nid from cards where id in ({placeholders})", *card_ids
        )
    )


def test_a_note_with_cards_in_two_decks_is_returned_twice_then_collapsed(col):
    notetype = make_notetype(col, "Two Decks", ["A", "B"], uuid="two-decks")
    stored = col.models.get(notetype["id"])
    second = col.models.new_template("Card 2")
    second["qfmt"] = "{{B}}"
    second["afmt"] = "{{FrontSide}}"
    col.models.add_template(stored, second)
    col.models.update_dict(stored)
    notetype = col.models.get(notetype["id"])

    first_deck = col.decks.id("First")
    second_deck = col.decks.id("Second")
    note = add_note(col, notetype, first_deck, ["a", "b"])
    card_ids = list(note.card_ids())
    assert len(card_ids) == 2
    col.set_deck([card_ids[1]], second_deck)

    assert note.id in _note_ids_in_deck(col, first_deck)
    assert note.id in _note_ids_in_deck(col, second_deck)

    shared = _Note(note.guid)
    root = _Deck("First", [shared])
    child = _Deck("Second", [shared])
    root.children = [child]

    assert deduplicate_notes(root) == 1
    assert [n.get_uuid() for n in root.notes] == [note.guid]
    assert child.notes == []


def _count_serialized_notes(node):
    return len(node.get("notes", [])) + sum(
        _count_serialized_notes(child) for child in node.get("children", [])
    )


def test_real_export_serializes_a_multi_deck_note_once(col, monkeypatch):
    """The full export path: 6 cards in 6 decks -> one note in the payload."""
    import aqt

    notetype = make_notetype(col, "Six Decks", ["A", "B"], uuid="six-decks")
    stored = col.models.get(notetype["id"])
    for index in range(2, 7):
        template = col.models.new_template(f"Card {index}")
        template["qfmt"] = f"{{{{A}}}}<br>Card {index}"
        template["afmt"] = "{{FrontSide}}"
        col.models.add_template(stored, template)
    col.models.update_dict(stored)
    notetype = col.models.get(notetype["id"])

    col.decks.id("Export Root")
    child_decks = [col.decks.id(f"Export Root::Deck {index}") for index in range(6)]
    note = add_note(col, notetype, child_decks[0], ["a", "b"])
    card_ids = list(note.card_ids())
    assert len(card_ids) == 6
    for card_id, deck_id in zip(card_ids, child_decks):
        col.set_deck([card_id], deck_id)

    # Deck.__init__ reads aqt.mw.col; the real suite runs outside the UI.
    monkeypatch.setattr(aqt, "mw", type("MW", (), {"col": col})())
    from ankicollab_under_test.crowd_anki.config.config_settings import (
        ConfigSettings,
    )

    # Note.__init__ resolves the add-on settings singleton, which needs mw.pm.
    monkeypatch.setattr(
        ConfigSettings, "get_instance", lambda *args, **kwargs: MagicMock()
    )
    deck_initializer, Deck = import_deck_layer()
    deck = deck_initializer.from_collection(col, "Export Root")

    assert deck.get_note_count() == 1
    payload = json.loads(json.dumps(deck, default=Deck.default_json))
    assert _count_serialized_notes(payload) == 1

    all_cards = col.db.list("select id from cards where nid = ?", note.id)
    assert len(all_cards) == 6
