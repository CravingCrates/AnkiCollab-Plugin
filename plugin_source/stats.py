import base64
from datetime import datetime, timedelta, timezone
import json

from aqt import mw
from collections import defaultdict
import aqt
import gzip

from .utils import DeckManager, get_did_from_hash, get_deck_and_subdecks
from .auth_manager import auth_manager
from .identifier import get_user_hash
from .var_defs import API_BASE_URL


class ReviewHistory:
    def __init__(self, deck_hash):
        self.deck_hash = deck_hash
        self.deck_id = get_did_from_hash(deck_hash)
        self.deck_ids = get_deck_and_subdecks(self.deck_id)

    def get_card_data(self, last_upload_date: int) -> defaultdict:
        # Query to get the card data of the given decks
        placeholders = ', '.join('?' for _ in self.deck_ids)
        query = f"""
            SELECT cards.id, cards.reps, cards.lapses, notes.guid, cards.did
            FROM cards
            JOIN notes ON cards.nid = notes.id
            WHERE cards.did IN ({placeholders})
            AND cards.mod > ?
            AND cards.type > 1
        """
        card_data = list(mw.col.db.execute(query, *self.deck_ids, last_upload_date))

        notes_by_deck_and_note_guid = defaultdict(lambda: defaultdict(lambda: {
            'retention': [],
            'lapses': [],
            'reps': []
        }))

        for card_id, reps, lapses, note_guid, deck_id in card_data:
            deck_name = mw.col.decks.name(deck_id)
            retention = self.calc_retention(card_id)

            # Skip this card if the true retention is invalid
            if retention == -1:
                continue

            notes_by_deck_and_note_guid[deck_name][note_guid]['retention'].append(retention)
            notes_by_deck_and_note_guid[deck_name][note_guid]['lapses'].append(lapses)
            notes_by_deck_and_note_guid[deck_name][note_guid]['reps'].append(reps)


        for deck_name, notes in notes_by_deck_and_note_guid.items():
            note_guids_to_remove = []

            for note_guid, note_data in notes.items():
                for key, values in note_data.items():
                    if values:
                        note_data[key] = int(sum(values) / len(values))

                # Remove the note if it has no valid true retentions
                if not note_data['retention']:
                    note_guids_to_remove.append(note_guid)

            for note_guid in note_guids_to_remove:
                del notes[note_guid]

        return notes_by_deck_and_note_guid

    def calc_retention(self, card_id) -> int:
        flunked, passed = mw.col.db.first("""
        select
        sum(case when ease = 1 and type == 1 then 1 else 0 end), /* flunked */
        sum(case when ease > 1 and type == 1 then 1 else 0 end) /* passed */
        from revlog where cid = ?""", card_id)
        flunked = flunked or 0
        passed = passed or 0

        total = passed + flunked

        if total == 0:
            return -1

        return int(passed * 100 / total)

    def upload_review_history(self, last_upload_date: int) -> None:
        
        review_history = self.get_card_data(last_upload_date)
        
        if len(review_history) == 0:
            return

        token = auth_manager.get_token()
        if not token:
            return
        from .api_client import api_client
        data = {
            'deck_hash': self.deck_hash,
            'review_history': review_history
        }
        api_client.post_gzip("/UploadDeckStats", data, timeout=30)


    def dump_review_history(self):
        review_history = self.get_card_data(0)
        user_hash = get_user_hash()
        if not user_hash:
            return None
        data = {
            'user_hash': user_hash,
            'review_history': review_history
        }
        return data

def update_stats_timestamp(deck_hash: str) -> None:
    with DeckManager() as decks:
        details = decks.get_by_hash(deck_hash)

        if details:
            details["last_stats_timestamp"] = int(datetime.now(timezone.utc).timestamp())

def on_stats_upload_done(done) -> None:
    mw.progress.finish()
