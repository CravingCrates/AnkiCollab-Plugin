import asyncio
import json
import os
import re
import traceback
import requests


import aqt
import aqt.utils
import anki
from anki.utils import point_version

from aqt.qt import *
from aqt import mw
import aqt.utils
from aqt.operations import QueryOp
from anki.utils import ids2str, join_fields, split_fields
from anki.errors import NotFoundError
from datetime import datetime, timedelta, timezone
import base64
import gzip
import logging

from typing import cast

from .crowd_anki.representation.note_model import NoteModel

from .crowd_anki.utils.uuid import UuidFetcher

from .var_defs import DEFAULT_PROTECTED_TAGS, PREFIX_PROTECTED_FIELDS

from .dialogs import RateAddonDialog

from .thread import run_function_in_thread, run_async_function_in_thread, sync_run_async


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

from .utils import get_deck_hash_from_did, get_local_deck_from_hash, get_timestamp, get_did_from_hash
from . import main
logger = logging.getLogger("ankicollab")

IMG_NAME_IN_IMG_TAG_REGEX = re.compile(
    r"<img.*?src=[\"'](?!http://|https://)(.+?)[\"']"
)

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
                        dialog = RateAddonDialog()
                        dialog.exec()
            mw.addonManager.writeConfig(__name__, strings_data)
    
def upload_media_pre(result):
    if result is None:
        mw.progress.finish()
        return
    (token, deckHash, media_files_info, media_file_paths, silent) = result

    op = QueryOp(
        parent=QApplication.focusWidget(),
        op=lambda _: upload_media_with_progress(token, deckHash, media_files_info, media_file_paths, silent),
        success=upload_media_post,
    )
    if point_version() >= 231000:
        op.without_collection()
        
    op.run_in_background()

def submit_with_progress(deck, did, rationale, commit_text):    
    
    # Get media files before export
    media_files = []
    protected_fields = deck.get_protected_fields(get_deck_hash_from_did(did))
    media_files = deck.get_media_file_note_map(protected_fields)
        
    files_info, file_paths = sync_run_async(optimize_media_files, media_files)
    # We need to recollect the notes after media optimization
    deck.refresh_notes(media_files)
    
    op = QueryOp(
        parent=QApplication.focusWidget(),
        op=lambda _: submit_deck(deck, did, rationale, commit_text, files_info, file_paths),
        success=upload_media_pre,
    )
    if point_version() >= 231000:
        op.without_collection()
    op.with_progress("Uploading to AnkiCollab...").run_in_background()

def get_maintainer_data(deckHash):
    token = auth_manager.get_token()
    auto_approve = auth_manager.get_auto_approve()
    
    # If we have a token, verify it's still valid
    if token:
        token_info = {
            'token': token,
            'deck_hash': deckHash,
        }
        
        try:
            token_check_response = requests.post(
                f"{API_BASE_URL}/CheckUserToken", 
                json=token_info, 
                headers={"Content-Type": "application/json"}
            )
            
            if token_check_response.status_code == 200:
                token_res = token_check_response.text
                if token_res != "true":  # Invalid token
                    # Try to refresh token
                    if auth_manager.refresh_token():
                        # If refresh succeeded, update token and try again
                        token = auth_manager.get_token()
                        token_info['token'] = token
                        
                        # Check again with new token
                        token_check_response = requests.post(
                            f"{API_BASE_URL}/CheckUserToken", 
                            json=token_info, 
                            headers={"Content-Type": "application/json"}
                        )
                        
                        if token_check_response.status_code != 200 or token_check_response.text != "true":
                            # If still invalid, force logout
                            from .menu import force_logout  # bypass circular import
                            mw.taskman.run_on_main(force_logout)
                            token = ""
                    else:
                        # If refresh failed, force logout
                        from .menu import force_logout  # bypass circular import
                        mw.taskman.run_on_main(force_logout)
                        token = ""
        except Exception as e:
            print(f"Error checking token: {e}")
            # Network error, return current token but don't force logout
    
    return token, auto_approve


def get_personal_tags(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    combined_tags = set()

    if strings_data:
        for hash, details in strings_data.items():
            if hash == deck_hash:
                personal_tags = details.get("personal_tags", DEFAULT_PROTECTED_TAGS)
                if "personal_tags" not in details:
                    details["personal_tags"] = personal_tags
                    mw.addonManager.writeConfig(__name__, strings_data)                
                combined_tags.update(personal_tags)
                combined_tags.add(PREFIX_PROTECTED_FIELDS)
                
                return list(combined_tags)
    return []

def get_note_id_from_guid(guid):
    """Get note ID from GUID"""
    try:
        note_id = mw.col.db.first("select id from notes where guid = ?", guid)
        if note_id:
            return note_id[0]
    except Exception as e:
        logger.error(f"Error getting note ID from GUID {guid}: {str(e)}")
    return None

def get_note_guid_from_id(note_id):
    """Get GUID from note ID"""
    try:
        guid = mw.col.db.first("select guid from notes where id = ?", note_id)
        if guid:
            return guid[0]
    except Exception as e:
        logger.error(f"Error getting GUID from note ID {note_id}: {str(e)}")
    return None

def update_media_references(filename_mapping, file_note_pairs):
    """
    Update note references to point to the new optimized media filenames
    
    Args:
        filename_mapping: Dict mapping original filenames to new optimized filenames
        file_note_pairs: List of (filename, note_guid) pairs used for upload
    """
    if not filename_mapping:
        return 0  # No updates needed
        
    # Create a mapping from old filename to affected note guids
    notes_by_filename = {}
    for filename, note_guid in file_note_pairs:
        if filename in filename_mapping:
            if filename not in notes_by_filename:
                notes_by_filename[filename] = []
            notes_by_filename[filename].append(note_guid)
      
    updated_notes = []
    # Process each filename that was optimized
    for old_filename, new_filename in filename_mapping.items():
        if old_filename not in notes_by_filename:
            continue
            
        # Get all notes that reference this file
        note_guids = notes_by_filename[old_filename]       
        for note_guid in note_guids:
            try:
                note_id = get_note_id_from_guid(note_guid)
                if not note_id:
                    continue
                    
                note = mw.col.get_note(note_id)
                if not note:
                    continue
                
                modified = False
                for i, field_content in enumerate(note.fields):
                    # Replace the old filename with the new one in the note fields
                    new_content = IMG_NAME_IN_IMG_TAG_REGEX.sub(
                        lambda m: m.group(0).replace(old_filename, new_filename),
                        field_content,
                    )
                    if new_content != field_content:
                        note.fields[i] = new_content
                        modified = True
                if modified:
                    updated_notes.append(note)
                                        
            except Exception as e:
                logger.error(f"Error updating references for note {note_guid}: {str(e)}")
    
    if updated_notes:
        mw.col.update_notes(updated_notes)
    
    return len(updated_notes)

# so this is a little hacky, but its not necessarily called from the main thread, but the reference updating should be handled from it, so we do that part on the main thread and wait for it to finish before proceeding
async def optimize_media_files(media_files: list):    
    filename_mapping, files_info, file_paths = await main.media_manager.optimize_media_for_upload(media_files)

    # Create a Future to wait for the main thread operation to complete
    from concurrent.futures import Future
    future = Future()
    
    def update_and_complete():
        try:
            result = update_media_references(filename_mapping, media_files)
            future.set_result(result)
        except Exception as e:
            logger.error(f"Error updating media references: {str(e)}")
            future.set_exception(e)
    
    mw.taskman.run_on_main(update_and_complete)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, future.result)
        print(f"Updated {result} notes with new media references")
    except Exception as e:
        logger.error(f"Failed while waiting for media reference update: {str(e)}")
    
    return files_info, file_paths
    
async def handle_media_upload(user_token: str, deck_hash: str, all_files_info, file_paths, cb=None, silent=False) -> None:
    """Upload media files for exported deck in batches"""
    if not all_files_info:
        return

    total_files = len(all_files_info)
    batch_size = 100
    uploaded_total = 0
    skipped_total = 0
    failed_total = 0
    error_messages = []
    
    try:
        batches = []
        for batch_start in range(0, total_files, batch_size):
            batch_end = min(batch_start + batch_size, total_files)
            batches.append((batch_start, batch_end))
        
        total_batches = len(batches)
        
        for batch_index, (batch_start, batch_end) in enumerate(batches):
            current_batch = all_files_info[batch_start:batch_end]
            
            batch_progress_start = batch_index / total_batches
            batch_progress_end = (batch_index + 1) / total_batches
            
            # Create a progress wrapper that scales the progress to this batch's range
            def batch_progress_wrapper(p):
                if cb:
                    # Scale p (0-1) to fit within this batch's progress range
                    scaled_progress = batch_progress_start + (p * (batch_progress_end - batch_progress_start))
                    cb(scaled_progress)
            
            # Process this bitch
            batch_result = await main.media_manager.upload_media_bulk(
                user_token=user_token,
                files_info=current_batch,
                file_paths=file_paths,
                deck_hash=deck_hash,
                progress_callback=batch_progress_wrapper
            )
            
            if mw.progress.want_cancel():
                if not silent:
                    msg = (f"Media upload cancelled:\n"
                           f"• {uploaded_total} files uploaded\n"
                           f"• {skipped_total} files already existed\n"
                           f"• {failed_total} files failed")
                    aqt.mw.taskman.run_on_main(
                        lambda: aqt.utils.showWarning(msg, title="Upload Cancelled", parent=QApplication.focusWidget())
                    )
                return
            
            # Update totals
            if batch_result["success"]:
                uploaded_total += batch_result.get("uploaded", 0)
                skipped_total += batch_result.get("existing", 0)
                failed_total += batch_result.get("failed", 0)
                                
                if "error" in batch_result:
                    error_messages.append(batch_result["error"])
                    
                if batch_end < total_files:
                    progress_msg = f"Media upload progress: {uploaded_total} uploaded, {skipped_total} existing, {failed_total} failed"
                    logger.info(progress_msg)
            else:
                # Handle batch failure
                error_msg = batch_result.get("message", "Unknown error")
                error_messages.append(f"Batch {batch_index + 1}/{total_batches}: {str(error_msg)}")
                failed_total += len(current_batch)
                logger.error(f"Batch upload error: {error_msg}")

        if cb:
            cb(0.95)  # pmuch done at this point
                   
        # Show final summary
        if failed_total > 0:
            # Show detailed error report
            msg = (f"Media upload completed with issues:\n"
                    f"• {uploaded_total} files uploaded successfully\n"
                    f"• {skipped_total} files already existed\n"
                    f"• {failed_total} files failed\n\n")
            
            if error_messages:
                msg += "Recent errors:\n" + "\n".join(error_messages[-3:])
                if len(error_messages) > 3:
                    msg += f"\n...and {len(error_messages) - 3} more errors"
            
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.showWarning(msg, title="Media Upload Summary", parent=QApplication.focusWidget())
            )
        elif (uploaded_total > 0 or skipped_total > 0) and not silent:
            msg = (f"Media upload complete:\n"
                    f" {uploaded_total} files uploaded\n"
                    f"| {skipped_total} files already existed")
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(msg, parent=QApplication.focusWidget())
            )
            
    except Exception as e:
        logger.error(f"Error during media upload: {str(e)}")
        logger.error(traceback.format_exc())        
        message = str(e)
        aqt.mw.taskman.run_on_main(
            lambda: aqt.utils.showWarning(
                f"Media upload error: {message}\n\n"
                f"Partial results:\n"
                f"• {uploaded_total} files uploaded\n"
                f"• {skipped_total} files already existed\n"
                f"• {failed_total} files failed",
                title="Upload Error",
                parent=mw
            )
        )
        
# Both are the success functions, once for a new deck upload, once for suggestions
def on_media_pload_upload_done(data) -> None:
    mw.progress.finish()
    aqt.utils.showInfo("Deck published. Thanks for sharing!")

def upload_media_post(result):
    mw.progress.finish()
    mw.reset()
    ask_for_rating()
    
def media_progress_cb(p):
    aqt.mw.taskman.run_on_main(
        lambda: aqt.mw.progress.update(
            label=f"Uploading media files... {int(p * 100)}%",
            value=int(p * 100),
            max=100,
        )
    )
           
def upload_media_with_progress(token: str, deckHash: str, files_info, file_paths, silent=False):
    try:
        # Run synchronously with exception handling
        sync_run_async(handle_media_upload, token, deckHash, files_info, file_paths, cb=media_progress_cb, silent=silent)
    except Exception as e:
        print(f"Error setting up media upload: {str(e)}")
        print(traceback.format_exc())
        error_msg = str(e)
        aqt.mw.taskman.run_on_main(
            lambda: aqt.utils.showWarning(f"Failed to start media upload: {error_msg}", parent=QApplication.focusWidget())
        )
                      
def submit_deck(deck, did, rationale, commit_text, media_files_info, media_file_paths):
        
    deckHash = get_deck_hash_from_did(did)
    newName = get_local_deck_from_hash(deckHash)
    deckPath =  mw.col.decks.name(did)
    token, force_overwrite = get_maintainer_data(deckHash)
    
    if media_files_info and token == "":
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.showWarning("You must be logged in to make this suggestion. Please login under AnkiCollab > Login in the menu bar. And try again", parent=QApplication.focusWidget()))
        return
    
    if token == "" and force_overwrite:
        commit_text = ""
        force_overwrite = False
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.showWarning("Your AnkiCollab Login expired or is invalid. Please renew your Login under AnkiCollab > Login in the menu bar.", parent=QApplication.focusWidget()))
        return
    
    if token and force_overwrite:
        rationale = 10 #rationale = Other
        commit_text = "" # useless anyway
    else:
        if rationale is None:
            return
            
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    
    data = {
        "remote_deck": deckHash, 
        "deck_path": deckPath, 
        "new_name": newName, 
        "deck": deck_res, 
        "rationale": rationale,
        "commit_text": commit_text,
        "token": token,
        "force_overwrite": force_overwrite,
        }
    compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
    based_data = base64.b64encode(compressed_data)
    headers = {"Content-Type": "application/json"}
    response = requests.post(f"{API_BASE_URL}/submitCard", data=based_data, headers=headers)
                
    if response.status_code == 200:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip(f"AnkiCollab Upload:\n{response.text}\n", parent=QApplication.focusWidget()))
        if media_files_info:
            return token, deckHash, media_files_info, media_file_paths, False

    if response.status_code == 500:
        if "Notetype Error: " in response.text:
            missing_note_uuid = response.text.split("Notetype Error: ")[1]
            note_model_dict = UuidFetcher(aqt.mw.col).get_model(missing_note_uuid)
            note_model = NoteModel.from_json(note_model_dict)
            maybe_name = note_model.anki_dict["name"]
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.showCritical(f"The Notetype\n{maybe_name}\ndoes not exist on the cloud deck. Please only use notetypes that the maintainer added.", title="AnkiCollab Upload Error: Notetype not found.")
            )
    return
        
                

def get_commit_info(default_opt = 0):
    options = [
        "None", "Deck Creation", "Updated content", "New content", "Content error",
        "Spelling/Grammar", "New card", "Updated Tags",
        "New Tags", "Bulk Suggestion", "Other", "Note Removal", "Changed Deck"
    ]
    
    # Create the dialog
    dialog = QDialog()
    dialog.setWindowTitle("Commit Information")
    
    # Create the layout
    layout = QVBoxLayout()
    
    # Create the list view for rationale
    listWidget = QListWidget()
    for option in options:
        item = QListWidgetItem(option)
        listWidget.addItem(item)
    listWidget.setCurrentRow(default_opt)
    listWidget.doubleClicked.connect(dialog.accept)  # Submit dialog on double-click
    layout.addWidget(QLabel("Select a rationale (mandatory):"))
    layout.addWidget(listWidget)
    
    # Create the text edit for additional information
    textEdit = QTextEdit()
    textEdit.setFixedHeight(5 * textEdit.fontMetrics().lineSpacing())
    textEdit.setPlaceholderText("Enter additional information (optional, max 255 characters)")
    
    def checkLength():
        text = textEdit.toPlainText()
        if len(text) > 255:
            cursor = textEdit.textCursor()
            pos = cursor.position()
            textEdit.setPlainText(text[:255])
            cursor.setPosition(pos)  # Restore cursor position
            textEdit.setTextCursor(cursor)
    
    textEdit.textChanged.connect(checkLength)
    layout.addWidget(QLabel("Additional Information: (optional)"))
    layout.addWidget(textEdit)

    #Added Ctrl+Return Shortcut to submit the form
    shortcut = QShortcut(QKeySequence("Ctrl+Return"), textEdit)
    shortcut.activated.connect(dialog.accept)
    
    # Create the submit and cancel buttons
    buttonLayout = QHBoxLayout()
    cancelButton = QPushButton("Cancel")
    okButton = QPushButton("Submit")
    buttonLayout.addWidget(cancelButton)
    buttonLayout.addWidget(okButton)
    layout.addLayout(buttonLayout)
    
    dialog.setLayout(layout)
    okButton.clicked.connect(dialog.accept)
    cancelButton.clicked.connect(dialog.reject)
    cancelButton.setFocus()
    textEdit.setReadOnly(True)
    textEdit.mousePressEvent = lambda _: textEdit.setReadOnly(False)
    
    if dialog.exec() == QDialog.DialogCode.Accepted:
        rationale = listWidget.currentIndex().row()
        additional_info = textEdit.toPlainText()
        return rationale, additional_info
    
    aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("Aborting", parent=QApplication.instance().focusWidget()))
    return None, None
    
def suggest_subdeck(did):
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    
    deckHash = get_deck_hash_from_did(did)
    if deckHash is None:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("Config Error: Please update the Local Deck in the Subscriptions window", parent=QApplication.focusWidget()))
        return
    response = requests.get(f"{API_BASE_URL}/GetDeckTimestamp/" + deckHash)
    
    if response and response.status_code == 200:
        last_updated = float(response.text)
        last_pulled = get_timestamp(deckHash)
        if last_pulled is None:
            last_pulled = 0.0
        deck_initializer.remove_unchanged_notes(deck, last_updated, last_pulled)
        deck_initializer.trim_empty_children(deck)
    
    personal_tags = get_personal_tags(deckHash)
    if personal_tags:
        deck_initializer.remove_tags_from_notes(deck, personal_tags)
    
    #spaghetti name fix
    deck.anki_dict["name"] = mw.col.decks.name(did).split("::")[-1]
    (rationale, commit_text) = get_commit_info(9) # Bulk Suggestion
    if rationale is None:
        return
    submit_with_progress(deck, did, rationale, commit_text)
    
def suggest_notes(nids, rationale_id, editor=None):
    notes = [aqt.mw.col.get_note(nid) for nid in nids]
    # Find top level deck and make sure it's the same for all notes
    deckHash = get_deck_hash_from_did(notes[0].cards()[0].did)
    
    if deckHash is None:
        aqt.utils.showInfo("Cannot find the Cloud Deck for these notes")
        return
    
    for note in notes:
        if get_deck_hash_from_did(note.cards()[0].did) != deckHash:
            aqt.utils.showInfo("Please only select cards from the same deck")
            return
        
    did = get_did_from_hash(deckHash)
    if did is None:
        aqt.utils.showInfo("This deck is not published")
        return
    
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        aqt.utils.showInfo("Filtered decks are not supported. Sorry!")
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name, note_ids=nids)
    deck_initializer.trim_empty_children(deck)
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    note_sorter.sort_deck(deck)
    
    personal_tags = get_personal_tags(deckHash)
    if personal_tags:
        deck_initializer.remove_tags_from_notes(deck, personal_tags)
    
    commit_text = ""
    token, force_overwrite = get_maintainer_data(deckHash)
    if not token:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.showWarning("You must be logged in to make this suggestion. Please login under AnkiCollab > Login in the menu bar. And try again", parent=QApplication.focusWidget()))
        return
    
    if rationale_id != 6 and not force_overwrite: # skip the dialog in the new card case
        (rationale_id, commit_text) = get_commit_info(rationale_id)
        if rationale_id is None:
            return
    
    submit_with_progress(deck, did, rationale_id, commit_text)
    
    # After editing a note in the editor, we have to reload it, even after jumping through all the loops in the collection db updating. 
    # Figuring this out took me over 3 hours of debugging. I hope you appreciate the effort and this comment
    if editor:
        editor_note = editor.note
        try:
            assert editor_note is not None
            editor_note.load()
        except NotFoundError:
            # note's been deleted // Impossible to hit here tbh
            return

        editor.set_note(editor_note)

# def make_new_card(note: anki.notes.Note):
#     if mw.form.invokeAfterAddCheckbox.isChecked():
#         suggest_notes([note.id], 6), # 6 New card rationale        
        
def handle_export(did, username) -> str:
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        aqt.utils.showInfo("Filtered decks are not supported. Sorry!")
        return
    
    user_token, _aa = get_maintainer_data("")
    if user_token == "":
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.showWarning("You must be logged in to create a new deck. Please login under AnkiCollab > Login in the menu bar. And try again", parent=QApplication.focusWidget()))
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    deck_initializer.trim_empty_children(deck)
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    note_sorter.sort_deck(deck)

    deck_initializer.remove_tags_from_notes(deck, DEFAULT_PROTECTED_TAGS + [PREFIX_PROTECTED_FIELDS])
    
    media_files = []
    protected_fields = deck.get_protected_fields(None)
    media_files = deck.get_media_file_note_map(protected_fields)
    
    files_info, file_paths = sync_run_async(optimize_media_files, media_files) # Special, because this already runs on main thread so we want to avoid a deadlock
    # We need to recollect all the notes with media after optimization bc paths changed
    deck.refresh_notes(media_files)
    
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)

    data = {"deck": deck_res, "username": username}
    compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
    based_data = base64.b64encode(compressed_data)
    headers = {"Content-Type": "application/json"}
    response = requests.post(f"{API_BASE_URL}/createDeck", data=based_data, headers=headers)

    if response.status_code == 200:
        res = response.json()
        
        if res["status"] == 0:
            msg_box = QMessageBox()
            msg_box.setText(res["message"])
            msg_box.exec()
        
        if res["status"] == 1:
            deckHash = res["message"]
            if aqt.utils.askUser("Do you want to upload media files attached to the Deck, too? Please make sure you only upload media files you own. New media you add to the deck will get uploaded automatically."):
                media_files = []
                protected_fields = deck.get_protected_fields(deckHash)
                media_files = deck.get_media_file_note_map(protected_fields)
                if media_files:
                    op = QueryOp(
                        parent=mw,
                        op=lambda _: upload_media_with_progress(user_token, deckHash, files_info, file_paths, silent=True),
                        success=on_media_pload_upload_done,
                    )
                    if point_version() >= 231000:
                        op.without_collection()
                    op.with_progress().run_in_background()
                return deckHash
            
            aqt.utils.showInfo("Deck published. Thanks for sharing!")                
            return deckHash
    elif response.status_code == 413:
        msg_box = QMessageBox()
        msg_box.setText("Deck is too big! Please reach out via Discord")
        msg_box.exec()        
    else:
        msg_box = QMessageBox()
        msg_box.setText("Unexpected Server response: " + str(response.status_code))
        msg_box.exec()
    
    return ""
