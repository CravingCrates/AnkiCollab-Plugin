from typing import Optional, Iterable, Dict, Set, Tuple
from collections import defaultdict

from .deck import Deck
from .note import Note
from .note_dedup import deduplicate_notes
from ..anki.adapters.anki_deck import AnkiDeck
from ..anki.adapters.note_model_file_provider import NoteModelFileProvider

import anki.collection
import anki.utils
from anki.decks import DeckManager


def get_card_ids(self, did, children=False, include_from_dynamic=False):
    deck_ids = [did] + (
        [deck_id for _, deck_id in self.children(did)] if children else []
    )

    request = "select id from cards where did in {}" + (
        " or (odid != 0 and odid in {})" if include_from_dynamic else ""
    )
    parameters = (anki.utils.ids2str(deck_ids),) + (
        (anki.utils.ids2str(deck_ids),) if include_from_dynamic else tuple()
    )

    return self.col.db.list(request.format(*parameters))


def get_note_ids(self, deck_id, children=False, include_from_dynamic=False):
    deck_ids = [deck_id] + (
        [child_id for _, child_id in self.children(deck_id)] if children else []
    )
    deck_ids_str = anki.utils.ids2str(deck_ids)
    if include_from_dynamic:
        request = (
            f"SELECT DISTINCT nid FROM cards "
            f"WHERE did IN {deck_ids_str} OR (odid != 0 AND odid IN {deck_ids_str})"
        )
    else:
        request = f"SELECT DISTINCT nid FROM cards WHERE did IN {deck_ids_str}"
    return self.col.db.list(request)


DeckManager.get_card_ids = get_card_ids
DeckManager.get_note_ids = get_note_ids


def _build_note_deck_context(
    collection, note_ids: Iterable[int]
) -> Tuple[Optional[Dict[int, Set[int]]], Optional[Set[str]]]:
    """Map note_ids to the deck IDs containing their cards and identify all relevant ancestor deck names.

    Returns (deck_note_map, relevant_deck_names_lower) or (None, None) if context cannot be built.
    """
    note_id_list = list(note_ids)
    if not note_id_list:
        return {}, set()

    if not (hasattr(collection, "db") and hasattr(collection.db, "all")):
        return None, None

    try:
        ids_str = anki.utils.ids2str(note_id_list)
        query = f"SELECT DISTINCT nid, did, odid FROM cards WHERE nid IN {ids_str}"
        rows = collection.db.all(query)
    except Exception:
        return None, None

    if not isinstance(rows, list):
        return None, None

    if not rows:
        # In a real Anki collection, an empty list means these notes genuinely have no cards.
        # In test doubles (e.g. RealisticCollection), db.all("... from cards ...") returns []
        # because cards are not stored in SQLite; fall back to collection.decks.get_note_ids.
        if isinstance(collection, anki.collection.Collection):
            return {}, set()
        return None, None

    deck_note_map: Dict[int, Set[int]] = defaultdict(set)
    for row in rows:
        try:
            nid, did, odid = row
        except (TypeError, ValueError):
            return None, None
        if not (
            isinstance(nid, int) and isinstance(did, int) and isinstance(odid, int)
        ):
            return None, None
        if did:
            deck_note_map[did].add(nid)
        if odid:
            deck_note_map[odid].add(nid)

    # Identify all relevant deck names (non-dynamic decks containing the notes + all their ancestor prefixes).
    relevant_deck_names_lower: Set[str] = set()
    delim = Deck.DECK_NAME_DELIMITER
    for did in list(deck_note_map.keys()):
        try:
            name = collection.decks.name(did)
        except Exception:
            name = None
        if not isinstance(name, str) or not name:
            return None, None

        # Skip dynamic (filtered) decks since from_collection always ignores them.
        try:
            anki_dict = collection.decks.by_name(name)
            if anki_dict is not None and AnkiDeck(anki_dict).is_dynamic:
                continue
        except Exception:
            pass

        parts = name.split(delim)
        for i in range(1, len(parts) + 1):
            relevant_deck_names_lower.add(delim.join(parts[:i]).lower())

    return deck_note_map, relevant_deck_names_lower


def from_collection(
    collection,
    name,
    deck_metadata=None,
    is_child=False,
    note_ids=None,
    deck_note_map=None,
    relevant_deck_names=None,
) -> Optional[Deck]:
    """load metadata, load notes, load children"""
    decks = collection.decks
    by_name = decks.by_name
    anki_dict = by_name(name)

    if anki_dict is None:
        return None

    if AnkiDeck(anki_dict).is_dynamic:
        return None

    # Precompute deck context once at root when note_ids are restricted
    if not is_child and note_ids is not None and deck_note_map is None:
        note_ids = list(note_ids)
        deck_note_map, relevant_deck_names = _build_note_deck_context(
            collection, note_ids
        )

    deck = Deck(NoteModelFileProvider, anki_dict, is_child)
    deck.collection = collection
    deck._update_fields()
    deck.metadata = deck_metadata
    deck._load_metadata()
    deck_id = deck.anki_dict["id"]

    if note_ids is None:  # When nids are unknown, load every note in the deck
        note_ids_to_load = collection.decks.get_note_ids(
            deck_id, include_from_dynamic=True
        )
    elif not note_ids:  # Short-circuit empty inputs instead of querying the DB
        note_ids_to_load = []
    elif deck_note_map is not None:
        deck_note_ids = deck_note_map.get(deck_id, ())
        note_ids_to_load = [note_id for note_id in note_ids if note_id in deck_note_ids]
    else:  # Restrict the provided nids to those that actually belong to this deck
        deck_note_ids = set(
            collection.decks.get_note_ids(deck_id, include_from_dynamic=True)
        )
        note_ids_to_load = [note_id for note_id in note_ids if note_id in deck_note_ids]

    # Finally load the notes
    if note_ids_to_load:
        deck.notes = Note.get_notes_from_nids(
            collection, deck.metadata.models, note_ids_to_load
        )
    else:
        deck.notes = []

    name_prefix_len = len(name) + len(Deck.DECK_NAME_DELIMITER)
    if relevant_deck_names is not None:
        direct_children = [
            child_name
            for child_name, _ in decks.children(deck_id)
            if child_name.lower() in relevant_deck_names
            and Deck.DECK_NAME_DELIMITER not in child_name[name_prefix_len:]
        ]
    else:
        direct_children = [
            child_name
            for child_name, _ in decks.children(deck_id)
            if Deck.DECK_NAME_DELIMITER not in child_name[name_prefix_len:]
        ]

    child_decks = [
        from_collection(
            collection,
            child_name,
            deck.metadata,
            True,
            note_ids,
            deck_note_map=deck_note_map,
            relevant_deck_names=relevant_deck_names,
        )
        for child_name in direct_children
    ]
    deck.children = sorted(
        (child for child in child_decks if child is not None),
        key=lambda child: child.anki_dict["name"],
    )

    if not is_child:
        deduplicate_notes(deck)

    return deck


def remove_tags_from_notes(deck, tags) -> None:
    """Remove tags from all notes in the deck and its children"""
    if deck is None:
        return

    for note in deck.notes:
        note.remove_tags(tags)

    for child in deck.children:
        remove_tags_from_notes(child, tags)


def _is_deck_empty_recursive(deck: Deck) -> bool:
    if deck is None:
        return True  # Treat None as empty

    non_empty_children = []
    for child in deck.children:
        if not _is_deck_empty_recursive(child):
            non_empty_children.append(child)

    deck.children = non_empty_children

    is_empty = not deck.notes and not deck.children
    return is_empty


def trim_empty_children(deck: Optional[Deck]) -> None:
    if deck is None:
        return
    _is_deck_empty_recursive(deck)


def from_json(json_dict, deck_metadata=None) -> Deck:
    """load metadata, load notes, load children"""
    deck = Deck(NoteModelFileProvider, json_dict)
    deck._update_fields()
    deck.metadata = deck_metadata

    if (
        not deck.metadata
    ):  # Todo mental check. The idea is that children don't have metadata
        deck._load_metadata_from_json(json_dict)

    deck.notes = [Note.from_json(json_note) for json_note in json_dict["notes"]]
    deck.children = [
        from_json(child, deck_metadata=deck.metadata) for child in json_dict["children"]
    ]

    deck.post_import_filter()

    if deck_metadata is None:
        deduplicate_notes(deck)

    return deck
