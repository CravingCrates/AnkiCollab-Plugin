import asyncio
import json
import os
import re
from threading import current_thread, main_thread
import traceback
import requests
import functools

import aqt
import aqt.utils
import anki
from anki.notes import NoteId
from anki.utils import point_version

from aqt.qt import *
from aqt import mw
import aqt.utils
from aqt.operations import QueryOp
from anki.utils import ids2str, join_fields, split_fields
from anki.errors import NotFoundError
from aqt.errors import show_exception
from datetime import datetime, timedelta, timezone
import base64
import gzip
import logging
from concurrent.futures import Future # Keep for main thread sync

from typing import Callable, cast, Tuple, Dict, List, Any, Optional, Sequence

from .crowd_anki.representation.note_model import NoteModel

from .crowd_anki.utils.uuid import UuidFetcher

from .var_defs import DEFAULT_PROTECTED_TAGS, PREFIX_PROTECTED_FIELDS

from .dialogs import RateAddonDialog

from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.note import Note
from .crowd_anki.config.config_settings import ConfigSettings
from .crowd_anki.export.note_sorter import NoteSorter
from .crowd_anki.utils.disambiguate_uuids import disambiguate_note_model_uuids

from .crowd_anki.representation import *
from .crowd_anki.representation import deck_initializer
from .crowd_anki.anki.adapters.anki_deck import AnkiDeck
from .crowd_anki.representation.deck import Deck

from .auth_manager import auth_manager
from .var_defs import API_BASE_URL

from .utils import get_deck_hash_from_did, get_local_deck_from_hash, get_timestamp, get_did_from_hash, create_backup
from . import main
logger = logging.getLogger("ankicollab")

# Define and compile regexes for various media types
SOUND_REGEX_STRINGS = [r"(?i)(\[sound:(?P<fname>[^]]+)\])"]
HTML_MEDIA_REGEX_STRINGS = [
    # src element quoted case for img, audio, source
    r"(?i)(<(?:img|audio|source)\b[^>]* src=(?P<str>[\"'])(?P<fname>[^>]+?)(?P=str)[^>]*>)",
    # unquoted case for img, audio, source
    r"(?i)(<(?:img|audio|source)\b[^>]* src=(?!['\"])(?P<fname>[^ >]+)[^>]*?>)",
    # data element quoted case for object
    r"(?i)(<object\b[^>]* data=(?P<str>[\"'])(?P<fname>[^>]+?)(?P=str)[^>]*>)",
    # unquoted case for object
    r"(?i)(<object\b[^>]* data=(?!['\"])(?P<fname>[^ >]+)[^>]*?>)",
]

COMPILED_SOUND_REGEXES = [re.compile(r_str) for r_str in SOUND_REGEX_STRINGS]
COMPILED_HTML_MEDIA_REGEXES = [re.compile(r_str) for r_str in HTML_MEDIA_REGEX_STRINGS]
ALL_COMPILED_MEDIA_REGEXES = COMPILED_SOUND_REGEXES + COMPILED_HTML_MEDIA_REGEXES


def do_nothing(count: int):
    pass

def ask_for_rating():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and "settings" in strings_data:
        if "push_counter" in strings_data["settings"]:
            push_counter = strings_data["settings"]["push_counter"]
            strings_data["settings"]["push_counter"] = push_counter + 1
            if push_counter % 15 == 0: # every 15 bulk suggestions
                last_ratepls = strings_data["settings"]["last_ratepls"]
                last_ratepls_dt = datetime.strptime(last_ratepls, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_ratepls_dt).days > 14:  # only ask every 14 days
                    if not strings_data["settings"]["rated_addon"]: # only ask if they haven't rated the addon yet
                        strings_data["settings"]["last_ratepls"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                        dialog = RateAddonDialog() # UI Element - Main thread OK
                        dialog.exec()
            mw.addonManager.writeConfig(__name__, strings_data)

def get_maintainer_data(deckHash):
    token = auth_manager.get_token()
    auto_approve = auth_manager.get_auto_approve()

    if token:
        token_info = {'token': token, 'deck_hash': deckHash}
        try:
            token_check_response = requests.post(
                f"{API_BASE_URL}/CheckUserToken",
                json=token_info,
                headers={"Content-Type": "application/json"}
            )
            if token_check_response.status_code == 200:
                token_res = token_check_response.text
                if token_res != "true":
                    from .menu import force_logout # bypass circular import
                    if auth_manager.refresh_token():
                        token = auth_manager.get_token()
                        token_info['token'] = token
                        token_check_response = requests.post(
                            f"{API_BASE_URL}/CheckUserToken",
                            json=token_info,
                            headers={"Content-Type": "application/json"}
                        )
                        if token_check_response.status_code != 200 or token_check_response.text != "true":
                            mw.taskman.run_on_main(force_logout) # Schedule UI action on main thread
                            token = ""
                    else:
                        mw.taskman.run_on_main(force_logout) # Schedule UI action on main thread
                        token = ""
        except Exception as e:
            logger.error(f"Error checking token: {e}")
            # Network error, return current token but don't force logout

    return token, auto_approve

def get_personal_tags(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    combined_tags = set()

    if strings_data:
        for hash_key, details in strings_data.items():
            if hash_key == deck_hash:
                personal_tags = details.get("personal_tags", DEFAULT_PROTECTED_TAGS)
                if "personal_tags" not in details:
                    details["personal_tags"] = personal_tags
                    mw.addonManager.writeConfig(__name__, strings_data)
                combined_tags.update(personal_tags)
                combined_tags.add(PREFIX_PROTECTED_FIELDS)
                return list(combined_tags)
    return []

def get_note_id_from_guid(guid):
    try:
        note_id = mw.col.db.first("select id from notes where guid = ?", guid)
        if note_id:
            return note_id[0]
    except Exception as e:
        logger.error(f"Error getting note ID from GUID {guid}: {str(e)}")
    return None

def get_note_guid_from_id(note_id):
    try:
        guid = mw.col.db.first("select guid from notes where id = ?", note_id)
        if guid:
            return guid[0]
    except Exception as e:
        logger.error(f"Error getting GUID from note ID {note_id}: {str(e)}")
    return None

def update_media_references(filename_mapping: Dict[str, str], file_note_pairs: List[Tuple[str, str]]):
    """
    Update note references. MUST run on the main thread as it modifies the collection.
    """
    assert mw.col is not None, "Collection must be available for media reference update"
    if not filename_mapping:
        return 0, None
    notes_by_filename: Dict[str, List[str]] = {}
    for filename, note_guid in file_note_pairs:
        if filename in filename_mapping:
            notes_by_filename.setdefault(filename, []).append(note_guid)

    updated_notes = []
    for old_filename, new_filename in filename_mapping.items():
        if old_filename not in notes_by_filename:
            continue

        note_guids = notes_by_filename[old_filename]
        for note_guid in note_guids:
            try:
                note_id = get_note_id_from_guid(note_guid)
                if not note_id:
                    continue

                note = mw.col.get_note(note_id)
                if not note:
                    continue

                modified_note = False # Flag to track if the current note was modified
                for i, field_content in enumerate(note.fields):
                    # Iterate through all compiled media regexes
                    for media_regex in ALL_COMPILED_MEDIA_REGEXES:
                        # Apply the regex to the original content of the field for this pass
                        new_content = media_regex.sub(
                            lambda m: m.group(0).replace(old_filename, new_filename),
                            field_content, # Use the original field_content for each regex check
                        )
                        if new_content != field_content:
                            note.fields[i] = new_content
                            modified_note = True
                    
                if modified_note:
                    updated_notes.append(note)

            except Exception as e:
                logger.error(f"Error updating references for note {note_guid}: {str(e)}")

    if updated_notes:
        opchanges = mw.col.update_notes(notes=updated_notes)
        return len(updated_notes), opchanges
    
    return 0, None

async def handle_media_upload(user_token: str, deck_hash: str, all_files_info: List[Dict], file_paths: Dict[str, str], progress_callback_wrapper=None, silent=False) -> Dict[str, Any]:
    """
    Async function to upload media files. Returns a summary dictionary.
    The progress_callback_wrapper is expected to handle threading (e.g., run_on_main).
    """
    if not all_files_info:
        return {"uploaded": 0, "existing": 0, "failed": 0, "errors": [], "cancelled": False}

    total_files = len(all_files_info)
    batch_size = 100
    uploaded_total = 0
    skipped_total = 0
    failed_total = 0
    error_messages = []
    cancelled = False

    try:
        batches = []
        for batch_start in range(0, total_files, batch_size):
            batch_end = min(batch_start + batch_size, total_files)
            batches.append((batch_start, batch_end))

        total_batches = len(batches)

        for batch_index, (batch_start, batch_end) in enumerate(batches):
            current_batch = all_files_info[batch_start:batch_end]

            # Define the progress callback for this specific batch
            def batch_progress_inner_cb(p: float):
                if progress_callback_wrapper:
                    batch_progress_start = batch_index / total_batches
                    batch_progress_end = (batch_index + 1) / total_batches
                    scaled_progress = batch_progress_start + (p * (batch_progress_end - batch_progress_start))
                    progress_callback_wrapper(scaled_progress) # Call the wrapper

            # Process this batch
            batch_result = await main.media_manager.upload_media_bulk(
                user_token=user_token,
                files_info=current_batch,
                file_paths=file_paths,
                deck_hash=deck_hash,
                progress_callback=batch_progress_inner_cb # Pass the inner callback
            )

            # Check for cancellation *after* the await point
            if mw.progress.want_cancel():
                cancelled = True
                logger.warning("Media upload cancelled by user.")
                break # Exit the loop

            # Update totals
            if batch_result.get("success", False):
                uploaded_total += batch_result.get("uploaded", 0)
                skipped_total += batch_result.get("existing", 0)
                failed_total += batch_result.get("failed", 0)
                if "error" in batch_result and batch_result["error"]:
                    error_messages.append(str(batch_result["error"]))
            else:
                error_msg = batch_result.get("message", "Unknown error")
                error_messages.append(f"Batch {batch_index + 1}/{total_batches}: {str(error_msg)}")
                failed_total += len(current_batch) # Assume all failed if batch failed
                logger.error(f"Batch upload error: {error_msg}")

        if progress_callback_wrapper and not cancelled:
             progress_callback_wrapper(1.0) # Signal completion if not cancelled

    except Exception as e:
        logger.error(f"Error during media upload coroutine: {str(e)}")
        logger.error(traceback.format_exc())
        error_messages.append(f"Unexpected error: {str(e)}")
        # Don't re-raise here, return summary

    return {
        "uploaded": uploaded_total,
        "existing": skipped_total,
        "failed": failed_total,
        "errors": error_messages,
        "cancelled": cancelled,
        "silent": silent
    }

def _sync_run_async(coro, *args, **kwargs):
    """Synchronously runs an async function."""
    # This ensures a new loop is created and closed if needed,
    # preventing conflicts with Anki's main thread loop if called from background.
    try:
        return asyncio.run(coro(*args, **kwargs))
    except RuntimeError as e:
        if "cannot run current event loop" in str(e):
            # If asyncio.run fails because a loop is already running in this thread
            # (might happen depending on how taskman manages threads),
            # try running in the existing loop.
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(coro(*args, **kwargs))
        else:
            raise

def _sync_optimize_media_and_update_refs(media_files: List[Tuple[str, str]]) -> Tuple[Dict[str, str], List[Dict], Dict[str, str]]:
    """
    Synchronous wrapper to ONLY optimize media.
    Runs async optimization in a background thread (e.g., via QueryOp).
    Returns (filename_mapping, files_info, file_paths).
    Database updates are handled later on the main thread.
    """
    assert mw.col is not None, "Collection must be available for media optimization"
    if not media_files:
        logger.info("No media files provided for optimization.")
        return {}, [], {}

    logger.info(f"Starting media optimization for {len(media_files)} files in background task.")

    # 1. Run async optimization using the synchronous runner
    try:
        filename_mapping, files_info, file_paths = _sync_run_async(
            main.media_manager.optimize_media_for_upload, media_files
        )
        logger.info(f"Background media optimization finished. {len(filename_mapping)} files mapped.")
    except Exception as e:
        logger.error(f"Error during async media optimization: {str(e)}")
        logger.error(traceback.format_exc())
        # Propagate error to QueryOp
        raise RuntimeError(f"Media optimization failed: {e}") from e

    # 2. Return results - NO database update here
    return filename_mapping, files_info, file_paths


def _sync_handle_media_upload(token: str, deck_hash: str, files_info: List[Dict], file_paths: Dict[str, str], silent: bool) -> Dict[str, Any]:
    """
    Synchronous wrapper for handle_media_upload async function.
    Designed to be run in a background thread (e.g., via QueryOp).
    Handles progress updates by scheduling them on the main thread.
    Returns the result dictionary from handle_media_upload.
    """
    assert mw is not None, "Anki environment (mw) must be available"

    # Wrapper for progress callback to ensure UI updates run on main thread
    def progress_wrapper(p: float):
        # Schedule the UI update on the main thread
        mw.taskman.run_on_main(
            lambda: mw.progress.update(
                label=f"Uploading media files... {int(p * 100)}%",
                value=int(p * 100),
                max=100,
            ) if mw.progress else None # Check if progress exists
        )

    logger.info(f"Starting synchronous media upload wrapper for {len(files_info)} files.")
    try:
        # Run the async upload function using the synchronous runner
        result = _sync_run_async(
            handle_media_upload,
            user_token=token,
            deck_hash=deck_hash,
            all_files_info=files_info,
            file_paths=file_paths,
            progress_callback_wrapper=progress_wrapper, # Pass the safe wrapper
            silent=silent
        )
        logger.info("Synchronous media upload wrapper finished.")
        return result
    except Exception as e:
        logger.error(f"Error during synchronous media upload wrapper: {str(e)}")
        logger.error(traceback.format_exc())
        # Return an error structure consistent with handle_media_upload's return
        return {
            "uploaded": 0, "existing": 0, "failed": len(files_info),
            "errors": [f"Upload failed: {str(e)}"], "cancelled": False, "silent": silent
        }

def _submit_deck_op(deck: Deck, did: int, rationale: int, commit_text: str, media_files_info: List[Dict], media_file_paths: Dict[str, str]) -> Optional[Tuple[str, str, List[Dict], Dict[str, str], bool]]:
    assert mw.col is not None, "Collection must be available for deck submission"
    deckHash = get_deck_hash_from_did(did)
    if not deckHash:
         # This case should ideally be caught earlier, but handle defensively.
         raise ValueError("Could not determine deck hash for submission.")

    newName = get_local_deck_from_hash(deckHash)
    deckPath = mw.col.decks.name(did)

    token, force_overwrite = get_maintainer_data(deckHash)

    if media_files_info and not token:
        # We raise an exception to be caught by QueryOp's failure handler
        raise ValueError("Login required to upload media with suggestion.")

    if not token and force_overwrite:
        # Invalid state, token expired but auto-approve was likely set
        raise ValueError("Login expired or invalid. Please log in again via AnkiCollab menu.")

    # Adjust rationale/commit based on maintainer status
    effective_rationale = rationale
    effective_commit_text = commit_text
    if token and force_overwrite:
        effective_rationale = 10 # Other
        effective_commit_text = "" # Useless anyway
    elif rationale is None:
         # Should be caught by UI, but handle defensively
         raise ValueError("Submission rationale is missing.")

    # Prepare and send data
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    
    data = {
        "remote_deck": deckHash,
        "deck_path": deckPath,
        "new_name": newName,
        "deck": deck_res,
        "rationale": effective_rationale,
        "commit_text": effective_commit_text,
        "token": token,
        "force_overwrite": force_overwrite,
    }
    try:
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        headers = {"Content-Type": "application/json"}
        logger.info(f"Submitting deck data for hash: {deckHash}")
        response = requests.post(f"{API_BASE_URL}/submitCard", data=based_data, headers=headers)
        response.raise_for_status()

        logger.info(f"Deck submission response status: {response.status_code}")
        # Return data needed for media upload if successful and media exists
        if media_files_info:
            # Pass necessary info to the success callback for the next step
            return token, deckHash, media_files_info, media_file_paths, False # silent = False for suggestions
        else:
            # No media to upload, return None to signal completion
            # Also pass back the success message text
            return None # Explicitly return None if no media upload needed

    except requests.exceptions.HTTPError as e:
        logger.error(f"Network error during deck submission: {e}")
        # Check for specific server error messages if possible
        if e.response is None:
            raise RuntimeError("Network error: No response from server.") from e
        
        error_text = e.response.text
        status_code = e.response.status_code
        print(f"Error: {error_text}") # Debugging output
        
        if status_code == 500 and error_text:
            if "Notetype Error: " in error_text:
                missing_note_uuid = error_text.split("Notetype Error: ")[1]
                # Cannot easily get notetype name here without collection access issues
                # Raise a specific error message for the failure handler
                logger.error(f"Notetype not allowed by maintainer. UUID: {missing_note_uuid}")
                raise ValueError(f"Notetype Error: A notetype used in your suggestion does not exist on the cloud deck. Please only use notetypes added by the maintainer.")
            elif "Subdecks are not allowed" == error_text:
                logger.error("Subdecks are not allowed in suggestions.")
                raise ValueError("The maintainer does not allow new subdecks in suggestions. Please only suggest changes to existing decks.")
            elif "Deck does not exist" == error_text:
                logger.error(f"Deck not found on server")
                raise ValueError(f"Deck Error: The deck used in your suggestion does not exist on the cloud. Please only use decks added by the maintainer.")
            else:
                logger.error(f"Deck submission failed with unknown error: {error_text}")
                raise RuntimeError(f"Submission failed: {error_text}") from e
        else:
             raise RuntimeError(f"Unknown submission error!") from e
    except Exception as e:
        logger.error(f"Unexpected error during deck submission: {e}")
        logger.error(traceback.format_exc())
        raise RuntimeError(f"An unexpected error occurred during submission: {e}") from e


def _handle_media_upload_result(result: Dict[str, Any]):
    """Handles the result dictionary from media upload (called in QueryOp success)."""
    # This runs on the main thread.
    mw.progress.finish()

    uploaded = result.get("uploaded", 0)
    existing = result.get("existing", 0)
    failed = result.get("failed", 0)
    errors = result.get("errors", [])
    cancelled = result.get("cancelled", False)
    silent = result.get("silent", False)

    parent_widget = QApplication.focusWidget() or mw

    if cancelled:
        msg = (f"Media upload cancelled:\n"
               f"• {uploaded} files uploaded\n"
               f"• {existing} files already existed\n"
               f"• {failed} files failed before cancellation")
        aqt.utils.showWarning(msg, title="Upload Cancelled", parent=parent_widget)
    elif failed > 0:
        msg = (f"Media upload completed with issues:\n"
               f"• {uploaded} files uploaded successfully\n"
               f"• {existing} files already existed\n"
               f"• {failed} files failed\n\n")
        if errors:
            msg += "Recent errors:\n" + "\n".join(errors[-3:])
            if len(errors) > 3:
                msg += f"\n...and {len(errors) - 3} more errors"
        aqt.utils.showWarning(msg, title="Media Upload Summary", parent=parent_widget)
    elif uploaded > 0 and not silent:
        msg = (f"Media upload: {uploaded} files uploaded"
               )
        aqt.utils.tooltip(msg, parent=parent_widget)
    elif not silent:
         # Case where there were files to upload, but all failed or were cancelled early
         # or maybe no files were actually found after optimization.
         aqt.utils.tooltip("Media upload finished. No new files were uploaded.", parent=parent_widget)
         pass # Or show a specific message if needed

    # Common post-upload actions (like rating request for suggestions)
    if not silent: # Only ask for rating after suggestions, not initial export
        mw.reset() # Reset UI state
        ask_for_rating()


def _start_media_upload(media_upload_data: Optional[Tuple[str, str, List[Dict], Dict[str, str], bool]], success_callback: Callable[[Dict[str, Any]], None]):
    """
    Starts the media upload process using QueryOp.
    Called from the success callback of the deck submission/creation Op.
    """
    if media_upload_data is None:
        # This means deck submission/creation was successful, but no media needed uploading.
        logger.info("Deck submission/creation successful, no media to upload.")
        mw.progress.finish() # Finish any previous progress
        # Call the final success callback directly with an empty result
        success_callback({"uploaded": 0, "existing": 0, "failed": 0, "errors": [], "cancelled": False, "silent": True})
        return

    token, deckHash, media_files_info, media_file_paths, silent = media_upload_data
    parent_widget = QApplication.focusWidget() or mw # type: ignore

    logger.info(f"Starting media upload QueryOp for deck {deckHash}. Silent: {silent}")

    op = QueryOp(
        parent=parent_widget,
        # Run the synchronous wrapper in the background op
        op=lambda col: _sync_handle_media_upload(token, deckHash, media_files_info, media_file_paths, silent),
        success=success_callback # Use the provided final success handler
    )
    # Configure QueryOp
    if point_version() >= 231000:
        op.without_collection() # Media upload doesn't need collection access itself

    # Use progress reporting that calls our main-thread safe wrapper inside _sync_handle_media_upload
    op.with_progress("Uploading media files...") # Basic progress bar
    op.run_in_background()

def suggest_notes(nids: Sequence[NoteId], rationale_id: int, editor: Optional[Any] = None):
    """Suggest changes for specific notes."""
    assert mw is not None and mw.col is not None, "Anki environment not ready"
    parent_widget = QApplication.focusWidget() or mw

    if not nids:
        aqt.utils.showWarning("No notes selected for suggestion.", parent=parent_widget)
        return

    try:
        notes = [mw.col.get_note(nid) for nid in nids]
        if not notes or any(n is None for n in notes):
             raise ValueError("One or more selected notes could not be found.")

        first_note_card = notes[0].cards()[0]
        deckHash = get_deck_hash_from_did(first_note_card.did)

        if deckHash is None:
            aqt.utils.showInfo("Cannot find the Cloud Deck for these notes. Ensure the parent deck is subscribed.", parent=parent_widget)
            return

        # Verify all notes belong to the same cloud deck
        for note in notes[1:]:
            if get_deck_hash_from_did(note.cards()[0].did) != deckHash:
                aqt.utils.showInfo("Please only select cards from the same cloud deck subscription.", parent=parent_widget)
                return

        did = get_did_from_hash(deckHash)
        if did is None:
            # This case implies a config mismatch, should be rare
            aqt.utils.showInfo("Cannot find the local Anki deck associated with this cloud deck.", parent=parent_widget)
            return

        deck_obj = mw.col.decks.get(did, default=False)
        if not deck_obj or deck_obj.get('dyn', False):
            aqt.utils.showInfo("Filtered decks are not supported for suggestions.", parent=parent_widget)
            return
        
        # --- Preparation (Main Thread) ---
        disambiguate_note_model_uuids(mw.col)
        deck_repr = deck_initializer.from_collection(mw.col, mw.col.decks.name(did), note_ids=nids)
        deck_initializer.trim_empty_children(deck_repr)
        note_sorter = NoteSorter(ConfigSettings.get_instance())
        note_sorter.sort_deck(deck_repr)

        personal_tags = get_personal_tags(deckHash)
        if personal_tags:
            deck_initializer.remove_tags_from_notes(deck_repr, personal_tags)

        # --- Get Commit Info (Main Thread - UI Interaction) ---
        commit_text = ""
        final_rationale_id = rationale_id
        token, force_overwrite = get_maintainer_data(deckHash) # Check login status early

        if not token:
            aqt.utils.showWarning("You must be logged in to make this suggestion. Please login under AnkiCollab > Login in the menu bar and try again.", parent=parent_widget)
            return

        if rationale_id != 6 and not force_overwrite: # Skip dialog for 'New Card' unless maintainer
            result = get_commit_info(rationale_id)
            if result is None or result[0] is None:
                aqt.utils.tooltip("Suggestion cancelled.", parent=parent_widget)
                return
            final_rationale_id, commit_text = result
        elif force_overwrite:
             final_rationale_id = 10 # Force 'Other' for maintainer overwrite
             commit_text = ""
            
        # --- Media Preparation (Main Thread) ---
        protected_fields = deck_repr.get_protected_fields(deckHash)
        media_files = deck_repr.get_media_file_note_map(protected_fields)

        # --- Start Background Operations ---
        logger.info("Starting suggestion process...")

        # Step 1: Optimize Media (Background Op)
        op_optimize = QueryOp(
            parent=parent_widget,
            op=lambda col: _sync_optimize_media_and_update_refs(media_files),
            success=lambda result: _on_suggest_media_optimized(result, deck_repr, media_files, did, final_rationale_id, commit_text, editor)
        )
        silent_on_new_cards = final_rationale_id == 6 # New Card rationale should be silent
        if not silent_on_new_cards:
            op_optimize.with_progress("Optimizing media files...")
        op_optimize.run_in_background()

    except Exception as e:
        logger.error(f"Error preparing suggestion: {e}")
        logger.error(traceback.format_exc())
        show_exception(parent=parent_widget, exception=e)

def _on_suggest_media_optimized(
    opt_result: Tuple[Dict[str, str], List[Dict], Dict[str, str]], # Result from _sync_optimize...
    deck_repr: Deck,
    media_files: List[Tuple[str, str]],
    did: int,
    rationale_id: int,
    commit_text: str,
    editor: Optional[Any]
):
    """Success callback after media optimization for suggestions."""
    # Runs on Main Thread
    parent_widget = QApplication.focusWidget() or mw
    filename_mapping, files_info, file_paths = opt_result # Unpack result

    # Create a backup before updating the fields in the collection?
    #create_backup()

    if filename_mapping:
        logger.info(f"Updating media references in {len(filename_mapping)} notes on main thread...")
        try:
            updated_count, op_changes = update_media_references(filename_mapping, media_files)
            logger.info(f"Successfully updated media references in {updated_count} notes.")
            if updated_count > 0:
                if editor and op_changes.note_text:
                    editor_note = editor.note
                    try:
                        assert editor_note is not None
                        editor_note.load()
                        editor.set_note(editor_note)
                        editor.loadNote()
                    except NotFoundError:
                        logger.warning("Note being edited was deleted during suggestion process.")
                        editor.cleanup()
                    except Exception as e:
                        logger.error(f"Error reloading editor note: {e}")
        except Exception as e:
            logger.error(f"Failed to update media references on main thread: {e}", exc_info=True)
            show_exception(parent=parent_widget, exception=e)
            # Abort the rest of the process if references couldn't be updated
            mw.progress.finish() # Ensure any progress bar is closed
            return # Stop here
    else:
        logger.info("No media references needed updating.")

    logger.info(f"Refreshing deck representation notes from collection...")
    try:
        deck_repr.refresh_notes(media_files)
    except Exception as e:
        logger.error(f"Failed to refresh deck_repr notes: {e}", exc_info=True)
        # Logged the error, but proceed with potentially stale data in deck_repr?
        # Or abort here too? Aborting might be safer.
        aqt.utils.showWarning(f"Failed to refresh note data before submission: {e}\n\nAborting suggestion.", parent=parent_widget)
        mw.progress.finish()
        return # Stop here

    # --- Step 2: Submit Deck (Background Op) ---
    logger.info("Starting deck submission QueryOp.")
    op_submit = QueryOp(
        parent=parent_widget,
        # Pass the potentially updated deck_repr
        op=lambda col: _submit_deck_op(deck_repr, did, rationale_id, commit_text, files_info, file_paths),
        success=lambda result: _on_suggest_deck_submitted(result, editor)
    )
    
    silent_on_new_cards = rationale_id == 6 # New Card rationale should be silent
    if not silent_on_new_cards:
        op_submit.with_progress("Submitting suggestion to AnkiCollab...")
    op_submit.run_in_background()


def _on_suggest_deck_submitted(submit_result: Optional[Tuple[str, str, List[Dict], Dict[str, str], bool]], editor: Optional[Any]):
    """Success callback after deck submission for suggestions."""
    # Runs on Main Thread
    logger.info("Deck submission complete. Starting media upload if needed.")

    # Step 3: Upload Media (Background Op, started by _start_media_upload)
    # The final success callback handles UI updates and rating request
    _start_media_upload(submit_result, success_callback=_on_suggest_media_uploaded)
    

def _on_suggest_media_uploaded(upload_result: Dict[str, Any]):
    """Final success callback after media upload for suggestions."""
    # Runs on Main Thread
    _handle_media_upload_result(upload_result)
    # ask_for_rating() is called inside _handle_media_upload_result if not silent


def suggest_subdeck(did: int):
    """Suggest an entire subdeck."""
    assert mw is not None and mw.col is not None, "Anki environment not ready"
    parent_widget = QApplication.focusWidget() or mw

    try:
        deck_obj = mw.col.decks.get(did, default=False)
        if not deck_obj or deck_obj.get('dyn', False):
            aqt.utils.showInfo("Filtered decks are not supported for suggestions.", parent=parent_widget)
            return

        deck_name = mw.col.decks.name(did)
        deckHash = get_deck_hash_from_did(did)
        if deckHash is None:
            aqt.utils.showWarning("Config Error: Could not find the cloud deck hash for this local deck. Please check the Subscriptions window.", parent=parent_widget)
            return
        
        # --- Preparation (Main Thread) ---
        disambiguate_note_model_uuids(mw.col)
        deck_repr = deck_initializer.from_collection(mw.col, deck_name) # Export whole deck. laggy on large decks, maybe we can move this to a background thread?

        #  could also be moved to an initial QueryOp
        try:
            response = requests.get(f"{API_BASE_URL}/GetDeckTimestamp/" + deckHash)
            response.raise_for_status()
            last_updated = float(response.text)
            last_pulled = get_timestamp(deckHash) or 0.0
            deck_initializer.remove_unchanged_notes(deck_repr, last_updated, last_pulled)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Could not get deck timestamp: {e}. Proceeding with full deck suggestion.")
        except Exception as e:
            logger.error(f"Error processing deck timestamps: {e}")
            aqt.utils.showWarning(f"Failed to process deck!", parent=parent_widget)
            return

        deck_initializer.trim_empty_children(deck_repr)
        personal_tags = get_personal_tags(deckHash)
        if personal_tags:
            deck_initializer.remove_tags_from_notes(deck_repr, personal_tags)

        # Fix name to be relative
        deck_repr.anki_dict["name"] = deck_name.split("::")[-1]

        # --- Get Commit Info (Main Thread - UI Interaction) ---
        
        token, force_overwrite = get_maintainer_data(deckHash) # Check login status early
        if not token:
            aqt.utils.showWarning("You must be logged in to make this suggestion. Please login under AnkiCollab > Login in the menu bar and try again.", parent=parent_widget)
            return

        if not force_overwrite:
            result = get_commit_info(9) # Default to Bulk Suggestion
            if result is None or result[0] is None:
                aqt.utils.tooltip("Suggestion cancelled.", parent=parent_widget)
                return
            rationale_id, commit_text = result
        else:
            rationale_id = 10
            commit_text = ""
        # --- Media Preparation (Main Thread) ---
        protected_fields = deck_repr.get_protected_fields(deckHash)
        media_files = deck_repr.get_media_file_note_map(protected_fields)

        # --- Start Background Operations ---
        logger.info("Starting subdeck suggestion process...")

        # Step 1: Optimize Media (Background Op)
        op_optimize = QueryOp(
            parent=parent_widget,
            op=lambda col: _sync_optimize_media_and_update_refs(media_files),
            success=lambda result: _on_suggest_media_optimized(result, deck_repr, media_files, did, rationale_id, commit_text, None) # No editor for subdeck
        )
        op_optimize.with_progress("Optimizing media files...")
        op_optimize.run_in_background()

    except Exception as e:
        logger.error(f"Error preparing subdeck suggestion: {e}")
        logger.error(traceback.format_exc())
        show_exception(parent=parent_widget, exception=e)


def handle_export(did: int, username: str):
    """Handles exporting a new deck to AnkiCollab."""
    assert mw is not None and mw.col is not None, "Anki environment not ready"
    parent_widget = QApplication.focusWidget() or mw

    try:
        deck_obj = mw.col.decks.get(did, default=False)
        if not deck_obj or deck_obj.get('dyn', False):
            aqt.utils.showInfo("Filtered decks cannot be published.", parent=parent_widget)
            return

        # --- Preparation & Checks (Main Thread) ---
        user_token, _ = get_maintainer_data("") # Check login status
        if not user_token:
            aqt.utils.showWarning("You must be logged in to publish a new deck. Please login under AnkiCollab > Login.", parent=parent_widget)
            return

        # Create bakcup before export
        create_backup(background=True)
        
        deck_name = mw.col.decks.name(did)
        disambiguate_note_model_uuids(mw.col)
        deck_repr = deck_initializer.from_collection(mw.col, deck_name)
        deck_initializer.trim_empty_children(deck_repr)
        note_sorter = NoteSorter(ConfigSettings.get_instance())
        note_sorter.sort_deck(deck_repr)

        # Remove standard protected tags for initial export
        deck_initializer.remove_tags_from_notes(deck_repr, DEFAULT_PROTECTED_TAGS + [PREFIX_PROTECTED_FIELDS])

        # --- Media Preparation (Main Thread) ---
        protected_fields = deck_repr.get_protected_fields(None) # No hash yet
        media_files = deck_repr.get_media_file_note_map(protected_fields)

        # --- Start Background Operations ---
        logger.info("Starting new deck export process...")

        # Step 1: Optimize Media (Background Op)
        op_optimize = QueryOp(
            parent=parent_widget,
            op=lambda col: _sync_optimize_media_and_update_refs(media_files),
            success=lambda result: _on_export_media_optimized(result, deck_repr, did, media_files, username, user_token)
        )
        op_optimize.with_progress("Optimizing media files...")
        op_optimize.run_in_background()

    except Exception as e:
        logger.error(f"Error preparing deck export: {e}")
        logger.error(traceback.format_exc())
        show_exception(parent=parent_widget, exception=e)



def _on_export_media_optimized(opt_result: Tuple[List[Dict], Dict[str, str]], deck_repr: Deck, did: int, media_files: list, username: str, user_token: str):
    """Success callback after media optimization for export."""
    # Runs on Main Thread
    parent_widget = QApplication.focusWidget() or mw
    filename_mapping, files_info, file_paths = opt_result

    # Create a backup before updating the fields in the collection?
    #create_backup()

    if filename_mapping:
        logger.info(f"Updating media references in {len(filename_mapping)} notes on main thread...")
        try:
            updated_count, _ = update_media_references(filename_mapping, media_files)
            logger.info(f"Successfully updated media references in {updated_count} notes.")
        except Exception as e:
            logger.error(f"Failed to update media references on main thread: {e}", exc_info=True)
            show_exception(parent=parent_widget, exception=e)
            mw.progress.finish() # Ensure any progress bar is closed
            return # Stop here
    else:
        logger.info("No media references needed updating.")
        
    deck_repr.refresh_notes(media_files)

    logger.info("Media optimization complete. Starting deck creation QueryOp.")

    op_create = QueryOp(
        parent=parent_widget,
        op=lambda col: _create_deck_op(deck_repr, username),
        success=lambda result: _on_export_deck_created(result, did, user_token, files_info, file_paths) # Pass token/media info
    )
    op_create.with_progress("Publishing deck to AnkiCollab...")
    op_create.run_in_background()


def _create_deck_op(deck_repr: Deck, username: str) -> Dict[str, Any]:
    """Operation function for QueryOp: Creates the deck via API."""
    # Runs in background thread
    deck_res = json.dumps(deck_repr, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    data = {"deck": deck_res, "username": username}
    try:
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        headers = {"Content-Type": "application/json"}
        logger.info("Sending create deck request...")
        response = requests.post(f"{API_BASE_URL}/createDeck", data=based_data, headers=headers)
        response.raise_for_status() # Check for HTTP errors

        logger.info(f"Create deck response status: {response.status_code}")
        return response.json() # Return parsed JSON response

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error during deck creation: {e}")
        status_code = e.response.status_code if e.response else 500
        error_text = e.response.text if e.response else str(e)
        # Re-raise a more informative error if possible
        if status_code == 413 or "Payload Too Large" in error_text:
            raise RuntimeError("Deck export failed: Deck is too large. Please reach out via Discord.") from e
        else:
            raise RuntimeError(f"Deck creation failed: {error_text} (Status: {status_code})") from e
    except Exception as e:
        logger.error(f"Unexpected error during deck creation: {e}")
        logger.error(traceback.format_exc())
        raise RuntimeError(f"An unexpected error occurred during deck creation: {e}") from e


def _on_export_deck_created(api_result: Dict[str, Any], did: int, user_token: str, files_info: List[Dict], file_paths: Dict[str, str]):
    """Success callback after deck creation API call."""
    # Runs on Main Thread
    parent_widget = QApplication.focusWidget() or mw
    mw.progress.finish() # Finish "Publishing..." progress

    status = api_result.get("status")
    message = api_result.get("message", "Unknown response from server.")

    if status == 1:
        deckHash = message
        logger.info(f"Deck successfully created with hash: {deckHash}")

        # Store it in the config
        if deckHash:
                strings_data = mw.addonManager.getConfig(__name__)
                if strings_data is None: strings_data = {} # Initialize if None

                strings_data[deckHash] = {
                    'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                    'deckId': did,
                    'optional_tags': {},
                    'personal_tags': DEFAULT_PROTECTED_TAGS,
                }
                mw.addonManager.writeConfig(__name__, strings_data)
                
        if files_info and aqt.utils.askUser(
            "Deck published successfully!\n\nDo you want to upload associated media files now? "
            "(New media added later via suggestions will be uploaded automatically).",
            parent=parent_widget, title="Upload Media?"
        ):
            logger.info("User opted to upload media for new deck.")
            
            media_upload_data = (user_token, deckHash, files_info, file_paths, True) # silent = True for initial export
            _start_media_upload(media_upload_data, success_callback=_on_export_media_uploaded)
        else:
            # No media upload requested or no media found
            aqt.utils.showInfo(f"Deck published successfully!\nCloud Deck Hash: {deckHash}", parent=parent_widget, title="Publish Successful")
            # Call final callback immediately if needed, indicating no media upload
            _on_export_media_uploaded({"uploaded": 0, "existing": 0, "failed": 0, "errors": [], "cancelled": False, "silent": True})

    elif status == 0:
        logger.warning(f"Deck creation reported status 0: {message}")
        aqt.utils.showWarning(f"Deck publication failed: {message}", parent=parent_widget, title="Publish Failed")
    else:
        logger.error(f"Unexpected status from createDeck API: {status} - {message}")
        aqt.utils.showCritical(f"Unexpected server response during deck publication: {message}", parent=parent_widget, title="Publish Error")


def _on_export_media_uploaded(upload_result: Dict[str, Any]):
    """Final success callback after media upload for export."""
    # Runs on Main Thread
    _handle_media_upload_result(upload_result)
    # No rating request after initial export

def get_commit_info(default_opt = 0):
    options = [
        "None", "Deck Creation", "Updated content", "New content", "Content error",
        "Spelling/Grammar", "New card", "Updated Tags",
        "New Tags", "Bulk Suggestion", "Other", "Note Removal", "Changed Deck"
    ]

    dialog = QDialog(QApplication.focusWidget() or mw)
    dialog.setWindowTitle("Commit Information")
    layout = QVBoxLayout()

    listWidget = QListWidget()
    listWidget.addItems(options)
    listWidget.setCurrentRow(default_opt)
    listWidget.doubleClicked.connect(dialog.accept)
    layout.addWidget(QLabel("Select a rationale (mandatory):"))
    layout.addWidget(listWidget)

    textEdit = QTextEdit()
    textEdit.setFixedHeight(5 * textEdit.fontMetrics().lineSpacing())
    textEdit.setPlaceholderText("Enter additional information (optional, max 255 characters)")

    def checkLength():
        text = textEdit.toPlainText()
        if len(text) > 255:
            cursor = textEdit.textCursor()
            pos = cursor.position()
            textEdit.setPlainText(text[:255])
            cursor.setPosition(pos)
            textEdit.setTextCursor(cursor)

    textEdit.textChanged.connect(checkLength)
    layout.addWidget(QLabel("Additional Information: (optional)"))
    layout.addWidget(textEdit)

    shortcut = QShortcut(QKeySequence("Ctrl+Return"), textEdit)
    shortcut.activated.connect(dialog.accept)

    buttonLayout = QHBoxLayout()
    cancelButton = QPushButton("Cancel")
    okButton = QPushButton("Submit")
    buttonLayout.addWidget(cancelButton)
    buttonLayout.addWidget(okButton)
    layout.addLayout(buttonLayout)

    dialog.setLayout(layout)
    okButton.clicked.connect(dialog.accept)
    cancelButton.clicked.connect(dialog.reject)
    # textEdit.setReadOnly(True) # Let user type immediately
    # textEdit.mousePressEvent = lambda _: textEdit.setReadOnly(False) # Not needed if not read-only initially
    listWidget.setFocus() # Focus the list first

    if dialog.exec() == QDialog.DialogCode.Accepted:
        selected_item = listWidget.currentItem()
        if selected_item:
             rationale = listWidget.row(selected_item) # Get index
             additional_info = textEdit.toPlainText().strip()
             # Ensure 'None' isn't selected if it's mandatory
             if rationale == 0:
                  aqt.utils.showWarning("Please select a valid rationale.", parent=dialog)
                  return get_commit_info(default_opt) # Re-show dialog
             return rationale, additional_info
        else:
             # Should not happen if an item is selected by default
             aqt.utils.tooltip("No rationale selected. Aborting.", parent=dialog)
             return None, None

    aqt.utils.tooltip("Aborting suggestion.", parent=QApplication.focusWidget() or mw)
    return None, None
