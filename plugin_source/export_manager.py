import json
import os
import requests


import aqt
import aqt.utils
import anki

from aqt.qt import *
from aqt import mw
import aqt.utils
from aqt.operations import QueryOp

from datetime import datetime, timedelta
import base64
import gzip

from .google_drive_api import GoogleDriveAPI
from .thread import run_function_in_thread


from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.note import Note
from .crowd_anki.config.config_settings import ConfigSettings
from .crowd_anki.export.note_sorter import NoteSorter
from .crowd_anki.utils.disambiguate_uuids import disambiguate_note_model_uuids

from .crowd_anki.representation import *
from .crowd_anki.representation import deck_initializer
from .crowd_anki.anki.adapters.anki_deck import AnkiDeck
from .crowd_anki.representation.deck import Deck

def get_timestamp(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:        
        for sub, details in strings_data.items():
            if sub == deck_hash:
                date_string = details["timestamp"]
                datetime_obj = datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')
                unix_timestamp = datetime_obj.timestamp()
                return unix_timestamp
    return None

def get_gdrive_data(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:        
        for sub, details in strings_data.items():
            if sub == deck_hash:                
                if "gdrive" not in details or len(details["gdrive"]) == 0:
                    return None
                return details["gdrive"]
    return None

def get_hash_from_local_id(deck_id):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if details["deckId"] == deck_id:
                return hash
    return

def get_deck_hash_from_did(did):
    deckHash = get_hash_from_local_id(did)
    parent = mw.col.decks.parents(did)
    if not deckHash and parent:
        parent_len = len(parent)
        i = 0
        deckHash = get_hash_from_local_id(did)
        while i < parent_len and not deckHash:
            deck_id = parent[parent_len - i - 1]["id"]
            deckHash = get_hash_from_local_id(deck_id)
            i += 1
    return deckHash

def do_nothing(count: int):
    pass

def submit_with_progress(deck, did, rationale):
    op = QueryOp(
        parent=mw,
        op=lambda _: submit_deck(deck, did, rationale, False),
        success=do_nothing,
    )
    op.with_progress("Uploading to AnkiCollab...").run_in_background()

def upload_media_to_gdrive(deck_hash, media_files):
    gdrive_data = get_gdrive_data(deck_hash)
    if gdrive_data is not None:
        api = GoogleDriveAPI(
            service_account=gdrive_data['service_account'],
            folder_id=gdrive_data['folder_id'],
        )
        existing_media = api.list_media_files_in_folder()
        
        # only upload media that is not already on the drive and that we have locally
        dir_path = aqt.mw.col.media.dir()
        missing_media = [media for media in media_files if not any(media_name['name'] == media for media_name in existing_media) and os.path.exists(os.path.join(dir_path, media))]
        
        api.upload_files_to_folder(dir_path, missing_media)
    else:
        if len(media_files) > 0:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder set for this deck."))
            
def submit_deck(deck, did, rationale, media_async = True):    
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    deckHash = get_deck_hash_from_did(did)
    deckPath =  mw.col.decks.name(did)
    
    if deckHash is None:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("Config Error: No local deck id"))
    else:
        data = {"remoteDeck": deckHash, "deckPath": deckPath, "deck": deck_res, "rationale": rationale}
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        headers = {"Content-Type": "application/json"}
        response = requests.post("https://plugin.ankicollab.com/submitCard", data=based_data, headers=headers)
        
        # Hacky, but for bulk suggestions we want the progress bar to include media files, 
        # but for single suggestions we can run it in the background to make it a smoother experience    
        if media_async: 
            run_function_in_thread(upload_media_to_gdrive, deckHash, deck.get_media_file_list())
        else:
            upload_media_to_gdrive(deckHash, deck.get_media_file_list())
            
        if response:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip(f"AnkiCollab Upload:\n{response.text}\n", parent=QApplication.focusWidget()))

def suggest_subdeck(did):
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    
    deckHash = get_deck_hash_from_did(did)
    response = requests.get("https://plugin.ankicollab.com/GetDeckTimestamp/" + deckHash)
    
    if response and response.status_code == 200:
        last_updated = float(response.text)
        last_pulled = get_timestamp(deckHash)
        if last_pulled is None:
            last_pulled = 0.0
        deck_initializer.remove_unchanged_notes(deck, last_updated, last_pulled)
    
    #spaghetti name fix
    deck.anki_dict["name"] = mw.col.decks.name(did).split("::")[-1]
    submit_with_progress(deck, did, 9) # 9: Bulk Suggestion rationale
    
def prep_suggest_card(note: anki.notes.Note, rationale):
    # i'm in the ghetto, help
    cards = note.cards()
    did = mw.col.decks.current()["id"] # lets hope this won't not be overwritten
    if cards:
        did = cards[0].current_deck_id()
        
    deck = Deck(NoteModelFileProvider, mw.col.decks.get(did))
    deck.collection = mw.col
    deck._update_fields()
    deck.metadata = None
    deck._load_metadata()

    newNote = Note.from_collection(mw.col, note.id, deck.metadata.models);
    deck.notes = [newNote]
    #spaghetti name fix
    deck.anki_dict["name"] = mw.col.decks.name(did).split("::")[-1]
    
    if rationale is None: 
        options = [
            "None", "Deck Creation", "Updated content", "New content", "Content error",
            "Spelling/Grammar", "New card", "Updated Tags",
            "New Tags", "Bulk Suggestion", "Other"
        ]

        selected, ok = QInputDialog.getItem(None, "Rationale", "Select a rationale:", options, 0, False)

        if ok:
            rationale = options.index(selected)
        else:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("Aborting due to lack of rationale", parent=QApplication.focusWidget()))
            return
    submit_deck(deck, did, rationale)

def make_new_card(note: anki.notes.Note):
    if mw.form.invokeAfterAddCheckbox.isChecked():
        op = QueryOp(
            parent=mw,
            op=lambda _: prep_suggest_card(note, 6), # 6 New card rationale
            success=do_nothing,
        )
        op.run_in_background()
        
def handle_export(did, email) -> str:
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    note_sorter.sort_deck(deck)

    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    
    data = {"deck": deck_res, "email": email}
    compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
    based_data = base64.b64encode(compressed_data)
    headers = {"Content-Type": "application/json"}
    response = requests.post("https://plugin.ankicollab.com/createDeck", data=based_data, headers=headers)

    if response.status_code == 200:
        res = response.json()
        msg_box = QMessageBox()
        if res["status"] == 0:
            msg_box.setText(res["message"])
        else:
            msg_box.setText("Deck published! Thanks for sharing! Please upload the media manually to Google Drive")
        msg_box.exec()
        
        if res["status"] == 1:
            return res["message"]
    elif response.status_code == 413:
        msg_box = QMessageBox()
        msg_box.setText("Deck is too big! Please reach out via Discord")
        msg_box.exec()        
    else:
        msg_box = QMessageBox()
        msg_box.setText("Unexpected Server response: " + str(response.status_code))
        msg_box.exec()
    
    return ""
