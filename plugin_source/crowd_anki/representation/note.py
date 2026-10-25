import re
import anki
import aqt
import anki.utils
from aqt import mw
from anki.notes import Note as AnkiNote
from anki.utils import is_win, point_version

import logging

from collections import defaultdict

ANKI_INT_VERSION = point_version()
ANKI_VERSION_23_10_00 = 231000
CHUNK_SIZE = 1000
if ANKI_INT_VERSION >= ANKI_VERSION_23_10_00:
    from anki.collection import AddNoteRequest

from ...var_defs import (
    PREFIX_OPTIONAL_TAGS,
    PREFIX_PROTECTED_FIELDS,
    DEFAULT_PROTECTED_TAGS,
)
from .json_serializable import JsonSerializableAnkiObject
from .note_model import NoteModel
from ..config.config_settings import ConfigSettings
from ..utils.constants import UUID_FIELD_NAME

from ...utils import get_logger

logger = get_logger("ankicollab.note")

# from .benchmarking import benchmark, BenchmarkStats
# call with @benchmark before the method you want to benchmark, evaluate on finish with BenchmarkStats.print_stats()


class Note(JsonSerializableAnkiObject):
    export_filter_set = JsonSerializableAnkiObject.export_filter_set | {
        "col",  # Don't need collection
        "_fmap",  # Generated data
        "_model",  # Card model. Would be handled by deck.
        "mid",  # -> uuid
        "scm",  # todo: clarify
        "config",
        "newlyAdded",
    }

    def __init__(self, anki_note=None, config: ConfigSettings = None):
        super(Note, self).__init__(anki_note)
        self.note_model_uuid = None
        self.config = config or ConfigSettings.get_instance()

    @staticmethod
    def get_notes_from_nids(collection, note_models, note_ids):
        return [
            Note.from_collection(collection, note_id, note_models)
            for note_id in note_ids
        ]

    @staticmethod
    def get_notes_from_collection(collection, deck_id, note_models):
        note_ids = collection.decks.get_note_ids(deck_id, include_from_dynamic=True)
        return Note.get_notes_from_nids(collection, note_models, note_ids)

    @classmethod
    def from_collection(cls, collection, note_id, note_models):
        anki_note = AnkiNote(collection, id=note_id)
        note = Note(anki_note=anki_note)

        note_model = NoteModel.from_collection(collection, note.anki_object.mid)
        note_models.setdefault(note_model.get_uuid(), note_model)

        note.note_model_uuid = note_model.get_uuid()

        return note

    @classmethod
    def from_json(cls, json_dict):
        note = Note()
        note.anki_object_dict = json_dict
        note.note_model_uuid = json_dict["note_model_uuid"]
        return note

    def get_uuid(self):
        return (
            self.anki_object.guid
            if self.anki_object
            else self.anki_object_dict.get("guid")
        )

    def note_type(self):
        # TODO Remove compatibility shims for Anki 2.1.46 and lower.
        # (Remove this method altogether — see old version in git
        # history.)
        return (
            self.anki_object.note_type()
            if hasattr(self.anki_object, "note_type")
            else self.anki_object.model()
        )

    @staticmethod
    def _move_notes_to_decks(collection, note_to_deck_map, temp_deck_id):
        """Move NEW notes from the temp deck to their correct decks.

        Updated notes are handled separately. The temp deck itself is removed by
        ``Deck._cleanup_temp_deck``, which refuses to delete a deck that still
        has cards.
        """
        note_uuids = list(note_to_deck_map.keys())
        if not temp_deck_id or not note_uuids:
            return

        temp_deck_ids = [temp_deck_id]
        deck_operations = defaultdict(list)  # deck_id -> [card_ids]

        deck_name_to_id_cache = {}

        # Batch fetch all note and card info for notes currently in temp decks
        for i in range(0, len(note_uuids), CHUNK_SIZE):
            chunk = note_uuids[i : i + CHUNK_SIZE]
            placeholders = ",".join("?" * len(chunk))
            temp_deck_placeholders = ",".join("?" * len(temp_deck_ids))

            try:
                # Only get cards that are currently in temp decks (new notes)
                note_card_data = collection.db.all(
                    f"""
                    SELECT n.guid, n.id, c.id as card_id, c.did, c.odid
                    FROM notes n 
                    LEFT JOIN cards c ON n.id = c.nid 
                    WHERE n.guid IN ({placeholders})
                    AND c.id IS NOT NULL
                    AND c.did IN ({temp_deck_placeholders})
                """,
                    *(chunk + temp_deck_ids),
                )

                for (
                    note_uuid,
                    note_id,
                    card_id,
                    current_deck_id,
                    odid,
                ) in note_card_data:
                    if note_uuid in note_to_deck_map:
                        target_deck_name = note_to_deck_map[note_uuid]

                        # Cache deck ID lookup
                        if target_deck_name not in deck_name_to_id_cache:
                            deck_name_to_id_cache[target_deck_name] = (
                                collection.decks.id(target_deck_name)
                            )
                        target_deck_id = deck_name_to_id_cache[target_deck_name]

                        # Move all cards from temp deck to target deck
                        # (New cards don't need the odid check since they're in temp deck)
                        deck_operations[target_deck_id].append(card_id)

            except Exception as e:
                logger.warning(f"Error fetching cards for note chunk: {e}")
                continue

        for deck_id, all_card_ids in deck_operations.items():
            try:
                for i in range(0, len(all_card_ids), CHUNK_SIZE):
                    chunk = all_card_ids[i : i + CHUNK_SIZE]
                    collection.set_deck(chunk, deck_id)
            except Exception as e:
                logger.warning(f"Error moving cards to deck {deck_id}: {e}")
                continue

    @staticmethod
    def _sync_sibling_suspension_status(collection, notes, cards_before_update):
        """
        Sync suspension status for newly generated cards based on their siblings.

        If all sibling cards of a note are suspended, new cards should be suspended too.
        If siblings are not suspended, new cards remain unsuspended (default behavior).

        Args:
            collection: Anki collection
            notes: List of notes that were updated
            cards_before_update: Dict mapping note_id -> set of card_ids that existed before update
        """
        cards_to_suspend = []

        for note in notes:
            if not note.anki_object:
                continue

            note_id = note.anki_object.id
            current_card_ids = set(note.anki_object.card_ids())
            previous_card_ids = cards_before_update.get(note_id, set())

            # Find newly created cards
            new_card_ids = current_card_ids - previous_card_ids

            if not new_card_ids or not previous_card_ids:
                # No new cards or no previous cards to check suspension status
                continue

            # Check if all previous sibling cards are suspended
            try:
                placeholders = ",".join("?" * len(previous_card_ids))
                sibling_data = collection.db.all(
                    f"SELECT id, queue FROM cards WHERE id IN ({placeholders})",
                    *list(previous_card_ids),
                )

                if not sibling_data:
                    continue

                # queue = -1 means suspended in Anki
                all_siblings_suspended = all(queue == -1 for _, queue in sibling_data)

                if all_siblings_suspended:
                    # All siblings are suspended, so suspend new cards too
                    cards_to_suspend.extend(new_card_ids)
                    logger.debug(
                        f"Note {note_id}: Suspending {len(new_card_ids)} new cards to match suspended siblings"
                    )

            except Exception as e:
                logger.warning(
                    f"Error checking sibling suspension status for note {note_id}: {e}"
                )
                continue

        # Bulk suspend new cards
        if cards_to_suspend:
            try:
                for i in range(0, len(cards_to_suspend), CHUNK_SIZE):
                    chunk = cards_to_suspend[i : i + CHUNK_SIZE]
                    collection.sched.suspend_cards(chunk)
                logger.info(
                    f"Suspended {len(cards_to_suspend)} new cards to match sibling suspension status"
                )
            except Exception as e:
                logger.warning(f"Error suspending new sibling cards: {e}")

    @staticmethod
    def _bulk_update_notes_preserving_placement(
        collection, update_notes, note_to_deck_map, import_config
    ):
        """Update notes while preserving user's manual deck placements"""
        if not update_notes:
            return

        # Track existing cards before update to detect newly generated cards
        cards_before_update = {}  # note_id -> set of card_ids
        for note in update_notes:
            if note.anki_object:
                note_id = note.anki_object.id
                cards_before_update[note_id] = set(note.anki_object.card_ids())

        # First, update all the note content in chunks
        for i in range(0, len(update_notes), CHUNK_SIZE):
            chunk = update_notes[i : i + CHUNK_SIZE]
            collection.update_notes(
                [note.anki_object for note in chunk if note.anki_object]
            )

        # Sync suspension status for newly generated cards based on siblings
        Note._sync_sibling_suspension_status(
            collection, update_notes, cards_before_update
        )

        # Then handle deck movement if not ignored
        if not import_config.ignore_deck_movement:
            deck_groups = defaultdict(list)  # target_deck_id -> [card_ids]
            cards_to_update = []

            # Collect all card IDs we need to check
            all_card_ids = []
            card_to_note_uuid = {}  # card_id -> note_uuid

            for note in update_notes:
                note_uuid = note.get_uuid()
                if note_uuid not in note_to_deck_map:
                    continue

                card_ids = note.anki_object.card_ids()
                all_card_ids.extend(card_ids)
                for card_id in card_ids:
                    card_to_note_uuid[card_id] = note_uuid

            if not all_card_ids:
                return

            deck_name_to_id_cache = {}

            for i in range(0, len(all_card_ids), CHUNK_SIZE):
                chunk = all_card_ids[i : i + CHUNK_SIZE]
                placeholders = ",".join("?" * len(chunk))

                try:
                    # Get card deck info in a single query
                    card_data = collection.db.all(
                        f"SELECT id, did, odid FROM cards WHERE id IN ({placeholders})",
                        *chunk,
                    )

                    for card_id, current_deck_id, odid in card_data:
                        note_uuid = card_to_note_uuid.get(card_id)
                        if not note_uuid or note_uuid not in note_to_deck_map:
                            continue

                        target_deck_name = note_to_deck_map[note_uuid]

                        # Cache deck ID lookup
                        if target_deck_name not in deck_name_to_id_cache:
                            deck_name_to_id_cache[target_deck_name] = (
                                collection.decks.id(target_deck_name)
                            )
                        target_deck_id = deck_name_to_id_cache[target_deck_name]

                        # Normal cards moved properly with set_deck; filtered cards get a ghetto update of odid and we pray nothing breaks down the road with the scheduler
                        if odid != 0:
                            if odid != target_deck_id:
                                card = collection.get_card(card_id)
                                card.odid = target_deck_id
                                cards_to_update.append(card)
                        elif current_deck_id != target_deck_id:
                            deck_groups[target_deck_id].append(card_id)

                except Exception as e:
                    logger.warning(f"Error fetching card data for chunk: {e}")
                    continue

            # Move cards grouped by deck
            for deck_id, card_ids_for_deck in deck_groups.items():
                try:
                    for i in range(0, len(card_ids_for_deck), CHUNK_SIZE):
                        chunk = card_ids_for_deck[i : i + CHUNK_SIZE]
                        collection.set_deck(chunk, deck_id)
                except Exception as e:
                    logger.warning(f"Error moving cards to deck {deck_id}: {e}")

            if cards_to_update:
                try:
                    for i in range(0, len(cards_to_update), CHUNK_SIZE):
                        chunk = cards_to_update[i : i + CHUNK_SIZE]
                        collection.update_cards(chunk)
                except Exception as e:
                    logger.warning(f"Error updating filtered card odid values: {e}")

    @staticmethod
    def bulk_add_notes(collection, notes, deck_id, import_config):
        CHUNK_SIZE = 1000
        if ANKI_INT_VERSION >= ANKI_VERSION_23_10_00:
            for i in range(0, len(notes), CHUNK_SIZE):
                chunk = notes[i : i + CHUNK_SIZE]
                requests = [
                    AddNoteRequest(note.anki_object, deck_id=deck_id)
                    for note in chunk
                    if note.anki_object
                ]
                collection.add_notes(requests)
        else:
            for i in range(0, len(notes), CHUNK_SIZE):
                chunk = notes[i : i + CHUNK_SIZE]
                for note in chunk:
                    if note.anki_object:
                        collection.add_note(note.anki_object, deck_id)

        if import_config and import_config.suspend_new_cards:
            cards_to_suspend = []
            for note in notes:
                cards_to_suspend.extend(note.anki_object.card_ids())
            if cards_to_suspend:
                for i in range(0, len(cards_to_suspend), CHUNK_SIZE):
                    chunk = cards_to_suspend[i : i + CHUNK_SIZE]
                    collection.sched.suspend_cards(chunk)

    def handle_import_config_changes(self, import_config, note_model, is_new=False):
        """
        Handle import configuration changes (protected fields, personal fields, tags).

        Local values are preserved by looking them up by field *name* in the
        note's current notetype. Note values are positional and the notetype
        update already remapped every existing note, so a merge-time index
        mapping must never be applied to them here.

        Protection only applies to values the note already had: a brand-new
        note (or a field the remote merge introduced) has no user value, so it
        takes the imported value instead of being blanked.

        Args:
            import_config: Import configuration object
            note_model: The note model for this note
            is_new: True when the note is being added, not updated
        """
        if (
            not hasattr(self, "anki_object_dict")
            or not self.anki_object_dict
            or not hasattr(self.anki_object, "fields")
        ):
            return

        # Cache field names and indices for faster lookup
        field_name_to_index = {
            field["name"]: idx for idx, field in enumerate(note_model.anki_dict["flds"])
        }
        field_name_to_index_lower = {
            name.lower(): idx for name, idx in field_name_to_index.items()
        }

        old_field_name_to_index = {}
        if self.anki_object:
            try:
                old_model = (
                    self.anki_object.note_type()
                    if hasattr(self.anki_object, "note_type")
                    else self.anki_object.model()
                )
                if old_model and "flds" in old_model:
                    old_field_name_to_index = {
                        f["name"]: idx for idx, f in enumerate(old_model["flds"])
                    }
            except Exception as e:
                logger.warning(f"Could not get old model fields: {e}")

        # Single invariant for every local-value read below: a value can only
        # come from a position the note actually stores. A note whose field
        # count disagrees with its notetype (loaded before a notetype change, a
        # short `flds` row) exposes only the values it has; the remaining
        # positions resolve to "" instead of raising or reading a stale offset.
        local_fields = self.anki_object.fields if self.anki_object else []
        if old_field_name_to_index and len(local_fields) != len(
            old_field_name_to_index
        ):
            logger.warning(
                f"Note has {len(local_fields)} field values but its notetype "
                f"defines {len(old_field_name_to_index)} fields - only the values "
                f"present can be preserved"
            )

        local_values = {
            name: local_fields[idx]
            for name, idx in old_field_name_to_index.items()
            if idx < len(local_fields)
        }

        # The note's notetype can spell a field differently than the merged one.
        # An exact match always wins; a case-variant is only used when it is
        # unambiguous (a malformed notetype could contain both spellings).
        local_values_lower = {}
        for name, value in local_values.items():
            lowered = name.lower()
            local_values_lower[lowered] = (
                None if lowered in local_values_lower else value
            )

        def local_value(field_name):
            """The note's own value for ``field_name``, or None if it has none."""
            if field_name in local_values:
                return local_values[field_name]
            return local_values_lower.get(field_name.lower())

        # A note loaded from disk has already been remapped/padded by the
        # notetype merge, so "empty" alone cannot distinguish "the user's value
        # is empty" from "the remote added this field". The notetype keeps the
        # pre-merge names for exactly that distinction. ``None`` means the
        # model did not come through the import path; preserve as before.
        known_before_merge = note_model.local_field_names_before_merge

        def should_preserve(field_name):
            if is_new:
                return False
            if known_before_merge is None:
                return True
            if field_name.lower() in known_before_merge:
                return True
            # Not part of the local notetype before the merge. This is either a
            # field the remote introduced (the note is padded with "") or a
            # value a notetype migration brought over. Keep real local content;
            # let padding be replaced by the imported value.
            value = local_value(field_name)
            return value not in (None, "")

        # Get protected fields set by maintainer (indices in NEW structure)
        max_fields = min(
            len(self.anki_object_dict["fields"]), len(note_model.anki_dict["flds"])
        )

        # use note_model original_name if it exists, otherwise use note_model name
        note_model_name = note_model.anki_dict.get(
            "original_name", note_model.anki_dict["name"]
        )
        protected_fields = [
            num
            for num in range(max_fields)
            if import_config.is_personal_field(
                note_model_name, note_model.anki_dict["flds"][num]["name"]
            )
        ]

        logger.debug("Handling import config changes for note")

        # Anki stores field values positionally. Rebuild the list against the
        # merged notetype: remote fields keep the incoming value, while only
        # genuinely local fields take the user's own value. Extra fields a
        # sender added locally must never leak into ours (and vice versa).
        local_only = {name.lower() for name in note_model.local_only_field_names}
        remote_values = self.anki_object_dict["fields"]
        fields = []
        for index, field in enumerate(note_model.anki_dict["flds"]):
            lower_name = field["name"].lower()
            if lower_name in local_only:
                value = local_value(field["name"])
                if value is None:
                    # Out-of-range positions are covered by the up-front warning;
                    # a name with no exact local counterpart is worth its own line.
                    if field["name"] not in old_field_name_to_index:
                        logger.warning(
                            f"Local-only field '{field['name']}' has no unambiguous "
                            f"local counterpart - importing it empty"
                        )
                    value = ""
                fields.append(value)
            elif index < len(remote_values):
                fields.append(remote_values[index])
            else:
                fields.append("")
        self.anki_object_dict["fields"] = fields

        logger.debug(
            f"Local fields processed - final count: {len(self.anki_object_dict['fields'])}"
        )
        # Override protected fields with OLD content (maintainer wants to preserve old values)
        # This overrides the NEW remote content for specific fields the maintainer marked as protected
        if protected_fields:
            for new_field_idx in protected_fields:
                try:
                    field_name = note_model.anki_dict["flds"][new_field_idx]["name"]
                    if not should_preserve(field_name):
                        logger.debug(
                            f"Protected field '{field_name}' has no local value "
                            f"to preserve - keeping the imported value"
                        )
                        continue
                    value = local_value(field_name)
                    if value is None:
                        logger.warning(
                            f"Could not find local value for protected field '{field_name}' (new_idx={new_field_idx})"
                        )
                        continue
                    # Keep the old content for this protected field
                    self.anki_object_dict["fields"][new_field_idx] = value
                    logger.debug(
                        f"Maintainer protected field '{field_name}' (new_idx={new_field_idx}): preserved content"
                    )
                except (IndexError, TypeError) as e:
                    logger.warning(
                        f"Error preserving protected field {new_field_idx}: {e}"
                    )
                    continue

        logger.debug(f"Protected fields handled: {protected_fields}")

        protected_fields_by_tag = []
        if self.anki_object and hasattr(self.anki_object, "tags"):
            protected_fields_by_tag = [
                tag
                for tag in self.anki_object.tags
                if tag.startswith(PREFIX_PROTECTED_FIELDS)
            ]

        for tag in protected_fields_by_tag:
            # Ensure tags list exists and protected tags are preserved
            if "tags" not in self.anki_object_dict:
                self.anki_object_dict["tags"] = []
            if tag not in self.anki_object_dict["tags"]:
                self.anki_object_dict["tags"].append(tag)

            if not self.anki_object:
                continue  # broken note?

            protected_field = tag.split("::", 1)[1]

            if (protected_field == "Tags" or protected_field == "All") and hasattr(
                self.anki_object, "tags"
            ):
                if not is_new:
                    self.anki_object_dict["tags"] = list(
                        self.anki_object.tags
                    )  # Copy to avoid reference issues

            if protected_field == "All":
                # If "All" is protected, copy all fields AND tags from anki_object to anki_object_dict
                for new_idx, field in enumerate(note_model.anki_dict["flds"]):
                    if not should_preserve(field["name"]):
                        continue
                    value = local_value(field["name"])
                    if value is not None:
                        self.anki_object_dict["fields"][new_idx] = value
                break

            # Handle individual field protection
            field_idx = field_name_to_index.get(protected_field)
            if field_idx is None:
                field_idx = field_name_to_index_lower.get(protected_field.lower())
            if field_idx is None:
                field_idx = field_name_to_index.get(protected_field.replace("_", " "))
            if field_idx is None:
                field_idx = field_name_to_index_lower.get(
                    protected_field.replace("_", " ").lower()
                )
            if field_idx is not None:
                field_name = note_model.anki_dict["flds"][field_idx]["name"]
                if not should_preserve(field_name):
                    continue
                value = local_value(field_name)
                if value is None:
                    logger.warning(
                        f"Could not find local value for user protected field '{protected_field}'"
                    )
                    continue
                # Preserve the old content for this protected field
                self.anki_object_dict["fields"][field_idx] = value
                logger.debug(
                    f"User protected field '{protected_field}' (new_idx={field_idx}): preserved content"
                )

        logger.debug(f"Protected fields by tags handled")

        protected_prefixes = import_config.personal_tags
        protected_prefixes_lower = {
            prefix.lower() for prefix in protected_prefixes if isinstance(prefix, str)
        }
        protected_tags = []
        if self.anki_object and hasattr(self.anki_object, "tags"):
            protected_tags = [
                tag
                for tag in self.anki_object.tags
                if (
                    tag.lower() in protected_prefixes_lower
                    or any(
                        tag.lower().startswith(f"{prefix}::")
                        for prefix in protected_prefixes_lower
                    )
                )
            ]

        # Merge preserved personal tags (leech, marked, missing-media, etc.) back into tags
        # These were on the local note and should be kept even though the server doesn't have them
        if protected_tags:
            if "tags" not in self.anki_object_dict:
                self.anki_object_dict["tags"] = []
            for tag in protected_tags:
                if tag not in self.anki_object_dict["tags"]:
                    self.anki_object_dict["tags"].append(tag)
            logger.debug(f"Preserved personal protected tags: {protected_tags}")

        if import_config.has_optional_tags and "tags" in self.anki_object_dict:
            self.anki_object_dict["tags"] = [
                tag
                for tag in self.anki_object_dict["tags"]
                if not tag.startswith(PREFIX_OPTIONAL_TAGS)
                or tag.split("::", 1)[1] in import_config.optional_tags
            ]

        if (
            self.anki_object
            and hasattr(self.anki_object, "fields")
            and len(self.anki_object_dict.get("fields", []))
            != len(self.anki_object.fields)
        ):
            logger.debug(
                f"Field count difference between remote and local note: {len(self.anki_object_dict['fields'])} vs {len(self.anki_object.fields)} (this is normal for custom fields)"
            )

        # check if anki_object_dict["fields"] has the same number of fields as note_model.fields
        if len(self.anki_object_dict.get("fields", [])) != len(
            note_model.anki_dict["flds"]
        ) or (
            self.anki_object
            and hasattr(self.anki_object, "fields")
            and len(self.anki_object.fields) != len(note_model.anki_dict["flds"])
        ):
            remote_count = len(self.anki_object_dict["fields"])
            notetype_count = len(note_model.anki_dict["flds"])
            old_count = (
                len(self.anki_object.fields)
                if self.anki_object and hasattr(self.anki_object, "fields")
                else 0
            )
            if remote_count > notetype_count:
                logger.debug(
                    f"Remote note has {remote_count - notetype_count} extra fields beyond notetype definition - treating as custom fields"
                )
            elif remote_count < notetype_count:
                logger.warning(
                    f"Remote note missing {notetype_count - remote_count} fields expected by notetype"
                )
            elif old_count != notetype_count:
                logger.warning(
                    f"Old note has {old_count - notetype_count} extra fields beyond notetype definition - treating as custom fields"
                )
            elif old_count < notetype_count:
                logger.warning(
                    f"Old note missing {notetype_count - old_count} fields expected by notetype"
                )
            else:
                logger.debug("Remote note fields match notetype definition")
            logger.debug(f"Note model name: {note_model.anki_dict['name']}")
            logger.debug(f"Note model fields: {note_model.anki_dict['flds']}")
            old_fields = (
                self.anki_object.fields
                if self.anki_object and hasattr(self.anki_object, "fields")
                else []
            )
            logger.debug(f"Old Note fields: {old_fields}")
            logger.debug(f"New Note fields: {self.anki_object_dict['fields']}")
            logger.debug(f"Note UUID: {self.get_uuid()}")

        logger.debug(
            f"Note {self.get_uuid()} handled import config changes successfully"
        )

    def remove_tags(
        self, tags
    ):  # Option to remove personal tags from notes before uploading them
        if not self.anki_object or not hasattr(self.anki_object, "tags"):
            return

        for personal_tag in tags:
            # Remove exact matches
            if personal_tag in self.anki_object.tags:
                self.anki_object.tags.remove(personal_tag)
            # Remove tags that start with the personal_tag prefix (hierarchical tags)
            self.anki_object.tags = [
                tag
                for tag in self.anki_object.tags
                if not tag.startswith(f"{personal_tag}::")
            ]

        # Remove any tags that are just whitespace
        self.anki_object.tags = [tag for tag in self.anki_object.tags if tag.strip()]
