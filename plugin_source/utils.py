import datetime
from datetime import datetime
import aqt
import aqt.utils
from anki.errors import NotFoundError
from aqt.operations import QueryOp
from anki.collection import Collection
from aqt import mw
import aqt.utils
from typing import Optional

def get_timestamp(given_deck_hash):
    with DeckConfigManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details is None:
            return None

        date_string = details["timestamp"]
        datetime_obj = datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')
        unix_timestamp = datetime_obj.timestamp()
        return unix_timestamp


def get_hash_from_local_id(deck_id) -> Optional[str]:
    with DeckConfigManager() as decks:
        for deck_hash, details in decks:
            if details.get("deckId") == deck_id:
                return deck_hash

    return None


def get_deck_hash_from_did(did):
    deck_hash = get_hash_from_local_id(did)
    parent = mw.col.decks.parents(did)
    if not deck_hash and parent:
        parent_len = len(parent)
        i = 0
        deck_hash = get_hash_from_local_id(did)
        while i < parent_len and not deck_hash:
            deck_id = parent[parent_len - i - 1]["id"]
            deck_hash = get_hash_from_local_id(deck_id)
            i += 1
    return deck_hash


def get_did_from_hash(given_deck_hash):
    with DeckConfigManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        return details and details.get("deckId")


def get_local_deck_from_hash(input_hash):
    deck_id = get_did_from_hash(input_hash)

    if deck_id is None:
        return "None"

    return mw.col.decks.name(deck_id)

def get_local_deck_from_id(deck_id):
    return mw.col.decks.name(deck_id)

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

from contextlib import AbstractContextManager
from typing import Dict, Iterator, Optional, Tuple

class DeckConfigManager(AbstractContextManager):
    def __init__(self):
        self._raw_data: Dict[str, Dict] = {}
        self._filtered_items: Dict[str, Dict] = {}

    def __enter__(self) -> "DeckConfigManager":
        self._raw_data = mw.addonManager.getConfig(__name__) or {}

        self._filtered_items = {
            deck_hash: details
            for deck_hash, details in self._raw_data.items()
            if deck_hash not in ['settings', 'auth']
        }

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        print('saved')
        mw.addonManager.writeConfig(__name__, self._raw_data)

    def get_by_hash(self, deck_hash: str) -> Optional[Dict]:
        return self._filtered_items.get(deck_hash)

    def __iter__(self) -> Iterator[Tuple[str, Dict]]:
        return iter(self._filtered_items.items())



def get_deck_and_subdecks(deck_id):
    if deck_id is None or deck_id == -1 or deck_id == 0:
        return []
    deck_ids = [deck_id]
    try:
        for subdeck in mw.col.decks.children(deck_id):
            deck_ids.extend(get_deck_and_subdecks(subdeck[1]))
    except NotFoundError:
        return deck_ids
    except Exception as e:
        return deck_ids
    return deck_ids