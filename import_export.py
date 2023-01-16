from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
import json
import requests

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

def handle_pull(input_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and len(strings_data) > 0:
        response = requests.post("https://plugin.ankicollab.com/pullChanges", json=strings_data)
        webresult = response.json()
        for entry in webresult:
            subscription = json.loads(entry)
            deck = deck_initializer.from_json(subscription['deck'])
            config = ImportConfig(
                    add_tag_to_cards= [],
                    use_notes=True,
                    use_media=False,
                    ignore_deck_movement= True
                )
            for protected_field in subscription['protected_fields']:
                model_name = protected_field['name']
                for field in protected_field['fields']:
                    field_name = field['name']
                    config.add_field(model_name, field_name)
            
            deck.save_to_collection(aqt.mw.col, import_config=config)
            if input_hash:
                for hash, details in strings_data.items():
                    if details["deckId"] == 0 and hash == input_hash: # should only be the case once when they add a new subscription and never ambiguous
                        details["deckId"] = aqt.mw.col.decks.id(deck.anki_dict["name"])
                mw.addonManager.writeConfig(__name__, strings_data)
        # i would like to show a tooltip of the total amount of new cards (simply var += len(deck.notes)), but I cannot print it since its in a different thread. maybe somebody reading this knows a cool way (signal? MetaObject.invoke?)

def get_hash_from_local_id(deck_id):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if details["deckId"] == deck_id:
                return hash
    return

def prep_suggest_card(note: anki.notes.Note):
        decks = mw.col.decks
        deck = Deck(NoteModelFileProvider, decks.current())
        deck.collection = mw.col
        deck._update_fields()
        deck.metadata = None
        deck._load_metadata()

        newNote = Note.from_collection(mw.col, note.id, deck.metadata.models);
        deck.notes = [newNote]
        deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
        
        # TODO Remove compatibility shims for Anki 2.1.46 and lower.
        by_name = decks.by_name if hasattr(decks, 'by_name') else decks.byName
        deckHash = get_hash_from_local_id(by_name(decks.current()['name'].split("::")[0])['id'])
        
        deckPath =  decks.current()['name']
        if deckHash is None:
            aqt.utils.tooltip("Config Error: No local deck id")
        else:
            data = {"remoteDeck": deckHash, "deckPath": deckPath, "deck": deck_res}
            response = requests.post("https://plugin.ankicollab.com/submitCard", json=data)
            if response:
                aqt.utils.tooltip(response.text)

def make_new_card(note: anki.notes.Note):
    if mw.form.invokeAfterAddCheckbox.isChecked():
        prep_suggest_card(note)
        
def handle_export(did, email) -> str:
    deck = AnkiDeck(aqt.mw.col.decks.get(did, default=False))
    if deck.is_dynamic:
        return
    
    disambiguate_note_model_uuids(aqt.mw.col)
    deck = deck_initializer.from_collection(aqt.mw.col, deck.name)
    note_sorter = NoteSorter(ConfigSettings.get_instance())
    deck.notes = note_sorter.sort_notes(deck.notes)

    deck_res = json.dumps(deck, default=Deck.default_json, sort_keys=True, indent=4, ensure_ascii=False)
    
    data = {"deck": deck_res, "email": email}
    response = requests.post("https://plugin.ankicollab.com/createDeck", json=data)
    
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
    else:
        print(response.text)
        msg_box = QMessageBox()
        msg_box.setText("Unexpected Server response.")
        msg_box.exec()
    
    return ""

def onAddCard():
    deck = aqt.mw.col.decks.current()['name']