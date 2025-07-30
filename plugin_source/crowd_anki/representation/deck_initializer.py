from typing import Optional
from functional import seq

from .deck import Deck
from .note import Note
from ..anki.adapters.anki_deck import AnkiDeck
from ..anki.adapters.note_model_file_provider import NoteModelFileProvider

import anki.utils
from anki.decks import DeckManager


def get_card_ids(self, did, children=False, include_from_dynamic=False):
    deck_ids = [did] + ([deck_id for _, deck_id in self.children(did)] if children else [])

    request = "select id from cards where did in {}" + ("or odid in {}" if include_from_dynamic else "")
    parameters = (anki.utils.ids2str(deck_ids),) + ((anki.utils.ids2str(deck_ids),)
                                                    if include_from_dynamic else tuple())

    return self.col.db.list(request.format(*parameters))


def get_note_ids(self, deck_id, children=False, include_from_dynamic=False):
    card_ids_str = anki.utils.ids2str(self.get_card_ids(deck_id, children, include_from_dynamic))
    request = "SELECT DISTINCT nid FROM cards WHERE id IN " + card_ids_str
    return self.col.db.list(request)

DeckManager.get_card_ids = get_card_ids
DeckManager.get_note_ids = get_note_ids

def from_collection(collection, name, deck_metadata=None, is_child=False, note_ids=None) -> Deck:
    """load metadata, load notes, load children"""
    decks = collection.decks
    by_name = decks.by_name
    anki_dict = by_name(name)

    if anki_dict is None:
        return None
    
    if AnkiDeck(anki_dict).is_dynamic:
        return None

    deck = Deck(NoteModelFileProvider, anki_dict, is_child)
    deck.collection = collection
    deck._update_fields()
    deck.metadata = deck_metadata
    deck._load_metadata()

    note_ids_to_load = note_ids # If we bulk suggest Notes, we know the nids beforehand
    
    if note_ids_to_load is None: # If we don't know the nids, we have to load all notes
        note_ids_to_load = collection.decks.get_note_ids(deck.anki_dict["id"], include_from_dynamic=True)
    else: # If we know the nids, we have to filter out the ones that are not in the deck to prevent duplicates and wrong deck assignments
        note_ids_to_load = [note_id for note_id in note_ids_to_load if note_id in collection.decks.get_note_ids(deck.anki_dict["id"], include_from_dynamic=True)]
    
    # Finally load the notes
    if note_ids_to_load:
        deck.notes = Note.get_notes_from_nids(collection, deck.metadata.models, note_ids_to_load)
    else:
        deck.notes = []
            
    direct_children = [child_name for child_name, _ in decks.children(deck.anki_dict["id"])
                       if Deck.DECK_NAME_DELIMITER
                       not in child_name[len(name) + len(Deck.DECK_NAME_DELIMITER):]]

    deck.children = seq(direct_children) \
        .map(lambda child_name: from_collection(collection, child_name, deck.metadata, True, note_ids)) \
        .filter(lambda it: it is not None).order_by(lambda x: x.anki_dict["name"]).to_list()

    return deck

def remove_unchanged_notes(deck, timestamp, timestamp2) -> None:
    """Remove notes that have not been changed since the last sync"""
    if deck is None:
        return
    
    deck.notes = [note for note in deck.notes if note.anki_object.mod > timestamp or note.anki_object.mod > timestamp2]
    
    for child in deck.children:
        remove_unchanged_notes(child, timestamp, timestamp2)

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
        return True # Treat None as empty

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

    if not deck.metadata:  # Todo mental check. The idea is that children don't have metadata
        deck._load_metadata_from_json(json_dict)

    deck.notes = [Note.from_json(json_note) for json_note in json_dict["notes"]]
    deck.children = [from_json(child, deck_metadata=deck.metadata) for child in json_dict["children"]]
    
    deck.post_import_filter()

    return deck
