import json
import requests


import aqt
import aqt.utils
import anki

from aqt.qt import *
from aqt import mw

from datetime import datetime, timedelta
import base64
import gzip


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

def submit_deck(deck, did, rationale):    
    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    deckHash = get_deck_hash_from_did(did)
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
            aqt.utils.tooltip(response.text, parent=QApplication.focusWidget())

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
            aqt.utils.tooltip("Aborting due to lack of rationale", parent=QApplication.focusWidget())
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
        msg_box = QMessageBox()
        msg_box.setText("Unexpected Server response: " + str(response.status_code))
        msg_box.exec()
    
    return ""

def onAddCard():
    deck = aqt.mw.col.decks.current()['name']
