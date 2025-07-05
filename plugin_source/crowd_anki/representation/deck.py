from collections import namedtuple, defaultdict
from concurrent.futures import Future
from typing import Callable, Any, Iterable

from .deck_config import DeckConfig
from .json_serializable import JsonSerializableAnkiDict
from .note_model import NoteModel
from .note import Note
from ..anki.adapters.file_provider import FileProvider
from ..importer.import_dialog import ImportConfig
from ..utils import utils
from ..utils.constants import UUID_FIELD_NAME
from ..utils.uuid import UuidFetcher
from ..utils.notifier import AnkiModalNotifier
from ..anki.overrides.change_model_dialog import ChangeModelDialog
from ...thread import run_function_in_thread, sync_run_async

from ... import main

from ...auth_manager import auth_manager

import os
import aqt
import anki
import requests
import logging
from anki.collection import Collection, EmptyCardsReport
from aqt.operations import QueryOp
from aqt.emptycards import EmptyCardsDialog
from aqt.operations.tag import clear_unused_tags
from aqt.utils import showInfo
from anki.notes import Note as AnkiNote
from aqt import mw
from anki.errors import NotFoundError

from ...var_defs import API_BASE_URL

CHUNK_SIZE = 1000
        
logger = logging.getLogger("ankicollab")
DeckMetadata = namedtuple("DeckMetadata", ["deck_configs", "models"])


def silent_clear_empty_cards() -> None:
    def on_done(fut: Future) -> None:
        report: EmptyCardsReport = fut.result()
        if report.notes:
            dialog = EmptyCardsDialog(aqt.mw, report)
            dialog._delete_cards(keep_notes=True)

    aqt.mw.taskman.run_in_background(aqt.mw.col.get_empty_cards, on_done)

def silent_clear_unused_tags() -> None:
    aqt.mw.taskman.run_in_background(aqt.mw.col.tags.clear_unused_tags)
    
class Deck(JsonSerializableAnkiDict):
    DECK_NAME_DELIMITER = "::"
    root_deck_id = None

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
                         "notes"}

    def __init__(self,
                 file_provider_supplier: Callable[[Any, Iterable[int]], FileProvider],
                 anki_deck=None,
                 is_child=False):
        super().__init__(anki_deck)

        self.file_provider_supplier = file_provider_supplier
        self.is_child = is_child

        self.collection = aqt.mw.col
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
            {"note_models": list(self.metadata.models.values()),
             "deck_configurations": list(self.metadata.deck_configs.values())} if not self.is_child else {})

    def get_media_file_list(self, data_from_models=True, include_children=True):
        media = set()
        for note in self.notes:
            anki_object = note.anki_object
            # TODO Remove compatibility shims for Anki 2.1.46 and
            # lower.
            if anki_object is None:
                continue
            join_fields = anki_object.joined_fields if hasattr(anki_object, 'joined_fields') else anki_object.joinedFields
            for media_file in self.collection.media.files_in_str(anki_object.mid, join_fields()):
                media.add(media_file)

        if include_children:
            for child in self.children:
                media |= child.get_media_file_list(False, include_children)

        return media | (self._get_media_from_models() if data_from_models else set())

    def get_protected_fields(self, deckHash):        
        # Create result structure with caches
        result = {
            "models": [],
            "model_name_to_fields": {},  # Maps model names to protected field names
            "model_name_to_indices": {}  # Maps model names to protected field indices
        }
        
        if not deckHash:
            return result
        
        response = requests.get(f"{API_BASE_URL}/GetProtectedFields/" + deckHash)
        
        if response and response.status_code == 200:
            result["models"] = response.json()
            
            for model in result["models"]:
                model_name = model['name']
                protected_field_names = [field['name'] for field in model['fields']]
                result["model_name_to_fields"][model_name] = protected_field_names
                
                for note_model_uuid, note_model in self.metadata.models.items():
                    current_model_name = note_model.anki_dict["name"]
                    if current_model_name == model_name:
                        # Find indices of protected fields
                        indices = []
                        for i, field in enumerate(note_model.anki_dict["flds"]):
                            if field['name'] in protected_field_names:
                                indices.append(i)
                        result["model_name_to_indices"][model_name] = indices
        
        return result

    def get_media_file_note_map(self, protected_fields, include_children=True):
        media_file_note_pairs = []
        
        model_name_to_indices = protected_fields.get("model_name_to_indices", {})
        
        for note in self.notes:
            anki_object = note.anki_object
            
            if anki_object is None:
                continue
                
            note_model = self.metadata.models[note.note_model_uuid]
            if not note_model:
                continue
            model_name = note_model.anki_dict["name"]
            
            protected_indices = model_name_to_indices.get(model_name, [])
            
            # Process fields except protected ones
            for i in range(len(anki_object.fields)):
                if i in protected_indices:
                    continue
                field = anki_object.fields[i]
                
                for media_file in self.collection.media.files_in_str(anki_object.mid, field):
                    # Skip files in subdirs
                    if media_file != os.path.basename(media_file):
                        continue
                    media_file_note_pairs.append((media_file, note.get_uuid()))
        
        if include_children:
            for child in self.children:
                media_file_note_pairs.extend(child.get_media_file_note_map(protected_fields, include_children))
                    
        return media_file_note_pairs

    def refresh_notes(self, media_file_note_pairs):
        # retrieves and updates the notes specified in the map with the media files since they changed
        for _, note_uuid in media_file_note_pairs:
            for note in self.notes:
                if note.get_uuid() == note_uuid:
                    note.anki_object = self.collection.get_note(note.anki_object.id) # refreshes it bc it changed
                    break
        for child in self.children:
                child.refresh_notes(media_file_note_pairs)
    
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
        
    def on_success_wrapper(self, result):
        """Wrapper for on_success that unpacks the tuple result"""
        count, media_result = result
        self.on_success(count, media_result) 
    
    def delete_empty_subdecks(self):
        for name, did in aqt.mw.col.decks.children(self.root_deck_id):
            try:
                if not aqt.mw.col.decks.is_filtered(did) and aqt.mw.col.decks.card_count(did, include_subdecks=True) == 0:
                    filtered_children = [
                        cid for _, cid in aqt.mw.col.decks.children(did)
                        if aqt.mw.col.decks.is_filtered(cid)
                    ]
                    if filtered_children:
                        parent_name = aqt.mw.col.decks.immediate_parent(name)
                        parent_did = aqt.mw.col.decks.id_for_name(parent_name) if parent_name else self.root_deck_id
                        if parent_did:
                            aqt.mw.col.decks.reparent(filtered_children, parent_did)
                            
                    aqt.mw.col.decks.remove([did])
                    
            except NotFoundError:
                continue
            except Exception as e:
                print(f"Error while processing deck {name}: {e}")
                continue
            
    def on_success(self, count: int, media_result) -> None:
        if count > 0:
            silent_clear_unused_tags()
            silent_clear_empty_cards()
        
        if self.root_deck_id:            
            self.delete_empty_subdecks()
            
        self.on_media_download_done(media_result)
                    
        mw.progress.finish()
        # Reset window without blocking main thread
        aqt.mw.reset()
    
    def import_progress_cb(self, curr: int, max_i: int):
        percentage = (curr / max_i) * 100
        aqt.mw.taskman.run_on_main(
            lambda: aqt.mw.progress.update(
                label=
                f"Processed {curr} / {max_i} notes ({percentage:.1f}%)...",
                value=curr,
                max=max_i,
            ) if aqt.mw.progress.busy() else None
        )
        
    def on_media_download_done(self, result=None) -> None:
        if result is None:
            result = {"success": False, "message": "Unknown error"}
            
        mw.col.media.check()
        
        if result["success"]:
            msg = (f"Media files: {result.get('downloaded', 0)} downloaded, "
                f"{result.get('skipped', 0)} existing")
            aqt.utils.tooltip(msg, parent=mw)
        else:
            aqt.utils.showWarning(
                f"Media download error: {result.get('message', 'Unknown error')}", 
                parent=mw
            )
        
    def process_media_download(self, deck_hash, media_files):
        try:
            if media_files is None:
                return
            dir_path = self.collection.media.dir()
            missing_files = []

            for file_name in media_files:
                if not os.path.exists(os.path.join(dir_path, file_name)):
                    missing_files.append(file_name)
                    
            if len(missing_files) > 0:
                user_token = auth_manager.get_token()
                return sync_run_async(main.media_manager.get_media_manifest_and_download, 
                    user_token=user_token,
                    deck_hash=deck_hash,
                    filenames=missing_files,
                    progress_callback=None,  # Uncomment if you want to show progress
                    # progress_callback=lambda p: mw.taskman.run_on_main(
                    #     lambda: mw.progress.update(
                    #         value=int(p * 100),
                    #         max=100,
                    #         label=f"Downloading media files... {int(p * 100)}%"
                    #     ) if mw.progress.busy() else None
                    # )
                )
            else:
                return {
                    "success": True, 
                    "message": f"No missing media files to download",
                    "downloaded": 0,
                    "skipped": 0
                }
        except Exception as e:
            logger.error(f"Error in process_media_download: {str(e)}")
            return {"success": False, "message": str(e)}
        
    def save_to_collection(self, collection, model_map_cache, note_type_data, import_config: ImportConfig):
        self.save_metadata(collection, import_config.home_deck, model_map_cache, note_type_data)
        med_res = {
                    "success": True, 
                    "message": f"Unknown Media download error",
                    "downloaded": 0,
                    "skipped": 0
                }
        op = QueryOp(
            parent=mw,
            op=lambda collection=collection,
            parent_name="",
            status_cb=self.import_progress_cb,
            status_cur=0,
            status_max=self.get_note_count(),
            media_result=med_res,
            import_config=import_config: 
                self.save_decks_and_notes(collection=collection,
                    parent_name=parent_name,
                    status_cb=status_cb,
                    status_cur=status_cur,
                    status_max=status_max,
                    import_config=import_config,
                    media_result=media_result,
                ),
            success=self.on_success_wrapper,
        )
        op.with_progress("Synchronizing...").run_in_background()
    
    def handle_notetype_changes(self, collection, model_map_cache, note_type_data):
        def on_accepted():
            model_map_cache[old_model_uuid][note.note_model_uuid] = \
                NoteModel.ModelMap(dialog.get_field_map(), dialog.get_template_map())
        
        saved_models = set()
        for note_model in self.metadata.models.values():
            model_uuid = note_model.get_uuid()
            if model_uuid not in saved_models:
                note_model.save_to_collection(collection)
                saved_models.add(model_uuid)
        
        fetcher = UuidFetcher(collection)
        
        # Fetch the ids and model GUIDs of all notes with a UUID in note_uuids
        note_uuids = [note.get_uuid() for note in self.notes]
        if not note_uuids:  # Early exit if no notes
            for child in self.children:
                child.handle_notetype_changes(collection, model_map_cache, note_type_data)
            return
            
        placeholders = ', '.join('?' for _ in note_uuids)
        query = "SELECT guid, mid, id FROM notes WHERE guid IN ({})"
        query = query.format(placeholders)
        mids_and_ids = collection.db.all(query, *note_uuids)
        model_guids = {uuid: (collection.models.get(mid).get(UUID_FIELD_NAME), id) for uuid, mid, id in mids_and_ids}

        model_cache = {}  # Cache for loaded models
        compatibility_cache = {}  # Cache for field compatibility results
        
        # Performance tracking
        model_fetch_count = 0
        compatibility_check_count = 0
        
        def get_cached_model(model_uuid):
            nonlocal model_fetch_count
            if model_uuid not in model_cache:
                model_dict = fetcher.get_model(model_uuid)
                if model_dict:
                    model_cache[model_uuid] = NoteModel.from_json(model_dict)
                    model_fetch_count += 1
                else:
                    model_cache[model_uuid] = None
            return model_cache[model_uuid]
        
        def check_cached_compatibility(old_uuid, new_uuid):
            nonlocal compatibility_check_count
            cache_key = (old_uuid, new_uuid)
            if cache_key not in compatibility_cache:
                old_model = get_cached_model(old_uuid)
                new_model = get_cached_model(new_uuid)
                
                if old_model and new_model:
                    # Check if this is a projektanki note type
                    note_type_name = new_model.anki_dict.get("name", "").lower()
                    should_preserve_templates = "projektanki" in note_type_name
                    
                    if should_preserve_templates:
                        # For projektanki: only check field compatibility
                        compatibility_cache[cache_key] = NoteModel.check_fields_compatible(old_model, new_model)
                    else:
                        # For non-projektanki: use semantic identity check
                        compatibility_cache[cache_key] = NoteModel.check_semantically_identical(old_model, new_model)
                    compatibility_check_count += 1
                else:
                    compatibility_cache[cache_key] = False
            return compatibility_cache[cache_key]

        # Group notes by model transitions to batch process similar changes
        model_transitions = defaultdict(list)  # (old_uuid, new_uuid) -> [notes]
        
        for note in self.notes:
            old_model_uuid, note_id = model_guids.get(note.get_uuid(), (None, None))
            if old_model_uuid and note.note_model_uuid != old_model_uuid:
                transition_key = (old_model_uuid, note.note_model_uuid)
                model_transitions[transition_key].append((note, note_id))
            elif note_id is not None:  # Existing note with same model
                # Still need to initialize note_type_data for existing notes
                if old_model_uuid not in note_type_data:
                    if note.anki_object is None:
                        note.anki_object = fetcher.get_note(note.get_uuid())
                    note_type = note.note_type()
                    note_type_data[old_model_uuid] = (note_type, note.note_model_uuid, [])

        # Process each unique model transition
        for (old_model_uuid, new_model_uuid), notes_with_ids in model_transitions.items():
            # Check compatibility once per transition type
            # For projektanki note types: only checks field compatibility (preserves templates)
            fields_compatible = check_cached_compatibility(old_model_uuid, new_model_uuid)
            
            # Initialize note_type_data entry if needed
            if old_model_uuid not in note_type_data:
                # Use the first note to get the note_type
                first_note = notes_with_ids[0][0]
                if first_note.anki_object is None:
                    first_note.anki_object = fetcher.get_note(first_note.get_uuid())
                note_type = first_note.note_type()
                note_type_data[old_model_uuid] = (note_type, new_model_uuid, [])
            
            if not fields_compatible:
                # Fields are incompatible - need change model dialog
                mapping = model_map_cache[old_model_uuid].get(new_model_uuid)
                if not mapping:
                    # Need to create mapping - use first note for dialog
                    first_note = notes_with_ids[0][0]
                    if first_note.anki_object is None:
                        first_note.anki_object = fetcher.get_note(first_note.get_uuid())
                    if first_note.anki_object is None:
                        print(f"No note found with UUID: {first_note.get_uuid()}")
                        continue
                    
                    new_model = get_cached_model(new_model_uuid)
                    if new_model:
                        new_model.make_current(collection)
                        dialog = ChangeModelDialog(collection, [first_note.anki_object.id], first_note.note_type(), mw)
                        dialog.accepted.connect(on_accepted)
                        dialog.exec()
                
                # Add all note IDs to note_type_data for batch processing
                note_ids = [note_id for _, note_id in notes_with_ids if note_id is not None]
                note_type_data[old_model_uuid][2].extend(note_ids)
         
        for child in self.children:
            child.handle_notetype_changes(collection, model_map_cache, note_type_data)

    def save_metadata(self, collection, home_deck, model_map_cache, note_type_data):
        for config in self.metadata.deck_configs.values():
            config.save_to_collection(collection)

        # Update notetypes for existing notes
        fetcher = UuidFetcher(collection)
        for old_model_uuid, (note_type, new_model_uuid, note_ids) in note_type_data.items():
            if note_ids:
                new_model = NoteModel.from_json(fetcher.get_model(new_model_uuid))
                new_model.make_current(collection)
                mapping = model_map_cache[old_model_uuid].get(new_model_uuid)
                if mapping:
                    collection.models.change(note_type,
                                            note_ids,
                                            new_model.anki_dict,
                                            mapping.field_map,
                                            mapping.template_map)
            
        self._save_deck(collection, "", home_deck) # We store the root deck in this thread to avoid concurrency issues
    
    def save_decks_and_notes(self, collection, parent_name, status_cb, status_cur, status_max, import_config: ImportConfig, media_result):
        full_name = self._save_deck(collection, parent_name, import_config.home_deck)
                    
        deck_id = self.anki_dict["id"] if self else None
        if not deck_id:
            return status_cur, media_result
        int_time = anki.utils.int_time()
        
        # Batch fetch existing notes
        note_uuids = [note.get_uuid() for note in self.notes]
        existing_notes = []
        for i in range(0, len(note_uuids), CHUNK_SIZE):
            chunk = note_uuids[i:i+CHUNK_SIZE]
            placeholders = ','.join('?' * len(chunk))
            existing_notes += collection.db.all(
                f"SELECT guid, id FROM notes WHERE guid IN ({placeholders})", *chunk
            )
        existing_note_map = {guid: nid for guid, nid in existing_notes}
        
        # Group notes by note type to reduce model lookups
        notes_by_type = defaultdict(list)  # note_model_uuid -> [notes]
        for note in self.notes:
            notes_by_type[note.note_model_uuid].append(note)
                
        # Pre-process notes in memory, grouped by type
        new_notes = []
        update_notes = []
        
        for note_model_uuid, notes_group in notes_by_type.items():
            note_model = self.metadata.models[note_model_uuid]  # Single lookup per note type
            
            for note in notes_group:
                uuid = note.get_uuid()
                if uuid not in existing_note_map:
                    note.anki_object = AnkiNote(collection, note_model.anki_dict)
                    new_notes.append(note)
                else:
                    note.anki_object = AnkiNote(collection, id=existing_note_map[uuid])
                    update_notes.append(note)
                
                note.handle_import_config_changes(import_config, note_model)
                note.anki_object.__dict__.update(note.anki_object_dict)
                note.anki_object.mid = note_model.anki_dict["id"]
                note.anki_object.mod = int_time
                
                status_cur += 1
                if status_cur % 100 == 0:
                    status_cb(status_cur, status_max)
                    if mw.progress.want_cancel():
                        return status_cur, media_result

        # Batch process notes
        if new_notes:
            Note.bulk_add_notes(collection, new_notes, deck_id, import_config)
        if update_notes:
            Note.bulk_update_notes(collection, update_notes, deck_id, import_config)
            
        # import media
        media_files = self.get_media_file_list(data_from_models=True, include_children=False)
        # Download media files after deck import
        this_deck_media_res = None
        if media_files:
            this_deck_media_res = self.process_media_download(import_config.deck_hash, media_files)
        
        # Append deck media to media result
        if this_deck_media_res:
            media_result["downloaded"] += this_deck_media_res.get("downloaded", 0)
            media_result["skipped"] += this_deck_media_res.get("skipped", 0)
            media_result["success"] = media_result["success"] and this_deck_media_res.get("success", False)
            
        for child in self.children:
            status_cur, media_result = child.save_decks_and_notes(
                collection, full_name, status_cb, status_cur, status_max, import_config, media_result
            )
            if mw.progress.want_cancel():
                return status_cur, media_result
                
        return status_cur, media_result

    def _save_deck(self, collection, parent_name, home_deck):        
        full_name = (parent_name + self.DECK_NAME_DELIMITER if parent_name else "") + self.anki_dict["name"]
        deck_dict = UuidFetcher(collection).get_deck(self.get_uuid())

        deck_id = collection.decks.id(full_name, create=False)
        
        # Only create new deck for root deck. Subdecks get overwritten.
        if deck_id and (not deck_dict or deck_dict["id"] != deck_id):
            if not parent_name:
                if home_deck: # set the home deck as the root deck
                    full_name = home_deck
                    new_deck_id = collection.decks.id(full_name)
                    deck_dict = collection.decks.get(new_deck_id) # Update to home deck
                else:
                    full_name = self._rename_deck(full_name, collection)
            #else: Don't rename subdecks to prevent ugly _(AnkiCollab) subdecks

        if not deck_dict:
            new_deck_id = collection.decks.id(full_name)
            deck_dict = collection.decks.get(new_deck_id) # Create deck in collection

        deck_dict.update(self.anki_dict)

        self.anki_dict = deck_dict
        self.anki_dict["name"] = full_name
        
        collection.decks.save(deck_dict)
        
        if not parent_name: # Only set root deck id for root deck
            self.root_deck_id = collection.decks.id(full_name, create=False)
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
