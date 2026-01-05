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
    
from ...var_defs import PREFIX_OPTIONAL_TAGS, PREFIX_PROTECTED_FIELDS, DEFAULT_PROTECTED_TAGS
from .json_serializable import JsonSerializableAnkiObject
from .note_model import NoteModel
from ..config.config_settings import ConfigSettings
from ..utils.constants import UUID_FIELD_NAME

from ...utils import get_logger

logger = get_logger("ankicollab.note")

#from .benchmarking import benchmark, BenchmarkStats
# call with @benchmark before the method you want to benchmark, evaluate on finish with BenchmarkStats.print_stats()

class Note(JsonSerializableAnkiObject):
    export_filter_set = JsonSerializableAnkiObject.export_filter_set | \
                        {"col",  # Don't need collection
                         "_fmap",  # Generated data
                         "_model",  # Card model. Would be handled by deck.
                         "mid",  # -> uuid
                         "scm",  # todo: clarify
                         "config",
                         "newlyAdded"
                         }

    def __init__(self, anki_note=None, config: ConfigSettings = None):
        super(Note, self).__init__(anki_note)
        self.note_model_uuid = None
        self.config = config or ConfigSettings.get_instance()

    @staticmethod
    def get_notes_from_nids(collection, note_models, note_ids):
        return [Note.from_collection(collection, note_id, note_models) for note_id in note_ids]
    
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
        return self.anki_object.guid if self.anki_object else self.anki_object_dict.get("guid")

    def note_type(self):
        # TODO Remove compatibility shims for Anki 2.1.46 and lower.
        # (Remove this method altogether — see old version in git
        # history.)
        return self.anki_object.note_type() if hasattr(self.anki_object, 'note_type') else self.anki_object.model()

    @staticmethod
    def _move_notes_to_decks(collection, note_to_deck_map, import_config):
        """Move NEW notes from temp deck to their correct decks (updated notes handled separately)"""

        # Only move notes that are currently in temp decks (these are new notes)
        temp_deck_pattern = "_ankicollab_import_"
        temp_deck_ids = []
        
        try:
            for deck_name_id in collection.decks.all_names_and_ids():
                deck_name = deck_name_id.name
                deck_id = deck_name_id.id
                if deck_name.startswith(temp_deck_pattern):
                    temp_deck_ids.append(deck_id)
        except Exception as e:
            logger.warning(f"Error finding temp decks: {e}")
            return
            
        if not temp_deck_ids:
            return
            
        deck_operations = defaultdict(list)  # deck_id -> [card_ids]
        
        note_uuids = list(note_to_deck_map.keys())
        if not note_uuids:
            return
        
        deck_name_to_id_cache = {}
            
        # Batch fetch all note and card info for notes currently in temp decks
        for i in range(0, len(note_uuids), CHUNK_SIZE):
            chunk = note_uuids[i:i+CHUNK_SIZE]
            placeholders = ','.join('?' * len(chunk))
            temp_deck_placeholders = ','.join('?' * len(temp_deck_ids))
            
            try:
                # Only get cards that are currently in temp decks (new notes)
                note_card_data = collection.db.all(f"""
                    SELECT n.guid, n.id, c.id as card_id, c.did, c.odid
                    FROM notes n 
                    LEFT JOIN cards c ON n.id = c.nid 
                    WHERE n.guid IN ({placeholders})
                    AND c.id IS NOT NULL
                    AND c.did IN ({temp_deck_placeholders})
                """, *(chunk + temp_deck_ids))
                
                for note_uuid, note_id, card_id, current_deck_id, odid in note_card_data:
                    if note_uuid in note_to_deck_map:
                        target_deck_name = note_to_deck_map[note_uuid]
                        
                        # Cache deck ID lookup
                        if target_deck_name not in deck_name_to_id_cache:
                            deck_name_to_id_cache[target_deck_name] = collection.decks.id(target_deck_name)
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
                    chunk = all_card_ids[i:i + CHUNK_SIZE]
                    collection.set_deck(chunk, deck_id)
            except Exception as e:
                logger.warning(f"Error moving cards to deck {deck_id}: {e}")
                continue
        
        # Clean up temp deck
        try:
            temp_deck_pattern = "_ankicollab_import_"
            deck_ids_to_remove = []
            
            for deck_name_id in collection.decks.all_names_and_ids():
                deck_name = deck_name_id.name
                deck_id = deck_name_id.id
                if deck_name.startswith(temp_deck_pattern):
                    deck_ids_to_remove.append(deck_id)
            
            if deck_ids_to_remove:
                collection.decks.remove(deck_ids_to_remove)
        except Exception as e:
            logger.warning(f"Error cleaning up temp deck: {e}")
    
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
                placeholders = ','.join('?' * len(previous_card_ids))
                sibling_data = collection.db.all(
                    f"SELECT id, queue FROM cards WHERE id IN ({placeholders})",
                    *list(previous_card_ids)
                )
                
                if not sibling_data:
                    continue
                
                # queue = -1 means suspended in Anki
                all_siblings_suspended = all(queue == -1 for _, queue in sibling_data)
                
                if all_siblings_suspended:
                    # All siblings are suspended, so suspend new cards too
                    cards_to_suspend.extend(new_card_ids)
                    logger.debug(f"Note {note_id}: Suspending {len(new_card_ids)} new cards to match suspended siblings")
                    
            except Exception as e:
                logger.warning(f"Error checking sibling suspension status for note {note_id}: {e}")
                continue
        
        # Bulk suspend new cards
        if cards_to_suspend:
            try:
                for i in range(0, len(cards_to_suspend), CHUNK_SIZE):
                    chunk = cards_to_suspend[i:i + CHUNK_SIZE]
                    collection.sched.suspend_cards(chunk)
                logger.info(f"Suspended {len(cards_to_suspend)} new cards to match sibling suspension status")
            except Exception as e:
                logger.warning(f"Error suspending new sibling cards: {e}")
    
    @staticmethod
    def _bulk_update_notes_preserving_placement(collection, update_notes, note_to_deck_map, import_config):
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
            chunk = update_notes[i:i + CHUNK_SIZE]
            collection.update_notes([note.anki_object for note in chunk if note.anki_object])
        
        # Sync suspension status for newly generated cards based on siblings
        Note._sync_sibling_suspension_status(collection, update_notes, cards_before_update)
        
        # Then handle deck movement if not ignored
        if not import_config.ignore_deck_movement:
            deck_groups = defaultdict(list)  # target_deck_id -> [card_ids]
            
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
                chunk = all_card_ids[i:i + CHUNK_SIZE]
                placeholders = ','.join('?' * len(chunk))
                
                try:
                    # Get card deck info in a single query
                    card_data = collection.db.all(
                        f"SELECT id, did, odid FROM cards WHERE id IN ({placeholders})",
                        *chunk
                    )
                    
                    for card_id, current_deck_id, odid in card_data:
                        note_uuid = card_to_note_uuid.get(card_id)
                        if not note_uuid or note_uuid not in note_to_deck_map:
                            continue
                        
                        target_deck_name = note_to_deck_map[note_uuid]
                        
                        # Cache deck ID lookup
                        if target_deck_name not in deck_name_to_id_cache:
                            deck_name_to_id_cache[target_deck_name] = collection.decks.id(target_deck_name)
                        target_deck_id = deck_name_to_id_cache[target_deck_name]
                        
                        # Only move if not already in target and not in filtered deck
                        if current_deck_id != target_deck_id and odid == 0:
                            deck_groups[target_deck_id].append(card_id)
                            
                except Exception as e:
                    logger.warning(f"Error fetching card data for chunk: {e}")
                    continue
            
            # Move cards grouped by deck
            for deck_id, card_ids_for_deck in deck_groups.items():
                try:
                    for i in range(0, len(card_ids_for_deck), CHUNK_SIZE):
                        chunk = card_ids_for_deck[i:i + CHUNK_SIZE]
                        collection.set_deck(chunk, deck_id)
                except Exception as e:
                    logger.warning(f"Error moving cards to deck {deck_id}: {e}")
        
    @staticmethod
    def bulk_add_notes(collection, notes, deck_id, import_config):
        CHUNK_SIZE = 1000
        if ANKI_INT_VERSION >= ANKI_VERSION_23_10_00:
            for i in range(0, len(notes), CHUNK_SIZE):
                chunk = notes[i:i + CHUNK_SIZE]
                requests = [AddNoteRequest(note.anki_object, deck_id=deck_id) for note in chunk if note.anki_object]
                collection.add_notes(requests)
        else:
            for i in range(0, len(notes), CHUNK_SIZE):
                chunk = notes[i:i + CHUNK_SIZE]
                for note in chunk:
                    if note.anki_object:
                        collection.add_note(note.anki_object, deck_id)

        if import_config and import_config.suspend_new_cards:
            cards_to_suspend = []
            for note in notes:
                cards_to_suspend.extend(note.anki_object.card_ids())
            if cards_to_suspend:
                for i in range(0, len(cards_to_suspend), CHUNK_SIZE):
                    chunk = cards_to_suspend[i:i + CHUNK_SIZE]
                    collection.sched.suspend_cards(chunk)              
        
    def handle_import_config_changes(self, import_config, note_model, field_mapping=None):
        """
        Handle import configuration changes including field mapping awareness.
        
        Args:
            import_config: Import configuration object
            note_model: The note model for this note
            field_mapping: List mapping new field indices to old field indices
        """
        if not hasattr(self, 'anki_object_dict') or not self.anki_object_dict or not hasattr(self.anki_object, 'fields'):
            return
        
        logger.debug(f"Field Mapping: {field_mapping}")
            
        # Cache field names and indices for faster lookup
        field_name_to_index = {
            field['name']: idx 
            for idx, field in enumerate(note_model.anki_dict['flds'])
        }
        
        # Get protected fields set by maintainer (indices in NEW structure)
        max_fields = min(len(self.anki_object_dict["fields"]), len(note_model.anki_dict['flds']))
        
        # use note_model original_name if it exists, otherwise use note_model name
        note_model_name = note_model.anki_dict.get('original_name', note_model.anki_dict['name'])
        protected_fields = [
            num for num in range(max_fields)
            if import_config.is_personal_field(note_model_name, 
                                            note_model.anki_dict['flds'][num]['name'])
        ]
        
        logger.debug("Handling import config changes for note")
        # Add local-only fields to anki_object_dict (user's custom fields at the bottom)
        if (field_mapping and self.anki_object and hasattr(self.anki_object, 'fields') and 
            len(self.anki_object.fields) > len(self.anki_object_dict["fields"])):
            start_index = len(self.anki_object_dict["fields"])
            # Only add the additional local fields that aren't in the remote data
            for new_field_idx in range(start_index, len(self.anki_object.fields)):
                if (new_field_idx < len(field_mapping) and 
                        field_mapping[new_field_idx] is not None and 
                        field_mapping[new_field_idx] < len(self.anki_object.fields)):
                        old_field_idx = field_mapping[new_field_idx]
                        # Keep the old content for this protected field
                        self.anki_object_dict["fields"].append(self.anki_object.fields[old_field_idx])
        
        # CRITICAL: Ensure field count matches notetype definition exactly
        # Anki's update_notes requires this strict matching
        expected_field_count = len(note_model.anki_dict['flds'])
        current_field_count = len(self.anki_object_dict["fields"])
        
        if current_field_count > expected_field_count:
            # Truncate extra fields that exceed notetype definition
            logger.debug(f"Truncating {current_field_count - expected_field_count} extra fields to match notetype definition")
            self.anki_object_dict["fields"] = self.anki_object_dict["fields"][:expected_field_count]
        elif current_field_count < expected_field_count:
            # Pad with empty fields if we're missing some
            logger.debug(f"Padding {expected_field_count - current_field_count} missing fields")
            while len(self.anki_object_dict["fields"]) < expected_field_count:
                self.anki_object_dict["fields"].append("")
                
        logger.debug(f"Local fields processed - final count: {len(self.anki_object_dict['fields'])}")
        # Override protected fields with OLD content (maintainer wants to preserve old values)
        # This overrides the NEW remote content for specific fields the maintainer marked as protected
        if field_mapping and protected_fields and self.anki_object and hasattr(self.anki_object, 'fields'):
            for new_field_idx in protected_fields:
                try:
                    # For protected fields, we want to keep the OLD value, not the new remote value
                    if (new_field_idx < len(field_mapping) and 
                        field_mapping[new_field_idx] is not None and 
                        field_mapping[new_field_idx] < len(self.anki_object.fields)):
                        old_field_idx = field_mapping[new_field_idx]
                        # Keep the old content for this protected field
                        self.anki_object_dict["fields"][new_field_idx] = self.anki_object.fields[old_field_idx]
                    else:
                        logger.warning(f"Invalid field mapping for protected field {new_field_idx}: mapping={field_mapping}")
                except (IndexError, TypeError) as e:
                    logger.warning(f"Error accessing field mapping for protected field {new_field_idx}: {e}")
                    continue

        logger.debug(f"Protected fields handled: {protected_fields}")
        
        protected_tags = []
        if self.anki_object and hasattr(self.anki_object, 'tags'):
            protected_tags = [
                tag for tag in self.anki_object.tags 
                if tag.startswith(PREFIX_PROTECTED_FIELDS)
            ]
        
        # Preserve DEFAULT_PROTECTED_TAGS (leech, marked, missing-media) from local note
        # These tags are stripped during export but should be kept during import updates
        preserved_default_tags = []
        if self.anki_object and hasattr(self.anki_object, 'tags'):
            preserved_default_tags = [
                tag for tag in self.anki_object.tags
                if tag in DEFAULT_PROTECTED_TAGS or 
                   any(tag.startswith(f"{pt}::") for pt in DEFAULT_PROTECTED_TAGS)
            ]
        
        for tag in protected_tags:
            # Ensure tags list exists and protected tags are preserved
            if 'tags' not in self.anki_object_dict:
                self.anki_object_dict['tags'] = []
            if tag not in self.anki_object_dict["tags"]:
                self.anki_object_dict["tags"].append(tag)
                
            protected_field = tag.split('::', 1)[1]
            
            if protected_field == "Tags" and self.anki_object and hasattr(self.anki_object, 'tags'):
                self.anki_object_dict["tags"] = list(self.anki_object.tags)  # Copy to avoid reference issues
                
            if protected_field == "All":
                # If "All" is protected, copy all fields AND tags from anki_object to anki_object_dict
                if self.anki_object and hasattr(self.anki_object, 'fields'):
                    if field_mapping:
                        # Use field mapping when available
                        for new_idx, old_idx in enumerate(field_mapping):
                            if (old_idx is not None and 
                                new_idx < len(self.anki_object_dict["fields"]) and
                                old_idx < len(self.anki_object.fields)):
                                self.anki_object_dict["fields"][new_idx] = self.anki_object.fields[old_idx]
                    else:
                        # Fallback: no field mapping, copy fields by position (same structure)
                        for idx in range(min(len(self.anki_object_dict["fields"]), len(self.anki_object.fields))):
                            self.anki_object_dict["fields"][idx] = self.anki_object.fields[idx]
                break
                
            # Handle individual field protection using field mapping
            field_idx = field_name_to_index.get(protected_field) or \
                    field_name_to_index.get(protected_field.replace('_', ' '))
            if (field_idx is not None and self.anki_object and 
                hasattr(self.anki_object, 'fields')):
                
                # For user-protected fields, use the same field mapping logic as maintainer-protected fields
                if field_mapping and field_idx < len(field_mapping):
                    old_field_idx = field_mapping[field_idx]
                    if (old_field_idx is not None and 
                        old_field_idx < len(self.anki_object.fields) and
                        field_idx < len(self.anki_object_dict['fields'])):
                        # Preserve the old content for this protected field
                        old_content = self.anki_object.fields[old_field_idx]
                        self.anki_object_dict["fields"][field_idx] = old_content
                        logger.debug(f"User protected field '{protected_field}' (new_idx={field_idx}, old_idx={old_field_idx}): preserved content")
                    else:
                        logger.warning(f"Invalid field mapping for user protected field '{protected_field}': field_idx={field_idx}, old_field_idx={old_field_idx}")
                else:
                    # Fallback: no field mapping available, use same position
                    if field_idx < len(self.anki_object.fields) and field_idx < len(self.anki_object_dict['fields']):
                        old_content = self.anki_object.fields[field_idx]
                        self.anki_object_dict["fields"][field_idx] = old_content
                        logger.debug(f"User protected field '{protected_field}' (idx={field_idx}): preserved content using same position fallback")
                    else:
                        logger.warning(f"User protected field '{protected_field}' index {field_idx} is out of range")

        logger.debug(f"Protected tags handled")
        
        # Merge preserved default tags (leech, marked, missing-media) back into tags
        # These were on the local note and should be kept even though the server doesn't have them
        if preserved_default_tags:
            if 'tags' not in self.anki_object_dict:
                self.anki_object_dict['tags'] = []
            for tag in preserved_default_tags:
                if tag not in self.anki_object_dict["tags"]:
                    self.anki_object_dict["tags"].append(tag)
            logger.debug(f"Preserved default protected tags: {preserved_default_tags}")
        
        if import_config.has_optional_tags and 'tags' in self.anki_object_dict:
            self.anki_object_dict["tags"] = [
                tag for tag in self.anki_object_dict["tags"]
                if not tag.startswith(PREFIX_OPTIONAL_TAGS) or 
                tag.split('::', 1)[1] in import_config.optional_tags
            ]
        
        if (self.anki_object and hasattr(self.anki_object, 'fields') and 
            len(self.anki_object_dict.get("fields", [])) != len(self.anki_object.fields)):
            logger.debug(f"Field count difference between remote and local note: {len(self.anki_object_dict['fields'])} vs {len(self.anki_object.fields)} (this is normal for custom fields)")
            
        # check if anki_object_dict["fields"] has the same number of fields as note_model.fields
        if (len(self.anki_object_dict.get("fields", [])) != len(note_model.anki_dict['flds']) or 
            (self.anki_object and hasattr(self.anki_object, 'fields') and 
             len(self.anki_object.fields) != len(note_model.anki_dict['flds']))):
            remote_count = len(self.anki_object_dict['fields'])
            notetype_count = len(note_model.anki_dict['flds'])
            old_count = len(self.anki_object.fields) if self.anki_object and hasattr(self.anki_object, 'fields') else 0
            if remote_count > notetype_count:
                logger.debug(f"Remote note has {remote_count - notetype_count} extra fields beyond notetype definition - treating as custom fields")
            elif remote_count < notetype_count:
                logger.warning(f"Remote note missing {notetype_count - remote_count} fields expected by notetype")
            elif old_count != notetype_count:
                logger.warning(f"Old note has {old_count - notetype_count} extra fields beyond notetype definition - treating as custom fields")
            elif old_count < notetype_count:
                logger.warning(f"Old note missing {notetype_count - old_count} fields expected by notetype")
            else:
                logger.debug("Remote note fields match notetype definition")
            logger.debug(f"Note model name: {note_model.anki_dict['name']}")
            logger.debug(f"Note model fields: {note_model.anki_dict['flds']}")
            old_fields = self.anki_object.fields if self.anki_object and hasattr(self.anki_object, 'fields') else []
            logger.debug(f"Old Note fields: {old_fields}")
            logger.debug(f"New Note fields: {self.anki_object_dict['fields']}")
            logger.debug(f"Note UUID: {self.get_uuid()}")
            
        logger.debug(f"Note {self.get_uuid()} handled import config changes successfully")
            
    
    def remove_tags(self, tags): # Option to remove personal tags from notes before uploading them
        if not self.anki_object_dict or 'tags' not in self.anki_object_dict:
            return
            
        for personal_tag in tags:
            if personal_tag in self.anki_object_dict["tags"]:
                self.anki_object_dict["tags"].remove(personal_tag)
            # Remove tags that start with the personal_tag prefix
            self.anki_object_dict["tags"] = [tag for tag in self.anki_object_dict["tags"] if not tag.startswith(f"{personal_tag}::")]

        # Remove any tags that are just whitespace
        self.anki_object_dict["tags"] = [tag for tag in self.anki_object_dict["tags"] if tag.strip()]