from collections import namedtuple, defaultdict
from concurrent.futures import Future
from typing import Callable, Any, Iterable, List, Dict, Optional

from .deck_config import DeckConfig
from .json_serializable import JsonSerializableAnkiDict
from .note_model import NoteModel
from .note import Note
from ..anki.adapters.file_provider import FileProvider
from anki.models import ChangeNotetypeRequest, NoteType, NotetypeDict, NotetypeId
from ..importer.import_dialog import ImportConfig
from ..utils import utils
from ..utils.constants import UUID_FIELD_NAME
from ..utils.uuid import UuidFetcher
from ..utils.notifier import AnkiModalNotifier
from ...thread import run_function_in_thread, sync_run_async
import uuid
                                
from ... import main
from ...media_progress_indicator import show_media_progress, update_media_progress, complete_media_progress

from ...auth_manager import auth_manager

import os
import aqt
import anki
import anki.utils
import requests
import logging
import time
from anki.collection import Collection, EmptyCardsReport
from aqt.operations import QueryOp
from aqt.emptycards import EmptyCardsDialog
from aqt.operations.tag import clear_unused_tags
from aqt.utils import showInfo, showWarning, tooltip
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
        
        # Store field mappings for intelligent note import
        self._field_mappings = {}  # notetype_uuid -> field_mapping
        
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

    # including notetype media is kinda useless because they cannot be uploaded without a note using them too. Should have thought about that case before, but here we are.
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
        """Extract media files referenced in notetype templates."""
        if not self.metadata or not self.metadata.models:
            return set()
        
        model_ids = [model.anki_dict["id"] for model in self.metadata.models.values()]
        
        if not model_ids:
            logger.warning("No model IDs available for media extraction")
            return set()
        
        try:
            file_provider = self.file_provider_supplier(self.collection, model_ids)
            return file_provider.get_files()
        except Exception as e:
            logger.error(f"Error getting media files from models: {e}")
            return set()

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
        
    def calculate_total_work(self, include_children=True):
        """Calculate total work units for progress tracking"""
        note_count = len(self.notes)
        
        if include_children:
            for child in self.children:
                child_note_count = child.calculate_total_work(include_children=include_children)
                note_count += child_note_count
                
        return note_count
    
    def _start_media_download_from_main_thread(self, deck_hash, missing_files, media_result):
        """Start media download from main thread using QueryOp"""
        if not missing_files:
            self.on_success(0, media_result)  # Pass dummy count
            return
            
        def download_media_operation():
            """Background operation to download media files"""
            try:
                user_token = auth_manager.get_token()
                if not user_token:
                    raise ValueError("No authentication token available")
                
                aqt.mw.taskman.run_on_main(
                    lambda: show_media_progress("download", len(missing_files))
                )
                
                # Call the download in batches to avoid spamming the network - more batching is done inside the media manager
                batch_size = 500
                total_downloaded = 0
                last_result = {"downloaded": 0, "success": True, "message": "Media download completed"}
                
                for i in range(0, len(missing_files), batch_size):
                    batch = missing_files[i:i + batch_size]
                    batch_start_index = i
                    
                    # Progress callback that accounts for current batch position
                    def progress_callback(progress_ratio):
                        batch_completed = int(progress_ratio * len(batch))
                        total_completed = batch_start_index + batch_completed
                        overall_ratio = total_completed / len(missing_files) if len(missing_files) > 0 else 1.0
                        
                        aqt.mw.taskman.run_on_main(
                            lambda: update_media_progress(overall_ratio, total_completed)
                        )
                    
                    batch_result = sync_run_async(main.media_manager.get_media_manifest_and_download,
                        user_token=user_token,
                        deck_hash=deck_hash,
                        filenames=batch,
                        progress_callback=progress_callback,
                    )
                    
                    # Accumulate results from all batches
                    if batch_result:
                        last_result = batch_result
                        total_downloaded += batch_result.get("downloaded", 0)
                
                # Update media result with accumulated download results
                media_result.update({
                    "downloaded": total_downloaded,
                    "success": last_result.get("success", False),
                    "message": last_result.get("message", "Media download completed")
                })
                
                # Complete the progress indicator
                success = last_result.get("success", False)
                message = f"Downloaded {total_downloaded:,} files" if total_downloaded > 0 else "All files were already present"
                aqt.mw.taskman.run_on_main(
                    lambda: complete_media_progress(success, message)
                )
                
                return media_result
                
            except Exception as e:
                logger.error(f"Media download error: {e}")
                media_result.update({
                    "success": False,
                    "message": f"Media download failed: {e}"
                })
                # Show error in progress indicator
                aqt.mw.taskman.run_on_main(
                    lambda: complete_media_progress(False, f"Download failed: {str(e)}")
                )
                return media_result
        
        def on_download_complete(result):
            """Called when media download completes"""
            self.on_success(0, result)  # Pass dummy count
        
        # Start the download operation
        op = QueryOp(
            parent=mw,
            op=lambda _: download_media_operation(),
            success=on_download_complete
        )
        
        # Run without collection access to avoid blocking
        op.without_collection().run_in_background()
    
    def on_success_wrapper(self, result):
        """Wrapper for on_success that unpacks the tuple result"""
        count, media_result = result
        
        # Check if we need to start media download
        missing_files = media_result.get("missing_files")
        deck_hash = media_result.get("deck_hash")
        
        if missing_files and deck_hash:
            # Start media download now from main thread
            self._start_media_download_from_main_thread(deck_hash, missing_files, media_result)
        else:
            # No media download needed, proceed directly
            self.on_success(count, media_result) 
    
    def delete_empty_subdecks(self):
        logger.info(f"Trying to delete empty subdecks in deck {self.anki_dict.get('name', 'unknown')}")
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

                    opchanges = aqt.mw.col.decks.remove(dids=[did])
                    if opchanges.count != 1:
                        logger.warning(f"Failed to delete deck {name}")

            except NotFoundError:
                continue
            except Exception as e:
                print(f"Error while processing deck {name}: {e}")
                continue
            
            
    def on_success(self, count: int, media_result) -> None:
        # Show cleanup phase at 95%
        aqt.mw.taskman.run_on_main(
            lambda: aqt.mw.progress.update(
                label="Finishing...",
                value=95,
                max=100,
            ) if aqt.mw.progress.busy() else None
        )
        
        if count > 0:
            silent_clear_unused_tags()
            silent_clear_empty_cards()
        
        if self.root_deck_id:            
            self.delete_empty_subdecks()
            
        # Show completion at 100%
        aqt.mw.taskman.run_on_main(
            lambda: aqt.mw.progress.update(
                label="Complete",
                value=100,
                max=100,
            ) if aqt.mw.progress.busy() else None
        )
            
        self.on_media_download_done(media_result)
                    
        mw.progress.finish()
        # Reset window without blocking main thread
        aqt.mw.reset()
    
    def create_unified_progress_tracker(self, total_notes):
        class UnifiedProgressTracker:
            def __init__(self, total_notes):
                self.total_notes = total_notes
                self.total_work = total_notes
                self.completed_notes = 0
                self.completed_media = 0
                
                # Pre-calculate phase percentages for smooth transitions
                if self.total_work > 0:
                    self.notes_phase_end = (total_notes / self.total_work) * 100
                    self.media_phase_end = 100
                else:
                    self.notes_phase_end = 100
                    self.media_phase_end = 100
            
            def update_notes_progress(self, completed_notes, deck_name=""):                
                self.completed_notes = completed_notes                                    
                
                if self.total_work == 0:
                    return
                    
                progress_value = min(self.completed_notes + self.completed_media, self.total_work)
                
                label = f"Importing notes: {completed_notes:,} / {self.total_notes:,}"
                
                aqt.mw.taskman.run_on_main(
                    lambda: aqt.mw.progress.update(
                        label=label,
                        value=progress_value,
                        max=self.total_work,
                    ) if aqt.mw.progress.busy() else None
                )
            
            def update_media_progress(self, completed_media, downloading_count=0):                
                self.completed_media = completed_media
                    
                if self.total_work == 0:
                    return
                    
                progress_value = min(self.completed_notes + self.completed_media, self.total_work)
                
                if downloading_count > 0:
                    label = f"Downloading media: {completed_media:,}"
                else:
                    label = f"Processing media: {completed_media:,}"
                
                aqt.mw.taskman.run_on_main(
                    lambda: aqt.mw.progress.update(
                        label=label,
                        value=progress_value,
                        max=self.total_work,
                    ) if aqt.mw.progress.busy() else None
                )
            
            def set_phase_label(self, label, percentage=None):
                """Set a custom label for transition phases"""
                if percentage is not None:
                    progress_value = int((percentage / 100) * self.total_work)
                else:
                    progress_value = self.completed_notes + self.completed_media
                    
                aqt.mw.taskman.run_on_main(
                    lambda: aqt.mw.progress.update(
                        label=label,
                        value=progress_value,
                        max=self.total_work,
                    ) if aqt.mw.progress.busy() else None
                )
        
        return UnifiedProgressTracker(total_notes)
        
    def on_media_download_done(self, result=None) -> None:
        if result is None:
            result = {"success": False, "message": "Unknown error"}
        
        #mw.col.media.check()
        
        if result["success"]:
            downloaded = result.get('downloaded', 0)
            skipped = result.get('skipped', 0)
            total = downloaded + skipped
            
            if downloaded > 0:
                msg = f"Deck imported successfully!\n📁 {downloaded:,} media files downloaded\n✓ {skipped:,} files already present"
            elif total > 0:
                msg = f"Deck imported successfully!\n✓ All {total:,} media files were already present"
            else:
                msg = "Deck imported successfully!\n📝 No media files needed"
                
            aqt.utils.tooltip(msg, parent=mw, period=4000)
        else:
            error_msg = result.get('message', 'Unknown error')
            aqt.utils.showWarning(
                f"Deck imported with media errors:\n{error_msg}\n\nYour notes were imported successfully, but some media files may be missing.", 
                parent=mw,
                title="Import Warning"
            )
    
    def _get_all_notes_recursive(self):
        """
        Recursively collects all notes from this deck and its children.
        Returns a flat list of Note objects.
        """
        all_notes = list(self.notes)
        
        for child in self.children:
            all_notes.extend(child._get_all_notes_recursive())
        
        return all_notes
            
    def handle_notetype_changes(self, collection: Collection) -> bool:
        """
        Comprehensively handles all notetype changes for the deck and its children.
        
        This process:
        1. Identifies and resolves notetype duplicates
        2. Creates/updates notetypes with modern API
        3. Changes note types of existing notes with proper field mapping
        4. Handles all edge cases and provides detailed error reporting
        
        Returns:
            bool: True if all operations succeeded, False if any failed
        """
        if not self.metadata or not self.metadata.models:
            logger.warning("No note models found in deck metadata")
            return True
        
        success = True
        failed_operations = []
        
        try:
            logger.info(f"Starting notetype management for {len(self.metadata.models)} models")
            
            # Phase 1: Handle duplicate detection and merging
            success &= self._handle_notetype_duplicates(collection, failed_operations)            
            logger.info("Notetype duplicates handled, proceeding with creation/update")
            
            # Phase 2: Create/update all notetypes
            success &= self._create_and_update_notetypes(collection, failed_operations)
            logger.info("Notetypes created/updated, proceeding with existing note type changes")
            
            # Phase 3: Change notetypes of existing notes
            success &= self._change_existing_note_types(collection, failed_operations)
            logger.info("Existing note types changed, proceeding with final validation")
            
            # Phase 4: Clean up and validate
            success &= self._validate_notetype_operations(collection, failed_operations)
            logger.info("Notetype operations completed, validating final state")
            
            if failed_operations:
                logger.warning(f"Some notetype operations failed: {failed_operations}")
            else:
                logger.info("All notetype operations completed successfully")
            
            return success
            
        except Exception as e:
            logger.error(f"Critical error in notetype management: {e}")
            return False

    def _handle_notetype_duplicates(self, collection: Collection, failed_operations: List[str]) -> bool:
        """
        Identifies and resolves notetype duplicates using UUID-based comparison.
        Ensures UUID_FIELD_NAME is always preserved for cross-user compatibility.
        """
        try:            
            # Ensure metadata exists
            if not self.metadata or not self.metadata.models:
                logger.warning("No metadata models available for duplicate handling")
                return True
            
            # Check for conflicts with our remote notetypes
            for note_model in self.metadata.models.values():
                model_name = note_model.anki_dict.get("name", "")
                remote_uuid = note_model.get_uuid()
                
                # should be present for all models
                if not remote_uuid:
                    logger.warning(f"Notetype '{model_name}' has no UUID, skipping duplicate check")
                    continue                
                
                # Find existing local models with same name
                existing_models_with_name = []
                for existing_nt in collection.models.all():
                    if existing_nt.get("name", "").startswith(model_name):
                        existing_models_with_name.append(existing_nt)
                
                if existing_models_with_name:
                    # Try to find a compatible local model to merge with
                    compatible_notetype = None
                    
                    for local_notetype in existing_models_with_name:
                        
                        # check structural compatibility
                        if self._are_notetypes_compatible(note_model, local_notetype):
                            compatible_notetype = local_notetype
                            logger.info(f"Found compatible local notetype for '{model_name}', will merge")
                            break
                    
                    if compatible_notetype:
                        # Update the remote model to use the local ID while preserving UUID tracking
                        note_model.anki_dict["id"] = compatible_notetype["id"]
                        #note_model.anki_dict["flds"] = compatible_model["flds"]
                        
                        # Critical: Always preserve the remote UUID for tracking
                        if UUID_FIELD_NAME not in note_model.anki_dict:
                            new_uuid = str(uuid.uuid1())
                            note_model.anki_dict[UUID_FIELD_NAME] = new_uuid
                            remote_uuid = note_model.get_uuid()
                        
                        # Also ensure the local model has the new UUID for future tracking
                        compatible_notetype[UUID_FIELD_NAME] = remote_uuid
                        collection.models.update_dict(compatible_notetype)
                    else:
                        logger.warning(f"Found incompatible local notetype '{model_name}', will rename remote")
                        # Rename the remote notetype to avoid conflicts
                        note_model.anki_dict["original_name"] = model_name
                        note_model.anki_dict["name"] = f"{model_name} (AnkiCollab)"
                        # UUID is still preserved for tracking
            
            return True
            
        except Exception as e:
            failed_operations.append(f"Duplicate handling: {e}")
            logger.error(f"Error handling notetype duplicates: {e}")
            return False

    def _create_and_update_notetypes(self, collection: Collection, failed_operations: List[str]) -> bool:
        """
        Creates new notetypes and updates existing ones using the modern NoteModel implementation.
        Ensures UUID_FIELD_NAME is preserved for all notetypes.
        """
        success = True
        
        # Ensure metadata exists
        if not self.metadata or not self.metadata.models:
            logger.warning("No metadata models available for creation/update")
            return True
        
        for note_model in self.metadata.models.values():
            try:
                model_name = note_model.anki_dict.get("name", "unknown")
                logger.info(f"Processing notetype '{model_name}' for creation/update")
                # Critical: Ensure UUID field is preserved before saving
                if UUID_FIELD_NAME not in note_model.anki_dict:
                    new_uuid = str(uuid.uuid1())
                    note_model.anki_dict[UUID_FIELD_NAME] = new_uuid
                    logger.info(f"Adding UUID field to notetype '{model_name}', uuid: {new_uuid}")

                # Use the new save_to_collection method which handles everything and captures field mapping
                new_notetype_dict, field_mapping = note_model.save_to_collection(collection)
                # Store field mapping for later use in note processing
                if field_mapping is not None:
                    logger.info(f"Notetype '{model_name}' saved with field mapping: {field_mapping}")
                    note_model_uuid = note_model.get_uuid()
                    self._field_mappings[note_model_uuid] = field_mapping
                if new_notetype_dict is not None:
                    # Update the note model with the new notetype dict fields. we keep all else 
                    # in the server version because stuff like naming is used to identify the protected fields (ikik this is getting confusing)
                    note_model.anki_dict['flds'] = new_notetype_dict['flds']
                    logger.info(f"Notetype '{model_name}' created/updated successfully")
                
            except Exception as e:
                model_name = note_model.anki_dict.get("name", "unknown")
                failed_operations.append(f"Error with notetype '{model_name}': {e}")
                logger.error(f"Failed to process notetype '{model_name}': {e}")
                success = False
        
        return success

    def _change_existing_note_types(self, collection: Collection, failed_operations: List[str]) -> bool:
        """
        Changes the notetype of existing notes that need to be updated.
        """
        try:
            # Collect all notes from this deck and children
            all_notes = self._get_all_notes_recursive()
            if not all_notes:
                return True
            
            logger.info(f"Processing notetype changes for {len(all_notes)} notes")
            
            note_guid_to_target_uuid = {note.get_uuid(): note.note_model_uuid for note in all_notes}
            
            fetcher = UuidFetcher(collection)
            all_local_models = {model["id"]: model for model in collection.models.all()}
            
            # Build UUID to model ID mappings for all relevant models
            local_mid_to_uuid = {}
            uuid_to_model_dict = {}
            
            for model in all_local_models.values():
                model_uuid = model.get(UUID_FIELD_NAME)
                if model_uuid:
                    local_mid_to_uuid[model["id"]] = model_uuid
                    uuid_to_model_dict[model_uuid] = model
            
            # Add target models from metadata
            if self.metadata and self.metadata.models:
                for target_model in self.metadata.models.values():
                    target_uuid = target_model.get_uuid()
                    uuid_to_model_dict[target_uuid] = target_model.anki_dict
            
            note_guids = list(note_guid_to_target_uuid.keys())
            
            LARGE_BATCH_SIZE = 10000
            note_type_changes = {}  # (old_mid, new_mid) -> [note_ids]
            problem_notes = []
            
            logger.info(f"Fetching database info for {len(note_guids)} notes...")
            
            for i in range(0, len(note_guids), LARGE_BATCH_SIZE):
                batch_guids = note_guids[i:i + LARGE_BATCH_SIZE]
                placeholders = ', '.join('?' for _ in batch_guids)
                query = f"SELECT id, mid, guid FROM notes WHERE guid IN ({placeholders})"
                
                try:
                    if collection.db:
                        notes_in_db = collection.db.all(query, *batch_guids)
                    else:
                        logger.error("Database connection not available")
                        continue
                    
                    for note_id, current_mid, note_guid in notes_in_db:
                        target_uuid = note_guid_to_target_uuid.get(note_guid)
                        if not target_uuid:
                            continue
                        
                        current_uuid = local_mid_to_uuid.get(current_mid)
                        if not current_uuid:
                            # Fix missing UUID: add it to the local notetype
                            current_model = all_local_models.get(current_mid)
                            if current_model:
                                # Generate and assign a UUID to the local notetype
                                new_uuid = str(uuid.uuid1())
                                current_model[UUID_FIELD_NAME] = new_uuid
                                collection.models.update_dict(current_model)
                                local_mid_to_uuid[current_mid] = new_uuid
                                current_uuid = new_uuid
                                logger.info(f"Added missing UUID {new_uuid} to notetype '{current_model.get('name', current_mid)}'")
                            else:
                                problem_notes.append(f"Note {note_guid}: current notetype {current_mid} not found in collection")
                                continue
                        
                        if current_uuid == target_uuid:
                            continue
                        
                        target_model_dict = uuid_to_model_dict.get(target_uuid)
                        if not target_model_dict:
                            problem_notes.append(f"Note {note_guid}: target model {target_uuid} not found")
                            continue
                        
                        target_mid = target_model_dict.get("id")
                        if not target_mid:
                            problem_notes.append(f"Note {note_guid}: target model {target_uuid} has no ID")
                            continue
                        
                        change_key = (current_mid, target_mid)
                        if change_key not in note_type_changes:
                            note_type_changes[change_key] = []
                        note_type_changes[change_key].append(note_id)
                
                except Exception as e:
                    logger.warning(f"Error processing note batch {i//LARGE_BATCH_SIZE + 1}: {e}")
                    continue
            
            logger.info(f"Found {len(note_type_changes)} different notetype changes to apply")
            
            # todo report these issues to the user in a dialog
            if problem_notes:
                logger.warning(f"Found {len(problem_notes)} problematic notes that were skipped")
                for problem in problem_notes[:10]:
                    logger.warning(problem)
                if len(problem_notes) > 10:
                    logger.warning(f"... and {len(problem_notes) - 10} more")
            
            success = True
            total_notes_changed = 0
            notes_requiring_review = []
            
            for (old_mid, new_mid), note_ids in note_type_changes.items():
                if not note_ids:
                    continue
                
                try:
                    # Get model info (already cached)
                    old_model_dict = all_local_models.get(old_mid)
                    new_model_dict = all_local_models.get(new_mid)
                    
                    if not old_model_dict or not new_model_dict:
                        logger.warning(f"Could not find models for change {old_mid} -> {new_mid}")
                        success = False
                        continue
                    
                    # Check if this change might cause data loss
                    requires_review = self._assess_change_risk(old_model_dict, new_model_dict)
                    
                    if requires_review:
                        old_name = old_model_dict.get("name", "unknown")
                        new_name = new_model_dict.get("name", "unknown")
                        notes_requiring_review.append(f"Notetype change '{old_name}' -> '{new_name}' ({len(note_ids)} notes)")
                    
                    batch_success = self._apply_notetype_change_optimized(collection, old_mid, new_mid, note_ids)
                    if batch_success:
                        total_notes_changed += len(note_ids)
                        logger.info(f"Successfully changed {len(note_ids)} notes from {old_model_dict.get('name')} to {new_model_dict.get('name')}")
                    else:
                        success = False
                        
                except Exception as e:
                    failed_operations.append(f"Notetype change {old_mid} -> {new_mid}: {e}")
                    logger.error(f"Failed to change notetype for {len(note_ids)} notes: {e}")
                    success = False
            
            # Report results and any required manual review
            if total_notes_changed > 0:
                logger.info(f"Successfully changed {total_notes_changed} notes to new notetypes")
            
            if notes_requiring_review:
                logger.warning("MANUAL REVIEW REQUIRED for the following notetype changes:")
                for review_note in notes_requiring_review:
                    logger.warning(f"  {review_note}")
                logger.warning("Please manually check these notes and consider reverting to backup if data is incorrect")
            
            return success
            
        except Exception as e:
            failed_operations.append(f"Note type changes: {e}")
            logger.error(f"Error in notetype changes: {e}")
            return False

    def _assess_change_risk(self, old_model: Dict, new_model: Dict) -> bool:
        """
        Assesses whether a notetype change might cause data loss requiring manual review.
        """
        try:
            old_fields = [f["name"].lower() for f in old_model.get("flds", [])]
            new_fields = [f["name"].lower() for f in new_model.get("flds", [])]
            
            # Check for removed fields (potential data loss)
            removed_fields = set(old_fields) - set(new_fields)
            if removed_fields:
                return True
            
            # Check for significant field reordering (potential mapping issues)
            if len(old_fields) > 1 and len(new_fields) > 1:
                # Check if field order changed significantly
                common_fields = [f for f in old_fields if f in new_fields]
                if len(common_fields) >= 2:
                    old_positions = {field: i for i, field in enumerate(old_fields) if field in common_fields}
                    new_positions = {field: i for i, field in enumerate(new_fields) if field in common_fields}
                    
                    # Count position changes
                    position_changes = sum(1 for field in common_fields if old_positions[field] != new_positions[field])
                    if position_changes > len(common_fields) // 2:  # More than half changed positions
                        return True
            
            return False
            
        except Exception:
            # If we can't assess risk, err on the side of caution
            return True

    def _apply_notetype_change_optimized(self, collection: Collection, old_mid: int, new_mid: int, note_ids: List[int]) -> bool:
        """
        Optimized version of notetype change for large batches.
        Uses larger batch sizes and optimized field mapping lookups.
        """
        if not note_ids:
            return True

        OPTIMIZED_BATCH_SIZE = 5000
        total_success = True
        
        for i in range(0, len(note_ids), OPTIMIZED_BATCH_SIZE):
            batch = note_ids[i:i + OPTIMIZED_BATCH_SIZE]
            try:
                batch_success = self._apply_notetype_change(collection, old_mid, new_mid, batch)
                if not batch_success:
                    logger.warning(f"Large batch {i//OPTIMIZED_BATCH_SIZE + 1} failed, trying smaller batches")
                    # Fallback to smaller batches if large batch fails
                    for j in range(0, len(batch), CHUNK_SIZE):
                        mini_batch = batch[j:j + CHUNK_SIZE]
                        try:
                            mini_success = self._apply_notetype_change(collection, old_mid, new_mid, mini_batch)
                            if not mini_success:
                                total_success = False
                        except Exception as e:
                            logger.error(f"Mini-batch failed: {e}")
                            total_success = False
                else:
                    # Large batch succeeded
                    logger.debug(f"Successfully processed large batch of {len(batch)} notes")
            except Exception as e:
                logger.error(f"Exception in optimized batch {i//OPTIMIZED_BATCH_SIZE + 1}: {e}")
                total_success = False
        
        return total_success

    def _apply_notetype_change(self, collection: Collection, old_mid: int, new_mid: int, note_ids: List[int]) -> bool:
        """
        Applies a notetype change to a group of notes using modern API with intelligent field mapping.
        Automatically maps fields by name with fallback strategies to prevent data loss.
        """
        try:
            # Get both old and new notetypes with proper type handling
            old_notetype = collection.models.get(NotetypeId(old_mid))
            new_notetype = collection.models.get(NotetypeId(new_mid))
            
            if not old_notetype or not new_notetype:
                logger.error(f"Could not find notetypes: old={old_mid}, new={new_mid}")
                return False
            
            old_name = old_notetype.get("name", f"ID{old_mid}")
            new_name = new_notetype.get("name", f"ID{new_mid}")
            
            # Field mapping is now captured during notetype save in _create_and_update_notetypes
            # and stored in self._field_mappings[note_model_uuid]
            note_model_uuid = new_notetype.get(UUID_FIELD_NAME)
            if note_model_uuid and note_model_uuid in self._field_mappings:
                field_map = self._field_mappings[note_model_uuid]
            else:
                # Fallback: create a simple 1:1 mapping if no mapping was captured
                field_map = list(range(len(new_notetype.get('flds', []))))
            
            # Get current schema with error handling
            try:
                if collection.db:
                    current_schema = collection.db.scalar("select scm from col")
                else:
                    current_schema = 0
            except Exception as e:
                logger.warning(f"Could not get current schema: {e}, using 0")
                current_schema = 0
            
            # Create the change request with our intelligent mapping
            request = ChangeNotetypeRequest(
                note_ids=note_ids,
                old_notetype_id=old_mid,
                new_notetype_id=new_mid,
                current_schema=current_schema,
                new_fields=field_map,
            )
            
            # Apply the change
            collection.models.change_notetype_of_notes(request)
            
            logger.info(f"Successfully changed {len(note_ids)} notes from '{old_name}' to '{new_name}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to apply notetype change {old_mid} -> {new_mid}: {e}")
            # Log the error but don't lose notes - they keep their old notetype
            return False

    def _validate_notetype_operations(self, collection: Collection, failed_operations: List[str]) -> bool:
        """
        Validates that all notetype operations completed successfully using UUID-based checks.
        Ensures UUID_FIELD_NAME preservation was successful.
        """
        try:
            # Check that all required notetypes exist
            missing_notetypes = []
            uuid_validation_failures = []
            fetcher = UuidFetcher(collection)
            
            # Ensure metadata exists
            if not self.metadata or not self.metadata.models:
                logger.warning("No metadata models available for validation")
                return True
            
            for note_model in self.metadata.models.values():
                model_uuid = note_model.get_uuid()
                model_name = note_model.anki_dict.get("name", "unknown")
                
                # Try to find the model by UUID
                actual_notetype = fetcher.get_model(model_uuid)
                if not actual_notetype:
                    missing_notetypes.append(f"{model_name} ({model_uuid})")
                else:
                    # Critical: Validate that UUID field is preserved
                    if UUID_FIELD_NAME not in actual_notetype:
                        uuid_validation_failures.append(f"{model_name} ({model_uuid})")
                    elif actual_notetype.get(UUID_FIELD_NAME) != model_uuid:
                        uuid_validation_failures.append(f"{model_name} (UUID mismatch: expected {model_uuid}, got {actual_notetype.get(UUID_FIELD_NAME)})")
            
            if missing_notetypes:
                failed_operations.append(f"Missing notetypes after operations: {missing_notetypes}")
                logger.error(f"Validation failed: missing notetypes {missing_notetypes}")
                return False
            
            if uuid_validation_failures:
                failed_operations.append(f"UUID validation failures: {uuid_validation_failures}")
                logger.error(f"Critical: UUID field validation failed for {uuid_validation_failures}")
                # This is critical for cross-user compatibility
                return False
            
            return True
            
        except Exception as e:
            failed_operations.append(f"Validation: {e}")
            logger.error(f"Error in notetype validation: {e}")
            return False

    def _are_notetypes_compatible(self, remote_model: NoteModel, local_notetype: Dict) -> bool:
        """
        Determines if two notetypes are compatible for merging using UUID-based approach.
        """
        try:
            # Check if this is a projektanki note type (preserve templates)
            remote_notetype = remote_model.anki_dict
            note_type_name = remote_notetype.get("name", "").lower()
            should_preserve_templates = "projektanki" in note_type_name
            
            if should_preserve_templates:
                # For projektanki: we do a very lenient fields-only check
                return self._check_fields_compatible(remote_notetype, local_notetype)
            else:
                # For non-projektanki: we ensure full compatibility in both fields and templates
                return not remote_model._detect_changes_needed(local_notetype, False)

        except Exception:
            return False

    def _check_fields_compatible(self, remote_notetype: Dict, local_notetype: Dict) -> bool:
        """Check if field structures are compatible (projektanki approach)
        
        Considers notetypes compatible if:
        1. All remote fields exist in local notetype (exact match)
        2. Local notetype can have additional fields
        """
        try:
            remote_fields = [f["name"] for f in remote_notetype.get("flds", [])]
            local_fields = [f["name"] for f in local_notetype.get("flds", [])]
            
            # Check if all remote fields exist in the local notetype
            # Local can have additional custom fields users added - that's fine
            remote_fields_set = set(remote_fields)
            local_fields_set = set(local_fields)
            
            # Compatible if remote is a subset of local (or equal)
            is_compatible = remote_fields_set.issubset(local_fields_set)
            
            if is_compatible and len(local_fields) > len(remote_fields):
                extra_fields = local_fields_set - remote_fields_set
                logger.info(f"Local notetype has {len(extra_fields)} additional fields: {list(extra_fields)} - this is compatible")
            
            return is_compatible
        except Exception:
            return False

    def save_metadata(self, collection: Collection, home_deck: Optional[str] = None):
        """
        Saves deck-related metadata (deck configurations) and handles notetype management.
        """
        
        # Handle notetype changes first (create/update notetypes and change existing notes)
        if not self.handle_notetype_changes(collection):
            logger.warning("Some notetype operations failed, but continuing with deck import")
        
        # Save deck configurations
        if self.metadata and self.metadata.deck_configs:
            for config in self.metadata.deck_configs.values():
                config.save_to_collection(collection)
        
        self._save_deck(collection, "", home_deck, self.anki_dict["name"])
    
    def save_decks_and_notes_bulk(self, collection, progress_tracker, import_config: ImportConfig):
        """
        New bulk import strategy:
        1. Collect all notes from entire deck tree
        2. Bulk import all notes to root deck
        3. Create deck structure
        4. Move notes to correct decks
        5. Start media download in background
        """
        # Validate inputs
        if not collection:
            raise ValueError("Collection is required")
        if not import_config:
            raise ValueError("Import config is required")
        if not self.metadata:
            raise ValueError("Deck metadata is required")
            
        try:
            all_notes = []
            note_to_deck_map = {}  # note_uuid -> full_deck_name
            logger.info("Starting collection of notes...")
            self._collect_all_notes(all_notes, note_to_deck_map, "", import_config.home_deck)
            
            if not all_notes:
                return 0, {"success": True, "downloaded": 0, "skipped": 0}
            
            progress_tracker.set_phase_label("Processing notes...")
            logger.info("Starting bulk processing of notes...")
            status_cur = self._bulk_process_all_notes(collection, all_notes, import_config, progress_tracker, note_to_deck_map)
            
            progress_tracker.set_phase_label("Creating deck structure...")
            server_root_name = self.anki_dict["name"]  # Get server root name
            logger.info(f"Creating deck structure with root name: {server_root_name}")
            root_deck_name = self._create_deck_structure(collection, "", import_config.home_deck, server_root_name)
            
            progress_tracker.set_phase_label("Organizing notes into decks...")
            logger.info("Organizing notes into decks based on collected mapping...")
            Note._move_notes_to_decks(collection, note_to_deck_map, import_config)
            self.root_deck_id = collection.decks.id(root_deck_name)
            
            media_files = self.get_media_file_list(data_from_models=True, include_children=True)
            
            if media_files:
                # Check which files actually need downloading
                dir_path = collection.media.dir()
                missing_files = [f for f in media_files if not os.path.exists(os.path.join(dir_path, f))]
                
                if missing_files:
                    progress_tracker.set_phase_label("Preparing media download...")
                    progress_tracker.update_media_progress(0, len(missing_files))
                    media_result = {
                        "success": True, 
                        "downloaded": 0, 
                        "skipped": len(media_files) - len(missing_files),
                        "deck_hash": import_config.deck_hash,
                        "missing_files": missing_files
                    }
                else:
                    # All files present - update progress to show completion
                    progress_tracker.update_media_progress(len(media_files), 0)
                    media_result = {"success": True, "downloaded": 0, "skipped": len(media_files)}
            else:
                media_result = {"success": True, "downloaded": 0, "skipped": 0}
                
            return status_cur, media_result
            
        except Exception as e:
            logger.error(f"Error in bulk import: {str(e)}")
            # Clean up any partial state
            try:
                temp_deck_pattern = "_ankicollab_import_"
                deck_ids_to_remove = []
                for deck_name_id in collection.decks.all_names_and_ids():
                    if deck_name_id.name.startswith(temp_deck_pattern):
                        deck_ids_to_remove.append(deck_name_id.id)
                if deck_ids_to_remove:
                    collection.decks.remove(deck_ids_to_remove)
            except:
                pass
            
            # Re-raise the exception rather than fallback to avoid data corruption
            raise ImportError(f"Bulk import failed: {str(e)}") from e
    
    def _collect_all_notes(self, all_notes, note_to_deck_map, parent_name, home_deck, server_root_name=None):
        """Recursively collect all notes and build note->deck mapping with smart subdeck mapping"""
        # For root deck, establish the server root name for mapping
        if not parent_name and not server_root_name:
            server_root_name = self.anki_dict["name"]
        
        full_name = self._get_full_deck_name(parent_name, home_deck, server_root_name)
        
        # Add this deck's notes
        for note in self.notes:
            all_notes.append(note)
            if note.get_uuid() in note_to_deck_map:
                logger.warning(f"Note {note.get_uuid()} already mapped to deck {note_to_deck_map[note.get_uuid()]}, overwriting with {full_name}")
            note_to_deck_map[note.get_uuid()] = full_name
            
        
        # Recursively process children, passing the server root name
        for child in self.children:
            child._collect_all_notes(all_notes, note_to_deck_map, full_name, None, server_root_name)
    
    def _get_full_deck_name(self, parent_name, home_deck, server_root_name=None):
        """Get the full deck name with smart subdeck mapping
        
        Args:
            parent_name: The parent deck name being built
            home_deck: User's configured home deck (only for root)
            server_root_name: Original server root deck name for mapping
        """
        current_deck_name = self.anki_dict["name"]
        
        # Root deck: use home deck if specified
        if not parent_name and home_deck:
            return home_deck
            
        # For subdecks: implement smart mapping
        if parent_name and home_deck and server_root_name:
            # Check if parent_name starts with home_deck (we're in the mapped structure)
            if parent_name.startswith(home_deck):
                # Continue building under home deck structure
                result = f"{parent_name}{self.DECK_NAME_DELIMITER}{current_deck_name}"
                return result
            else:
                # This shouldn't happen in normal flow, fallback to standard behavior
                result = f"{parent_name}{self.DECK_NAME_DELIMITER}{current_deck_name}"
                logger.warning(f"Unexpected subdeck mapping: {current_deck_name} -> {result}")
                return result
        
        # Standard behavior for non-mapped scenarios
        result = (parent_name + self.DECK_NAME_DELIMITER if parent_name else "") + current_deck_name
        return result
    
    def _bulk_process_all_notes(self, collection, all_notes, import_config, progress_tracker, note_to_deck_map):
        """Process all notes in bulk with optimized database operations"""
        if not all_notes:
            return 0
            
        int_time = anki.utils.int_time()
        total_notes = len(all_notes)
        
        note_uuid_cache = {}  # note -> uuid mapping to avoid repeated calls
        note_uuids = []
        for note in all_notes:
            uuid = note.get_uuid()
            note_uuid_cache[note] = uuid
            note_uuids.append(uuid)
        
        existing_notes = []
        for i in range(0, len(note_uuids), CHUNK_SIZE):
            chunk = note_uuids[i:i+CHUNK_SIZE]
            placeholders = ','.join('?' * len(chunk))
            try:
                existing_notes += collection.db.all(
                    f"SELECT guid, id FROM notes WHERE guid IN ({placeholders})", *chunk
                )
            except Exception as e:
                logger.warning(f"Error fetching existing notes chunk: {e}")
                continue
                
        existing_note_set = set(guid for guid, _ in existing_notes)
        existing_note_map = {guid: nid for guid, nid in existing_notes}
        
        has_separate_new_notes_deck = bool(import_config.new_notes_home_deck and 
                                          import_config.new_notes_home_deck.strip() and
                                          import_config.new_notes_home_deck != import_config.home_deck)
        
        home_deck_prefix = None
        home_deck_prefix_len = 0
        if has_separate_new_notes_deck:
            home_deck = import_config.home_deck or ""
            if home_deck:
                home_deck_prefix = home_deck
                home_deck_prefix_len = len(home_deck)
        
        notes_by_model_new = defaultdict(list)  # model_uuid -> [new_notes]
        notes_by_model_update = defaultdict(list)  # model_uuid -> [update_notes]
        
        # Ensure metadata exists
        if not self.metadata:
            logger.error("No metadata available for processing notes")
            return 0
        
        # Single iteration to do everything at once
        for note in all_notes:
            note_uuid = note_uuid_cache[note]
            note_model_uuid = note.note_model_uuid
            
            # Skip notes with missing models upfront
            if note_model_uuid not in self.metadata.models:
                logger.warning(f"Note model {note_model_uuid} not found in metadata, skipping note {note_uuid}")
                continue
            
            # Update deck mapping for this note
            target_deck = note_to_deck_map.get(note_uuid)
            if target_deck:
                if note_uuid in existing_note_set:
                    # Existing note - keep original mapping
                    pass  # target_deck already correct
                else:
                    # New note - apply new notes deck mapping if configured
                    if has_separate_new_notes_deck and home_deck_prefix and target_deck.startswith(home_deck_prefix):
                        relative_path = target_deck[home_deck_prefix_len:].lstrip(self.DECK_NAME_DELIMITER)
                        if relative_path:
                            note_to_deck_map[note_uuid] = f"{import_config.new_notes_home_deck}{self.DECK_NAME_DELIMITER}{relative_path}"
                        else:
                            note_to_deck_map[note_uuid] = import_config.new_notes_home_deck
                    elif has_separate_new_notes_deck:
                        note_to_deck_map[note_uuid] = import_config.new_notes_home_deck
            
            # Categorize note by model and new/update status
            if note_uuid in existing_note_set:
                notes_by_model_update[note_model_uuid].append(note)
            else:
                notes_by_model_new[note_model_uuid].append(note)
        
        import uuid as uuid_module
        temp_deck_name = f"_ankicollab_import_{uuid_module.uuid4().hex[:8]}"
        root_deck_id = collection.decks.id(temp_deck_name)
        
        processed_count = 0
        progress_update_interval = max(1, total_notes // 20)  # Update progress ~20 times total
        
        all_new_notes = []
        all_update_notes = []
        
        # Process all model groups
        all_model_uuids = set(notes_by_model_new.keys()) | set(notes_by_model_update.keys())
        
        for note_model_uuid in all_model_uuids:
            try:
                note_model = self.metadata.models[note_model_uuid]
                field_mapping = self._field_mappings.get(note_model_uuid)
                
                model_new_notes = notes_by_model_new.get(note_model_uuid, [])
                model_update_notes = notes_by_model_update.get(note_model_uuid, [])
                
                total_model_notes = len(model_new_notes) + len(model_update_notes)
                logger.info(f"Processing {total_model_notes} notes for model {note_model.anki_dict.get('name', 'unknown')} ({len(model_new_notes)} new, {len(model_update_notes)} updates)")
                
                if model_new_notes:
                    self._batch_process_new_notes(model_new_notes, collection, note_model, 
                                                field_mapping, import_config, int_time)
                    all_new_notes.extend(model_new_notes)
                
                if model_update_notes:
                    self._batch_process_update_notes(model_update_notes, collection, note_model,
                                                   field_mapping, import_config, int_time, existing_note_map)
                    all_update_notes.extend(model_update_notes)
                
                processed_count += total_model_notes
                
                if processed_count % progress_update_interval == 0 or processed_count == total_notes:
                    progress_tracker.update_notes_progress(processed_count)
                    if mw.progress.want_cancel():
                        return processed_count
                        
            except Exception as e:
                logger.warning(f"Error processing note model {note_model_uuid}: {e}")
                continue
        
        try:
            if all_new_notes:
                Note.bulk_add_notes(collection, all_new_notes, root_deck_id, import_config)
            
            if all_update_notes:
                Note._bulk_update_notes_preserving_placement(collection, all_update_notes, note_to_deck_map, import_config)
            
        except Exception as e:
            logger.error(f"Error in bulk note operations: {e}")
            raise
            
        return processed_count
    
    def _batch_process_new_notes(self, notes_batch, collection, note_model, field_mapping, import_config, int_time):
        """Batch process new notes for better performance"""
        self._batch_process_notes(notes_batch, collection, note_model, field_mapping, import_config, int_time, 
                                 is_new=True, existing_note_map=None)
    
    def _batch_process_update_notes(self, notes_batch, collection, note_model, field_mapping, import_config, int_time, existing_note_map):
        """Batch process update notes for better performance"""
        self._batch_process_notes(notes_batch, collection, note_model, field_mapping, import_config, int_time, 
                                 is_new=False, existing_note_map=existing_note_map)
    
    def _batch_process_notes(self, notes_batch, collection, note_model, field_mapping, import_config, int_time, is_new, existing_note_map):
        """Unified batch processing for notes with error handling and fallback"""
        def process_single_note(note):
            """Process a single note - extracted for reuse in fallback"""
            if is_new:
                note.anki_object = AnkiNote(collection, note_model.anki_dict)
            else:
                note_uuid = note.get_uuid()
                note.anki_object = AnkiNote(collection, id=existing_note_map[note_uuid])
            
            note.handle_import_config_changes(import_config, note_model, field_mapping)
            note.anki_object.__dict__.update(note.anki_object_dict)
            note.anki_object.mid = note_model.anki_dict["id"]
            note.anki_object.mod = int_time
        
        try:
            # Try batch processing first
            for note in notes_batch:
                process_single_note(note)
        except Exception as e:
            note_type = "new" if is_new else "update"
            logger.warning(f"Error in batch processing {note_type} notes: {e}")
            # Fallback to individual processing with per-note error handling
            for note in notes_batch:
                try:
                    process_single_note(note)
                except Exception as note_error:
                    logger.warning(f"Error processing individual {note_type} note {note.get_uuid()}: {note_error}")
        
    def _create_deck_structure(self, collection, parent_name, home_deck, server_root_name=None):
        """Create the entire deck structure with smart subdeck mapping"""
        try:
            # For the root deck, establish server_root_name
            if not parent_name and not server_root_name:
                server_root_name = self.anki_dict["name"]
            
            full_name = self._save_deck(collection, parent_name, home_deck, server_root_name)
            
            # For child decks, we need to maintain the home deck context
            # but ensure proper relative structure mapping
            for child in self.children:
                child._create_deck_structure(collection, full_name, None, server_root_name)
                
            return full_name
        except Exception as e:
            logger.warning(f"Error creating deck structure for {self.anki_dict.get('name', 'unknown')}: {e}")
            return parent_name
    
    def _show_media_completion(self, result):
        """Show media download completion notification - now handled by progress indicator"""
        # The beautiful progress indicator now handles completion messages
        # No need for additional tooltips that interrupt the user
        if result and not result.get("success"):
            # Only show errors that weren't already shown by the progress indicator
            error_msg = result.get('message', 'Unknown error')
            logger.error(f"Media download error: {error_msg}")
        else:
            # Success cases are beautifully handled by the progress indicator
            logger.info("Media download completed successfully")

    
    def _save_deck(self, collection, parent_name, home_deck, server_root_name=None):        
        # Use the smart mapping logic for deck names
        full_name = self._get_full_deck_name(parent_name, home_deck, server_root_name)
        deck_dict = UuidFetcher(collection).get_deck(self.get_uuid())

        # For root deck, if we have a configured home_deck, use it directly
        # This prevents creating duplicate "(AnkiCollab)" decks on subsequent imports
        if not parent_name and home_deck:
            full_name = home_deck
            deck_id = collection.decks.id(full_name, create=False)
            if deck_id:
                deck_dict = collection.decks.get(deck_id)
            else:
                # Home deck was configured but doesn't exist - create it
                new_deck_id = collection.decks.id(full_name)
                deck_dict = collection.decks.get(new_deck_id)
        else:
            # For subdecks or when no home_deck is configured, check for conflicts
            deck_id = collection.decks.id(full_name, create=False)
            
            # Only create new deck for root deck. Subdecks get overwritten.
            if deck_id and (not deck_dict or deck_dict["id"] != deck_id):
                if not parent_name:
                    # Root deck conflict without configured home_deck - rename it
                    logger.warning(f"Root deck conflict for '{full_name}', renaming")
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
