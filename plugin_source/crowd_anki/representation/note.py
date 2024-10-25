import re
import anki
import aqt
import anki.utils
from aqt import mw
from anki.notes import Note as AnkiNote
from anki.utils import is_win, point_version

ANKI_INT_VERSION = point_version()
ANKI_VERSION_23_10_00 = 231000
if ANKI_INT_VERSION >= ANKI_VERSION_23_10_00:
    from anki.collection import AddNoteRequest
    
from ...var_defs import PREFIX_OPTIONAL_TAGS, PREFIX_PROTECTED_FIELDS
from .json_serializable import JsonSerializableAnkiObject
from .note_model import NoteModel
from ..importer.import_dialog import ImportConfig
from ..config.config_settings import ConfigSettings
from ..utils.constants import UUID_FIELD_NAME


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

    # def handle_model_update(self, collection, model_map_cache):
    #     """
    #     Update note's cards if note's model has changed
    #     """
    #     old_model_uuid = self.note_type().get(UUID_FIELD_NAME)
    #     if self.note_model_uuid == old_model_uuid:
    #         return


    #     uuid_fetcher = UuidFetcher(collection)
    #     new_model = NoteModel.from_json(uuid_fetcher.get_model(self.note_model_uuid))
    #     # todo if models semantically identical - create map without calling dialog
    #     # old_model = NoteModel.from_json(uuid_fetcher.get_model(old_model_uuid))
    #     # if NoteModel.check_semantically_identical(new_model, old_model):
    #     #     # models are semantically identical, so create a mapping without calling dialog
    #     #     field_map = {}
    #     #     template_map = {}
    #     #     for i, field in enumerate(new_model.anki_dict['flds']):
    #     #         field_map[i] = i
    #     #     for i, template in enumerate(new_model.anki_dict['tmpls']):
    #     #         template_map[i] = i
    #     #     model_map_cache[old_model_uuid][self.note_model_uuid] = NoteModel.ModelMap(field_map, template_map)
    #     #     return

    #     # # models are not semantically identical, so call dialog to create mapping
    #     mapping = model_map_cache[old_model_uuid].get(self.note_model_uuid)
    #     if mapping: # should always be true from the background thread
    #         collection.models.change(self.note_type(),
    #                                  [self.anki_object.id],
    #                                  new_model.anki_dict,
    #                                  mapping.field_map,
    #                                  mapping.template_map)
    #     else:
    #         print("Error: model mapping not found for note model update")
    #         print("note info: ", self.anki_object.id, old_model_uuid)

    #     # To get an updated note to work with
    #     self.anki_object = uuid_fetcher.get_note(self.get_uuid())

    @staticmethod
    def bulk_update_notes(collection, notes, deck_id, import_config):
        if not notes:
            return
        collection.update_notes([note.anki_object for note in notes])
        
        if not import_config.ignore_deck_movement:
            cards_to_move = []
            target_deck_id = deck_id
            for note in notes:
                card_ids = note.anki_object.card_ids()
                for card_id in card_ids:
                    card = collection.get_card(card_id)
                    if card.did != target_deck_id and card.odid == 0: # skip filtered decks 2
                        cards_to_move.append(card_id)
            if cards_to_move:
                collection.set_deck(cards_to_move, target_deck_id)
        
    @staticmethod
    def bulk_add_notes(collection, notes, deck_id, import_config):
        if ANKI_INT_VERSION >= ANKI_VERSION_23_10_00:
            add_note_requests = [AddNoteRequest(note.anki_object, deck_id=deck_id) for note in notes]
            collection.add_notes(add_note_requests)
        else:
            for note in notes:
                collection.add_note(note.anki_object, deck_id)

        # Suspend new cards if configured
        if import_config and import_config.suspend_new_cards:
            cards_to_suspend = []
            for note in notes:
                cards_to_suspend.extend(note.anki_object.card_ids())
            if cards_to_suspend:
                collection.sched.suspend_cards(cards_to_suspend)                

    #returns True if the note is new
    def prep_for_update(self, collection, deck, import_config, int_time, fetcher):
        note_model = deck.metadata.models[self.note_model_uuid]

        self.anki_object = fetcher.get_note(self.get_uuid())
        new_note = self.anki_object is None
        if new_note:
            self.anki_object = AnkiNote(collection, note_model.anki_dict)

        self.handle_import_config_changes(import_config, note_model)

        self.anki_object.__dict__.update(self.anki_object_dict)
        self.anki_object.mid = note_model.anki_dict["id"]
        self.anki_object.mod = int_time
        
        return new_note
            
    def handle_import_config_changes(self, import_config, note_model):
        # Cache field names and indices for faster lookup
        field_name_to_index = {
            field['name']: idx 
            for idx, field in enumerate(note_model.anki_dict['flds'])
        }
        
        # Handle protected fields set by maintainer 
        protected_fields = [
            num for num in range(len(self.anki_object_dict["fields"]))
            if import_config.is_personal_field(note_model.anki_dict['name'], 
                                            note_model.anki_dict['flds'][num]['name'])
        ]
        for num in protected_fields:
            self.anki_object_dict["fields"][num] = self.anki_object.fields[num]

        protected_tags = [
            tag for tag in self.anki_object.tags 
            if tag.startswith(PREFIX_PROTECTED_FIELDS)
        ]
        
        for tag in protected_tags:
            # Ensure protected tags are preserved in anki_object_dict
            if tag not in self.anki_object_dict["tags"]:
                self.anki_object_dict["tags"].append(tag)
                
            protected_field = tag.split('::', 1)[1]
            
            if protected_field == "Tags":
                self.anki_object_dict["tags"] = self.anki_object.tags
                
            if protected_field == "All":
                self.anki_object_dict["fields"] = self.anki_object.fields
                break
                
            # Handle individual field protection
            field_idx = field_name_to_index.get(protected_field) or \
                    field_name_to_index.get(protected_field.replace('_', ' '))
            if field_idx is not None:
                self.anki_object_dict["fields"][field_idx] = self.anki_object.fields[field_idx]

        if import_config.has_optional_tags:
            self.anki_object_dict["tags"] = [
                tag for tag in self.anki_object_dict["tags"]
                if not tag.startswith(PREFIX_OPTIONAL_TAGS) or 
                tag.split('::', 1)[1] in import_config.optional_tags
            ]

    def remove_tags(self, tags): # Option to remove personal tags from notes before uploading them
        for personal_tag in tags:
            if personal_tag in self.anki_object_dict["tags"]:
                self.anki_object_dict["tags"].remove(personal_tag)
            # Remove tags that start with the personal_tag prefix
            self.anki_object_dict["tags"] = [tag for tag in self.anki_object_dict["tags"] if not tag.startswith(f"{personal_tag}::")]

        # Remove any tags that are just whitespace
        self.anki_object_dict["tags"] = [tag for tag in self.anki_object_dict["tags"] if tag.strip()]