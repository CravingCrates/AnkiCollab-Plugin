
import datetime
from datetime import datetime, timedelta
import aqt
import aqt.utils
import anki
from anki.utils import point_version
from aqt.operations import QueryOp
from anki.collection import Collection
from aqt.qt import *
from aqt import mw
import aqt.utils


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
            if "deckId" in details and details["deckId"] == deck_id:
                return hash
    return None

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

def get_did_from_hash(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == deck_hash:
                return details["deckId"]
    return None

def get_local_deck_from_hash(input_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == input_hash:
                return mw.col.decks.name(details["deckId"])
    return "None"

def create_backup(background: bool = False):
    print("Creating backup...")
    def do_backup(col: Collection):
        try:
            _ = col.create_backup(
                backup_folder=aqt.mw.pm.backupFolder(),
                force=True,
                wait_for_completion=True,
            )
        except Exception as e:
            print(f"Error creating backup: {e}")
            
    if background:
        QueryOp(
                parent=mw,
                op=do_backup,
                success=lambda _: 1,
            ).run_in_background()
    else:
        do_backup(aqt.mw.col)
