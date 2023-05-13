
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
import json
import os
import requests
from datetime import datetime, timedelta
from concurrent.futures import Future

from pprint import pp
from typing import List

import aqt
import aqt.utils
from aqt.operations import QueryOp
import anki

from aqt.qt import *
from aqt import mw
from .dialogs import ChangelogDialog
from .dialogs import OptionalTagsDialog

from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.note import Note
from .crowd_anki.config.config_settings import ConfigSettings
from .crowd_anki.export.note_sorter import NoteSorter
from .crowd_anki.utils.disambiguate_uuids import disambiguate_note_model_uuids

from .crowd_anki.representation import *
from .crowd_anki.representation import deck_initializer
from .crowd_anki.anki.adapters.anki_deck import AnkiDeck
from .crowd_anki.representation.deck import Deck

from .google_drive_api import GoogleDriveAPI


import base64
import gzip

@dataclass
class ConfigEntry:
    config_name: str
    default_value: any

@dataclass
class PersonalFieldsHolder:
    personal_fields: defaultdict = field(init=False, default_factory=lambda: defaultdict(list))

    def is_personal_field(self, model_name, field_name):
        if model_name in self.personal_fields:
            if field_name in self.personal_fields[model_name]:
                return True
        return False

    def add_field(self, model_name, field_name):
        self.personal_fields[model_name].append(field_name)

@dataclass
class ImportConfig(PersonalFieldsHolder):
    add_tag_to_cards: List[str]
    
    optional_tags: List[str]
    has_optional_tags: bool

    use_notes: bool
    use_media: bool

    ignore_deck_movement: bool
    
def media_download_progress_cb(progress: int):
    aqt.mw.taskman.run_on_main(
        lambda: aqt.mw.progress.update(
            label="Downloading missing media...",
            value=progress + 1,
            max=101,
        )
    )

def on_media_download_done(count: int) -> None:
    mw.col.media.check()
    mw.progress.finish()
    if count == 0:
        aqt.utils.showWarning("No new media downloaded.")
    elif count == -1:
        aqt.utils.showInfo("Missing media files not found on Google Drive.")
    elif count == -2:
        aqt.utils.showInfo("Google API Error.")

def handle_media_import(media_files, api):
    if media_files is None:
        return    
    dir_path = aqt.mw.col.media.dir()
    missing_files = []
    for file_name in media_files:
        if not os.path.exists(os.path.join(dir_path, file_name)):
            missing_files.append(file_name)
    # Download the missing files
    if len(missing_files) > 0:        
        op = QueryOp(
            parent=mw,
            op=lambda _: api.download_selected_files_as_zip(missing_files, dir_path, media_download_progress_cb),
            success=on_media_download_done,
        )
        op.with_progress(f"Downloading {len(missing_files)} media files...").run_in_background()

def update_optional_tag_config(deck_hash, optional_tags):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == deck_hash:
                details["optional_tags"] = optional_tags
    mw.addonManager.writeConfig(__name__, strings_data)
    
def get_optional_tags(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == deck_hash:
                if "optional_tags" not in details:
                    return {}
                return details["optional_tags"]
    return {}

def check_optional_tag_changes(deck_hash, optional_tags):
    sorted_old = sorted(get_optional_tags(deck_hash).keys())
    sorted_new = sorted(optional_tags)
    return sorted_old != sorted_new

def update_timestamp(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:        
        for sub, details in strings_data.items():
            if sub == deck_hash:
                details["timestamp"] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                break
        mw.addonManager.writeConfig(__name__, strings_data)     
        
def update_gdrive_data(deck_hash, gdrive_new):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:        
        for sub, details in strings_data.items():
            if sub == deck_hash:
                details["gdrive"] = gdrive_new
                break
        mw.addonManager.writeConfig(__name__, strings_data)   

def install_update(subscription):
    if check_optional_tag_changes(subscription['deck_hash'], subscription['optional_tags']):
        dialog = OptionalTagsDialog(get_optional_tags(subscription['deck_hash']), subscription['optional_tags'])
        dialog.exec()
        update_optional_tag_config(subscription['deck_hash'], dialog.get_selected_tags())
    subscribed_tags = get_optional_tags(subscription['deck_hash'])
    
    service_account = subscription['gdrive']['service_account'] 
    gdrive_folder = subscription['gdrive']['folder_id']
    
    deck = deck_initializer.from_json(subscription['deck'])
    config = prep_config(subscription['protected_fields'], [tag for tag, value in subscribed_tags.items() if value], True if subscription['optional_tags'] else False)
    deck.save_to_collection(aqt.mw.col, import_config=config)
    
    # Handle Media
    if subscription['gdrive']['service_account'] != "":
        update_gdrive_data(subscription['deck_hash'], subscription['gdrive'])
        api = GoogleDriveAPI(
            service_account=service_account,
            folder_id=gdrive_folder
        )
        handle_media_import(deck.media_files, api)
    else:
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip("No Google Drive folder found. Please ask the maintainer to set one up on the website."))
    
    return deck.anki_dict["name"]
    
def abort_update(deck_hash):
    update_timestamp(deck_hash)

def postpone_update():
    pass
        

def prep_config(protected_fields, optional_tags, has_optional_tags):
    config = ImportConfig(
            add_tag_to_cards= [],
            optional_tags= optional_tags,
            has_optional_tags= has_optional_tags,
            use_notes=True,
            use_media=False,
            ignore_deck_movement= False
        )
    for protected_field in protected_fields:
        model_name = protected_field['name']
        for field in protected_field['fields']:
            field_name = field['name']
            config.add_field(model_name, field_name)

    return config

def show_changelog_popup(subscription):
    changelog = subscription['changelog']
    deck_hash = subscription['deck_hash']
      
    dialog = ChangelogDialog(changelog, deck_hash)
    choice = dialog.exec()

    if choice == QDialog.DialogCode.Accepted:
        install_update(subscription)
        update_timestamp(deck_hash)
    elif choice == QDialog.DialogCode.Rejected:
        postpone_update()
    else:
        abort_update(deck_hash)
     
def import_webresult(webresult, input_hash):    
    #if webresult is empty, make popup to tell user that there are no updates
    if not webresult:
        msg_box = QMessageBox()
        msg_box.setWindowTitle("AnkiCollab")
        msg_box.setText("You're already up-to-date!")
        msg_box.exec()
        return
        
    for subscription in webresult:     
        if input_hash: # New deck
            deck_name = install_update(subscription)
            strings_data = mw.addonManager.getConfig(__name__)
            for hash, details in strings_data.items():
                if details["deckId"] == 0 and hash == input_hash: # should only be the case once when they add a new subscription and never ambiguous
                    details["deckId"] = aqt.mw.col.decks.id(deck_name)
                    # large decks use cached data that may be a day old, so we need to update the timestamp to force a refresh
                    details["timestamp"] = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')

            mw.addonManager.writeConfig(__name__, strings_data)
        else: # Update deck
            show_changelog_popup(subscription)

def handle_pull(input_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and len(strings_data) > 0:
        response = requests.post("https://plugin.ankicollab.com/pullChanges", json=strings_data if input_hash is None else {input_hash: strings_data[input_hash]})
        if response.status_code == 200:
            compressed_data = base64.b64decode(response.content)
            decompressed_data = gzip.decompress(compressed_data)
            webresult = json.loads(decompressed_data.decode('utf-8'))
            aqt.mw.taskman.run_on_main(lambda: import_webresult(webresult, input_hash))
        else:            
            infot = "A Server Error occurred. Please notify us!"
            aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip(infot))