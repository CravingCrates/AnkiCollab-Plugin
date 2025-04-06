import base64
import json
from aqt import QApplication, mw
from collections import defaultdict
import aqt
import requests 
import gzip

from .identifier import get_user_hash
from .var_defs import API_BASE_URL

class ReviewHistory:
    def __init__(self, deck_hash):
        self.deck_hash = deck_hash
        self.deck_id = self.get_did_from_hash(deck_hash)
        self.deck_ids = self.get_deck_and_subdecks(self.deck_id)

    def get_did_from_hash(self, deck_hash):
        strings_data = mw.addonManager.getConfig(__name__)
        if strings_data:
            for hash, details in strings_data.items():
                if hash == deck_hash:
                    return details["deckId"]
        return None

    def get_deck_and_subdecks(self, deck_id):
        if deck_id is None or deck_id == -1 or deck_id == 0:
            return []
        deck_ids = [deck_id]
        for subdeck in mw.col.decks.children(deck_id):
            # Assuming the deck id is the first element in the subdeck tuple
            deck_ids.extend(self.get_deck_and_subdecks(subdeck[1]))
        return deck_ids

    def get_card_data(self, last_upload_date):
        # Query to get the card data of the given decks
        query = f"""
            SELECT cards.id, cards.ord, cards.mod, cards.ivl, cards.factor, cards.reps, cards.lapses, notes.guid, cards.did
            FROM cards
            JOIN notes ON cards.nid = notes.id
            WHERE cards.did IN ({', '.join(map(str, self.deck_ids))})
            AND cards.mod > {last_upload_date}
            AND cards.type > 1
        """
        card_data = list(mw.col.db.execute(query))

        notes_by_deck_and_note_guid = defaultdict(lambda: defaultdict(dict))
        for card in card_data:
            note_guid = card[-2]
            deck_id = card[-1]
            deck_name = mw.col.decks.name(deck_id)

            retention = self.calc_retention(card[0])
            lapses = card[6]
            reps = card[5]

            # Skip this card if the true retention is invalid
            if retention == -1:
                continue

            if 'retention' not in notes_by_deck_and_note_guid[deck_name][note_guid]:
                notes_by_deck_and_note_guid[deck_name][note_guid]['retention'] = []
            if 'lapses' not in notes_by_deck_and_note_guid[deck_name][note_guid]:
                notes_by_deck_and_note_guid[deck_name][note_guid]['lapses'] = []
            if 'reps' not in notes_by_deck_and_note_guid[deck_name][note_guid]:
                notes_by_deck_and_note_guid[deck_name][note_guid]['reps'] = []

            notes_by_deck_and_note_guid[deck_name][note_guid]['retention'].append(retention)
            notes_by_deck_and_note_guid[deck_name][note_guid]['lapses'].append(lapses)
            notes_by_deck_and_note_guid[deck_name][note_guid]['reps'].append(reps)

        for deck_name, notes in notes_by_deck_and_note_guid.items():
            for note_guid, note_data in list(notes.items()):  # Use list to avoid RuntimeError due to dictionary size change during iteration
                for key in note_data:
                    note_data[key] = int(sum(note_data[key]) / len(note_data[key])) if note_data[key] else None
                # Remove the note if it has no valid true retentions
                if not note_data['retention']:
                    del notes[note_guid]

        return notes_by_deck_and_note_guid
    
    def calc_retention(self, card_id):
        flunked, passed = mw.col.db.first("""
        select
        sum(case when ease = 1 and type == 1 then 1 else 0 end), /* flunked */
        sum(case when ease > 1 and type == 1 then 1 else 0 end) /* passed */
        from revlog where cid = ?""", card_id)
        flunked = flunked or 0
        passed = passed or 0
        try:
            temp = int(passed / float(passed + flunked) * 100)
        except ZeroDivisionError:
            temp = -1
        return temp

    def upload_review_history(self, last_upload_date):
        review_history = self.get_card_data(last_upload_date)
        user_hash = get_user_hash()
        data = {
            'user_hash': user_hash,
            'deck_hash': self.deck_hash,
            'review_history': review_history
        }
        compressed_data = gzip.compress(json.dumps(data).encode('utf-8'))
        based_data = base64.b64encode(compressed_data)
        response = requests.post(f"{API_BASE_URL}/UploadDeckStats", data=based_data, headers={'Content-Type': 'application/json'}, timeout=30)
        aqt.mw.taskman.run_on_main(lambda: aqt.utils.tooltip(response.text, parent=QApplication.focusWidget()))
        return

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
