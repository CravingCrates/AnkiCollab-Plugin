"""Deck-tree note deduplication.

Anki assigns decks to cards, AnkiCollab to notes. A note whose cards are in
several decks appears in each of those deck objects. Exporting that tree without
collapsing duplicates uploads the note once per deck, and importing creates one
full note (all cards) per occurrence.
"""

from __future__ import annotations

from tests.conftest import make_deck_json, make_note_dict

from crowd_anki.representation import deck_initializer
from crowd_anki.representation.note_dedup import deduplicate_notes


class _Note:
    def __init__(self, guid):
        self.guid = guid

    def get_uuid(self):
        return self.guid


class _Deck:
    def __init__(self, name, notes=None, children=None):
        self.anki_dict = {"name": name}
        self.notes = list(notes or [])
        self.children = list(children or [])


def _guids(deck):
    found = [note.get_uuid() for note in deck.notes]
    for child in deck.children:
        found.extend(_guids(child))
    return found


def test_parent_and_child_occurrence_are_collapsed():
    shared = _Note("same-note")
    child = _Deck("Root::Child", [shared])
    root = _Deck("Root", [shared], [child])

    assert deduplicate_notes(root) == 1
    assert _guids(root) == ["same-note"]
    assert root.notes[0] is shared


def test_sibling_duplicates_keep_the_first_sorted_child():
    shared = _Note("same-note")
    first = _Deck("Root::A", [shared])
    second = _Deck("Root::B", [shared])
    root = _Deck("Root", [], [first, second])

    assert deduplicate_notes(root) == 1
    assert _guids(first) == ["same-note"]
    assert _guids(second) == []


def test_notes_without_a_guid_are_not_collapsed_into_one():
    root = _Deck("Root", [_Note(""), _Note(""), _Note("real")])

    assert deduplicate_notes(root) == 0
    assert len(root.notes) == 3


def test_from_json_collapses_duplicates_across_decks():
    note = make_note_dict(guid="duplicated", note_model_uuid="m-001")
    child = make_deck_json(name="Child", notes=[note])
    root_json = make_deck_json(name="Root", notes=[note], children=[child])

    deck = deck_initializer.from_json(root_json)

    assert len(deck.notes) == 1
    assert len(deck.children[0].notes) == 0
    assert deck.get_note_count() == 1
