from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
import json
import requests
from datetime import datetime, timedelta

from pprint import pp
from typing import List

import aqt
import aqt.utils
import anki

from aqt.qt import *
from aqt import mw

from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.note import Note
from .crowd_anki.config.config_settings import ConfigSettings
from .crowd_anki.export.note_sorter import NoteSorter
from .crowd_anki.utils.disambiguate_uuids import disambiguate_note_model_uuids

from .crowd_anki.representation import *
from .crowd_anki.representation import deck_initializer
from .crowd_anki.anki.adapters.anki_deck import AnkiDeck
from .crowd_anki.representation.deck import Deck

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

    use_notes: bool
    use_media: bool

    ignore_deck_movement: bool

def get_local_deck_from_hash(input_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == input_hash:
                return mw.col.decks.name(details["deckId"])
    return "None"

def update_timestamp(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:        
        for sub, details in strings_data.items():
            if sub == deck_hash:
                details["timestamp"] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        mw.addonManager.writeConfig(__name__, strings_data)        

def install_update(subscription):    
    media_url = subscription['media_url']
    deck = deck_initializer.from_json(subscription['deck'])
    config = prep_config(subscription['protected_fields'])
    deck.save_to_collection(media_url, aqt.mw.col, import_config=config)
    return deck.anki_dict["name"]
    
def abort_update(deck_hash):
    update_timestamp(deck_hash)

def postpone_update():
    pass
        
class ChangelogDialog(QDialog):
    def __init__(self, changelog, deck_hash):
        super().__init__()
        local_name = get_local_deck_from_hash(deck_hash)
        self.setWindowTitle(f"Changelog for Deck {local_name}")
        self.setModal(True)

        layout = QVBoxLayout()

        label = QLabel("The following changes are available:")
        layout.addWidget(label)

        changelog_text = QTextBrowser()
        changelog_text.setPlainText(changelog)
        layout.addWidget(changelog_text)

        button_box = QDialogButtonBox()
        install_button = button_box.addButton("Install", QDialogButtonBox.ButtonRole.AcceptRole)
        later_button = button_box.addButton("Later", QDialogButtonBox.ButtonRole.RejectRole)
        skip_button = QPushButton("Skip")
        button_box.addButton(skip_button, QDialogButtonBox.ButtonRole.ActionRole)

        layout.addWidget(button_box)

        self.setLayout(layout)

        install_button.clicked.connect(self.accept)
        later_button.clicked.connect(self.reject)
        skip_button.clicked.connect(self.skip_update)

        self.adjustSize()

    def skip_update(self):
        self.done(2)

def prep_config(protected_fields):
    config = ImportConfig(
            add_tag_to_cards= [],
            use_notes=True,
            use_media=False,
            ignore_deck_movement= True
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
    strings_data = mw.addonManager.getConfig(__name__)
    for subscription in webresult:     
        if input_hash: # New deck
            deck_name = install_update(subscription)
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
            
def get_hash_from_local_id(deck_id):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if details["deckId"] == deck_id:
                return hash
    return

def submit_deck(deck, did, rationale):    
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    parent = mw.col.decks.parents(did)
    if parent:
        deckHash = get_hash_from_local_id(parent[0]["id"])
    else:
        deckHash = get_hash_from_local_id(did)
    deckPath =  mw.col.decks.name(did)
    
    if deckHash is None:
        aqt.utils.tooltip("Config Error: No local deck id")
    else:
        data = {"remoteDeck": deckHash, "deckPath": deckPath, "deck": deck_res, "rationale": rationale}
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        headers = {"Content-Type": "application/json"}
        response = requests.post("https://plugin.ankicollab.com/submitCard", data=based_data, headers=headers)
        if response:
            print(response)
            aqt.utils.tooltip(response.text, parent=mw)

def suggest_subdeck(did):
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    note_sorter.sort_deck(deck)
    #spaghetti name fix
    deck.anki_dict["name"] = mw.col.decks.name(did).split("::")[-1]
    submit_deck(deck, did, 9) # 9: Bulk Suggestion rationale
    
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
            aqt.utils.tooltip("Aborting due to lack of rationale", parent=mw)
            return
    submit_deck(deck, did, rationale)

def make_new_card(note: anki.notes.Note):
    if mw.form.invokeAfterAddCheckbox.isChecked():
        prep_suggest_card(note, 6) # 6 New card to add rationale
        
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
            msg_box.setText("Deck published! Thanks for sharing!")
        msg_box.exec()
        
        if res["status"] == 1:
            return res["message"]
    elif response.status_code == 413:
        msg_box = QMessageBox()
        msg_box.setText("Deck is too big! Please reach out via Discord")
        msg_box.exec()        
    else:
        print(response.text)
        msg_box = QMessageBox()
        msg_box.setText("Unexpected Server response: " + str(response.status_code))
        msg_box.exec()
    
    return ""

def onAddCard():
    deck = aqt.mw.col.decks.current()['name']