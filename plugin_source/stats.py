import base64
from datetime import datetime, timezone
import json
from statistics import mean

from aqt import mw
from collections import defaultdict
import requests
import gzip

from .utils import DeckManager, get_did_from_hash, get_deck_and_subdecks
from .identifier import get_user_hash
from .var_defs import API_BASE_URL


class ReviewHistory:
    def __init__(self, deck_hash):
        self.deck_hash = deck_hash
        self.deck_id = get_did_from_hash(deck_hash)
        self.deck_ids = get_deck_and_subdecks(self.deck_id)

    def get_card_data(self, last_upload_date: int) -> defaultdict:
        # Query to get the card data of the given decks
        query = f"""
            SELECT cards.id, cards.reps, cards.lapses, notes.guid, cards.did
            FROM cards
            JOIN notes ON cards.nid = notes.id
            WHERE cards.did IN ({', '.join(map(str, self.deck_ids))})
            AND cards.mod > {last_upload_date}
            AND cards.type > 1
        """
        card_data = list(mw.col.db.execute(query))

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

        averages = defaultdict(lambda: defaultdict(lambda: {
            'retention': 0,
            'lapses': 0,
            'reps': 0
        }))

        for deck_name, notes in notes_by_deck_and_note_guid.items():
            for note_guid, note_data in notes.items():

                if note_data['retention']:
                    for key, values in note_data.items():
                        if values:
                            averages[deck_name][note_guid][key] = mean(values)

        return averages


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

        return int(passed / total) * 100

    def upload_review_history(self, last_upload_date: int) -> None:
        
        review_history = self.get_card_data(last_upload_date)

        if len(review_history) == 0:
            return

        user_hash = get_user_hash()
        data = {
            'user_hash': user_hash,
            'deck_hash': self.deck_hash,
            'review_history': review_history
        }
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        requests.post(f"{API_BASE_URL}/UploadDeckStats",
                                 data=based_data,
                                 headers={'Content-Type': 'application/json'},
                                 timeout=30)


    def dump_review_history(self):
        review_history = self.get_card_data(0)
        user_hash = get_user_hash()
        data = {
            'user_hash': user_hash,
            'review_history': review_history
        }
        # Take all the average retention rates from all notes in review_history and calculate the average retention rate for the deck
        # Print the deck name and the average retention rate
        for deck_name, notes in review_history.items():
            retention_rates = []
            for note in notes.values():
                retention_rates.append(note['retention'])
            average_retention_rate = int(sum(retention_rates) / len(retention_rates))
            print(f'{deck_name}: {average_retention_rate}%')
        return data

def update_stats_timestamp(deck_hash: str) -> None:
    with DeckManager() as decks:
        details = decks.get_by_hash(deck_hash)

        if details:
            details["last_stats_timestamp"] = int(datetime.now(timezone.utc).timestamp())

def on_stats_upload_done(done) -> None:
    mw.progress.finish()
