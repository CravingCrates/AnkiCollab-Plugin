from collections import namedtuple, defaultdict
from typing import Callable, Any, Iterable

from .deck_config import DeckConfig
from .json_serializable import JsonSerializableAnkiDict
from .note_model import NoteModel
from ..anki.adapters.file_provider import FileProvider
from ..importer.import_dialog import ImportConfig
from ..utils import utils
from ..utils.constants import UUID_FIELD_NAME
from ..utils.uuid import UuidFetcher
from ..utils.notifier import AnkiModalNotifier
from ...mega_downloader import download_media_from_url
from ...thread import run_function_in_thread

import os
import aqt
from aqt import mw

DeckMetadata = namedtuple("DeckMetadata", ["deck_configs", "models"])

def handle_media_import(media_url, media_files):
    # Get the directory path
    dir_path = aqt.mw.col.media.dir()
    missing_files = []
    for file_name in media_files:
        if not os.path.exists(os.path.join(dir_path, file_name)):
            missing_files.append(file_name)
    # Download the missing files
    if len(missing_files) > 0:
        download_media_from_url(media_url, missing_files, dir_path)

class Deck(JsonSerializableAnkiDict):
    DECK_NAME_DELIMITER = "::"

    export_filter_set = JsonSerializableAnkiDict.export_filter_set | \
                        {
                            "collection",  # runtime-relevant
                            "newToday",
                            "revToday",
                            "timeToday",
                            "lrnToday",
                            "metadata",
                            "browserCollapsed",
                            "collapsed",
                            "is_child",  # runtime-relevant
                            "conf",  # uuid
                            "file_provider_supplier"
                        }

    import_filter_set = JsonSerializableAnkiDict.import_filter_set | \
                        {"note_models",
                         "deck_configurations",
                         "children",
                         "media_files",
                         "notes"}

    def __init__(self,
                 file_provider_supplier: Callable[[Any, Iterable[int]], FileProvider],
                 anki_deck=None,
                 is_child=False):
        super().__init__(anki_deck)

        self.file_provider_supplier = file_provider_supplier
        self.is_child = is_child

        self.collection = None
        self.notes = []
        self.children = []
        self.metadata = None

    def flatten(self):
        """
        Specification in order to store only deck lowest level name in JSON
        :return:
        """
        result = super(Deck, self).flatten()
        if self.is_child:
            result["name"] = result["name"].split(self.DECK_NAME_DELIMITER)[-1]

        return result

    def get_note_count(self):
        return len(self.notes) + sum(child.get_note_count() for child in self.children)

    def _update_db(self):
        # Introduce uuid field for unique identification of entities
        utils.add_column(self.collection.db, "notes", UUID_FIELD_NAME)

    def _load_metadata(self):
        if not self.metadata:
            self.metadata = DeckMetadata({}, {})

        self._load_deck_config()

    def _load_deck_config(self):
        # Todo switch to uuid
        new_config = DeckConfig.from_collection(self.collection, self.anki_dict["conf"])
        self.metadata.deck_configs.setdefault(new_config.get_uuid(), new_config)

    def serialization_dict(self):
        return utils.merge_dicts(
            super(Deck, self).serialization_dict(),
            {"media_files": list(sorted(self.get_media_file_list(include_children=False)))},
            {"note_models": list(self.metadata.models.values()),
             "deck_configurations": list(self.metadata.deck_configs.values())} if not self.is_child else {})

    def get_media_file_list(self, data_from_models=True, include_children=True):
        media = set()
        for note in self.notes:
            anki_object = note.anki_object
            # TODO Remove compatibility shims for Anki 2.1.46 and
            # lower.
            join_fields = anki_object.joined_fields if hasattr(anki_object, 'joined_fields') else anki_object.joinedFields
            for media_file in self.collection.media.files_in_str(anki_object.mid, join_fields()):
                media.add(media_file)

        if include_children:
            for child in self.children:
                media |= child.get_media_file_list(False, include_children)

        return media | (self._get_media_from_models() if data_from_models else set())

    def _get_media_from_models(self):
        model_ids = [model.anki_dict["id"] for model in self.metadata.models.values()]
        file_provider = self.file_provider_supplier(self.collection, model_ids)

        return file_provider.get_files()

    # This is a little gadget i wrote, because in ankicollab the note_models are in the deck that use it, not in the topmost deck. So in the "OG" Deck we aggregate all notetypes
    def _add_models_from_children(self, json_dict, note_models_list):
        note_models_list += [NoteModel.from_json(model) for model in json_dict.get("note_models", [])]
        for child in json_dict.get("children", []):
            self._add_models_from_children(child, note_models_list)

    def _load_metadata_from_json(self, json_dict):
        if not self.metadata:
            self.metadata = DeckMetadata({}, {})

        note_models_list = []
        self._add_models_from_children(json_dict, note_models_list)
        new_models = utils.merge_dicts(self.metadata.models,
                                    {model.get_uuid(): model for model in note_models_list})

        deck_config_list = [DeckConfig.from_json(deck_config) for deck_config in
                            json_dict.get("deck_configurations", [])]

        new_deck_configs = utils.merge_dicts(self.metadata.deck_configs,
                                             {deck_config.get_uuid(): deck_config for deck_config in deck_config_list})

        self.metadata = DeckMetadata(new_deck_configs, new_models)

    def save_to_collection(self, media_url, collection, import_config: ImportConfig):
        self.save_metadata(collection)

        self.save_decks_and_notes(collection=collection,
                                  parent_name="",
                                  model_map_cache=defaultdict(dict),
                                  import_config=import_config,
                                  media_url=media_url)

    def save_metadata(self, collection):
        for config in self.metadata.deck_configs.values():
            config.save_to_collection(collection)

        for note_model in self.metadata.models.values():
            note_model.save_to_collection(collection)
        
    def save_decks_and_notes(self, collection, parent_name, model_map_cache, import_config: ImportConfig, media_url):
        full_name = self._save_deck(collection, parent_name)

        for child in self.children:
            child.save_decks_and_notes(collection=collection,
                                       parent_name=full_name,
                                       model_map_cache=model_map_cache,
                                       import_config=import_config,
                                       media_url=media_url
                                       )

        # Import Media if the url is not an empty string
        if media_url:
            run_function_in_thread(handle_media_import, media_url, self.media_files)
        
        if import_config.use_notes:
            for note in self.notes:
                note.save_to_collection(collection, self, model_map_cache, import_config=import_config)
        

    def _save_deck(self, collection, parent_name):
        full_name = (parent_name + self.DECK_NAME_DELIMITER if parent_name else "") + self.anki_dict["name"]

        deck_dict = UuidFetcher(collection).get_deck(self.get_uuid())

        deck_id = collection.decks.id(full_name, create=False)
        if deck_id and (not deck_dict or deck_dict["id"] != deck_id):
            full_name = self._rename_deck(full_name, collection)

        if not deck_dict:
            new_deck_id = collection.decks.id(full_name)
            deck_dict = collection.decks.get(new_deck_id)

        deck_dict.update(self.anki_dict)

        self.anki_dict = deck_dict
        self.anki_dict["name"] = full_name
        
        collection.decks.save(deck_dict)

        return full_name

    @staticmethod
    def _rename_deck(initial_name, collection):
        """Adds unique suffix to the name, until it becomes unique (required by Anki)"""
        # Todo consider popup

        # This approach can be costly if we have a lot of decks with specific set of names.
        # And adding random appendix would've been faster, but less user-friendly
        number = 2
        new_name = initial_name + " (AnkiCollab)"
        deck_id = collection.decks.id(new_name, create=False)
        while deck_id:
            new_name = new_name + "_" + str(number)
            number += 1
            deck_id = collection.decks.id(new_name, create=False)

        return new_name
