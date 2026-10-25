"""Remove duplicate note occurrences from a deck tree.

Anki tracks deck membership per *card*, while AnkiCollab tracks it per *note*.
A note whose cards live in several decks therefore appears in several deck
objects when the tree is built. Exporting that tree as-is serializes the same
note once per deck; the server stores one note row per deck and a later import
creates one note (and all of its cards) for every occurrence.

The fix is deliberately at the tree level: whichever occurrence is visited
first wins (parent before child, children in their sorted order), and every
later occurrence is dropped. This is independent of how the per-deck note lists
were produced and keeps the note's full card set in the deck we assign it to.
"""

from __future__ import annotations

from ...utils import get_logger

logger = get_logger("ankicollab.note_dedup")


def deduplicate_notes(root) -> int:
    """Keep exactly one occurrence of every note GUID in ``root``'s tree.

    Notes without a GUID (malformed data) are kept rather than collapsing all
    of them into one. Returns the number of removed duplicates.
    """
    if root is None:
        return 0

    first_deck_by_uuid = {}
    removed = 0

    def walk(deck):
        nonlocal removed
        deck_name = deck.anki_dict.get("name", "unknown deck")
        unique = []
        for note in deck.notes:
            note_uuid = note.get_uuid()
            if not note_uuid:
                unique.append(note)
                continue
            if note_uuid in first_deck_by_uuid:
                removed += 1
                logger.debug(
                    f"Duplicate note {note_uuid} in deck '{deck_name}' ignored; "
                    f"it was already exported in "
                    f"'{first_deck_by_uuid[note_uuid]}'"
                )
                continue
            first_deck_by_uuid[note_uuid] = deck_name
            unique.append(note)
        deck.notes = unique
        for child in deck.children:
            walk(child)

    walk(root)
    if removed:
        logger.warning(
            f"Removed {removed} duplicate note occurrence(s) from the deck tree "
            f"(notes with cards in multiple decks are exported once)"
        )
    return removed
