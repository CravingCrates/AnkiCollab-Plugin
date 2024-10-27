import json
import os
import requests


import aqt
import aqt.utils
import anki
from anki.utils import point_version

from aqt.qt import *
from aqt import mw
import aqt.utils
from aqt.operations import QueryOp

from datetime import datetime, timedelta, timezone
import base64
import gzip

from .crowd_anki.representation.note_model import NoteModel

from .crowd_anki.utils.uuid import UuidFetcher

from .var_defs import DEFAULT_PROTECTED_TAGS, PREFIX_PROTECTED_FIELDS

from .dialogs import RateAddonDialog

from .google_drive_api import GoogleDriveAPI, get_gdrive_data
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

from .utils import get_deck_hash_from_did, get_local_deck_from_hash, get_timestamp, get_did_from_hash

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
                if (datetime.now(timezone.utc) - last_ratepls_dt).days > 14:
                    if not strings_data["settings"]["rated_addon"]: # only ask if they haven't rated the addon yet
                        strings_data["settings"]["last_ratepls"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                        dialog = RateAddonDialog()
                        dialog.exec()
            mw.addonManager.writeConfig(__name__, strings_data)

def media_upload_progress_cb(curr: int, max_i: int):
    aqt.mw.taskman.run_on_main(
        lambda: aqt.mw.progress.update(
            label=
             "Uploading missing media...\n"
            f"{curr} / {max_i}",
            value=curr,
            max=max_i,
        )
    )

def on_media_upload_done(count: int) -> None:
    mw.progress.finish()
    if count == 0:
        aqt.utils.showWarning("No new media uploaded.")
    else:
        aqt.utils.showInfo("Upload done!")

def upload_media_with_progress(deck_hash, media_files):
    gdrive_data = get_gdrive_data(deck_hash)
    if gdrive_data is not None:
        api = GoogleDriveAPI(
            service_account=gdrive_data['service_account'],
            folder_id=gdrive_data['folder_id'],
        )
        dir_path = aqt.mw.col.media.dir()
        op = QueryOp(
            parent=mw,
            op=lambda _: api.upload_files_to_folder(dir_path, media_files, media_upload_progress_cb),
            success=on_media_upload_done
        )
        if point_version() >= 231000:
            op.without_collection()
        op.with_progress(f"Checking {len(media_files)} media files...").run_in_background()
    else:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder set for this deck. Please set one in the AnkiCollab settings.", parent=QApplication.focusWidget()))

def submit_with_progress(deck, did, rationale, commit_text):
    upload_media = aqt.utils.askUser("Do you want to upload the media to Google Drive?")
    
    op = QueryOp(
        parent=mw,
        op=lambda _: submit_deck(deck, did, rationale, commit_text, False, upload_media),
        success=lambda _: 1,
    )
    if point_version() >= 231000:
        op.without_collection()
    op.with_progress("Uploading to AnkiCollab...").run_in_background()

def upload_media_to_gdrive(deck_hash, media_files):
    gdrive_data = get_gdrive_data(deck_hash)
    if gdrive_data is not None:                
        api = GoogleDriveAPI(
            service_account=gdrive_data['service_account'],
            folder_id=gdrive_data['folder_id'],
        )
        dir_path = aqt.mw.col.media.dir()
        api.upload_files_to_folder(dir_path, media_files)
    else:
        if len(media_files) > 0:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder set for this deck.", parent=QApplication.focusWidget()))

def get_maintainer_data():    
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None:
        if "settings" in strings_data and strings_data["settings"]["token"] != "":
            return strings_data["settings"]["token"], strings_data["settings"]["auto_approve"]
    return "", False

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
            
def submit_deck(deck, did, rationale, commit_text, media_async, upload_media):    
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
        
    deckHash = get_deck_hash_from_did(did)
    newName = get_local_deck_from_hash(deckHash)
    deckPath =  mw.col.decks.name(did)
    
    if deckHash is None:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("Config Error: Please update the Local Deck in the Subscriptions window", parent=QApplication.focusWidget()))
    else:
        token, auto_approve = get_maintainer_data()
        data = {
            "remote_deck": deckHash, 
            "deck_path": deckPath, 
            "new_name": newName, 
            "deck": deck_res, 
            "rationale": rationale,
            "commit_text": commit_text,
            "token": token,
            "force_overwrite": auto_approve,
            }
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        headers = {"Content-Type": "application/json"}
        response = requests.post("https://plugin.ankicollab.com/submitCard", data=based_data, headers=headers)
        
        # Hacky, but for bulk suggestions we want the progress bar to include media files, 
        # but for single suggestions we can run it in the background to make it a smoother experience            
        if upload_media:
            if media_async: 
                run_function_in_thread(upload_media_to_gdrive, deckHash, deck.get_media_file_list())
            else:
                upload_media_to_gdrive(deckHash, deck.get_media_file_list())    
                
        if response.status_code == 200:
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip(f"AnkiCollab Upload:\n{response.text}\n", parent=QApplication.focusWidget()))
            aqt.mw.taskman.run_on_main(lambda: ask_for_rating())
            return
    
        if response.status_code == 500:
            print(response.text)
            if "Notetype Error: " in response.text:
                missing_note_uuid = response.text.split("Notetype Error: ")[1]
                note_model_dict = UuidFetcher(aqt.mw.col).get_model(missing_note_uuid)
                note_model = NoteModel.from_json(note_model_dict)
                maybe_name = note_model.anki_dict["name"]
                aqt.mw.taskman.run_on_main(
                    lambda: aqt.utils.showCritical(f"The Notetype\n{maybe_name}\ndoes not exist on the cloud deck. Please only use notetypes that the maintainer added.", title="AnkiCollab Upload Error: Notetype not found.")
                )
                

def get_commit_info(default_opt = 0):
    options = [
        "None", "Deck Creation", "Updated content", "New content", "Content error",
        "Spelling/Grammar", "New card", "Updated Tags",
        "New Tags", "Bulk Suggestion", "Other", "Note Removal"
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
    response = requests.get("https://plugin.ankicollab.com/GetDeckTimestamp/" + deckHash)
    
    if response and response.status_code == 200:
        last_updated = float(response.text)
        last_pulled = get_timestamp(deckHash)
        if last_pulled is None:
            last_pulled = 0.0
        deck_initializer.remove_unchanged_notes(deck, last_updated, last_pulled)
    
    personal_tags = get_personal_tags(deckHash)
    if personal_tags:
        deck_initializer.remove_tags_from_notes(deck, personal_tags)
    
    #spaghetti name fix
    deck.anki_dict["name"] = mw.col.decks.name(did).split("::")[-1]
    (rationale, commit_text) = get_commit_info(9) # Bulk Suggestion
    if rationale is None:
        return
    submit_with_progress(deck, did, rationale, commit_text)
    
def bulk_suggest_notes(nids):
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
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    note_sorter.sort_deck(deck)
    
    personal_tags = get_personal_tags(deckHash)
    if personal_tags:
        deck_initializer.remove_tags_from_notes(deck, personal_tags)
    
    (rationale, commit_text) = get_commit_info(9)# 9: Bulk Suggestion rationale
    if rationale is None:
        return
    submit_with_progress(deck, did, rationale, commit_text)

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

    newNote = Note.from_collection(mw.col, note.id, deck.metadata.models)
    
    deckHash = get_deck_hash_from_did(did)
    personal_tags = get_personal_tags(deckHash)
    if personal_tags:
        newNote.remove_tags(personal_tags)
    
    deck.notes = [newNote]
    #spaghetti name fix
    deck.anki_dict["name"] = mw.col.decks.name(did).split("::")[-1]
    
    commit_text = ""
    if rationale is None:
        (rationale, commit_text) = get_commit_info()
    if rationale is None:
        return
        
    submit_deck(deck, did, rationale, commit_text, True, True)

def make_new_card(note: anki.notes.Note):
    if mw.form.invokeAfterAddCheckbox.isChecked():
        op = QueryOp(
            parent=mw,
            op=lambda _: prep_suggest_card(note, 6), # 6 New card rationale
            success=lambda _: 1,
        )
        if point_version() >= 231000:
            op.without_collection()
        op.run_in_background()
        
def handle_export(did, email) -> str:
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        aqt.utils.showInfo("Filtered decks are not supported. Sorry!")
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    note_sorter.sort_deck(deck)

    deck_initializer.remove_tags_from_notes(deck, DEFAULT_PROTECTED_TAGS + [PREFIX_PROTECTED_FIELDS])
        
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
            msg_box.setText("Deck published. Thanks for sharing! Please upload the media manually to Google Drive")
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
