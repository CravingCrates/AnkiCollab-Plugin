import base64
from datetime import datetime, timedelta, timezone
import json
from typing import Optional

from aqt import mw
from collections import defaultdict
import aqt
import gzip

from . import consent
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
        # Query to get the card data of the given decks.
        #
        # A card borrowed by a filtered deck keeps its home deck in `odid` and
        # points `did` at the filtered deck, so both columns must be matched -
        # otherwise studying through a filtered deck uploads nothing.
        placeholders = ", ".join("?" for _ in self.deck_ids)
        query = f"""
            SELECT cards.id, cards.reps, cards.lapses, notes.guid, cards.did, cards.odid
            FROM cards
            JOIN notes ON cards.nid = notes.id
            WHERE (cards.did IN ({placeholders}) OR cards.odid IN ({placeholders}))
            AND cards.mod > ?
            AND cards.type > 1
        """
        card_data = list(
            mw.col.db.execute(query, *self.deck_ids, *self.deck_ids, last_upload_date)
        )

        tracked_deck_ids = set(self.deck_ids)
        notes_by_deck_and_note_guid = defaultdict(
            lambda: defaultdict(lambda: {"retention": [], "lapses": [], "reps": []})
        )

        for card_id, reps, lapses, note_guid, deck_id, original_deck_id in card_data:
            # Report the card under the deck it belongs to, not under the
            # filtered deck it is temporarily studying in.
            if original_deck_id in tracked_deck_ids:
                deck_id = original_deck_id

            deck_name = mw.col.decks.name(deck_id)
            retention = self.calc_retention(card_id)

            # Skip this card if the true retention is invalid
            if retention == -1:
                continue

            notes_by_deck_and_note_guid[deck_name][note_guid]["retention"].append(
                retention
            )
            notes_by_deck_and_note_guid[deck_name][note_guid]["lapses"].append(lapses)
            notes_by_deck_and_note_guid[deck_name][note_guid]["reps"].append(reps)

        for notes in notes_by_deck_and_note_guid.values():
            for note_guid, note_data in list(notes.items()):
                # Remove the note if it has no valid true retentions. All three
                # metrics are appended together, so an empty retention list
                # means nothing was collected for this note. A note whose cards
                # were all failed (0% retention) is real data and is kept.
                if not note_data["retention"]:
                    del notes[note_guid]
                    continue

                for key, values in note_data.items():
                    note_data[key] = int(sum(values) / len(values))

        return notes_by_deck_and_note_guid

    def calc_retention(self, card_id) -> int:
        """True retention in percent, or -1 when the card has no usable answers.

        Retention is Anki's definition: the share of review answers that were
        not "Again". Answers that do not affect scheduling (manual
        rescheduling, or cramming in a filtered deck that is not allowed to
        reschedule) carry no rating and are ignored.

        A card that has not reached a review step yet - it graduated out of
        learning today, or was answered in a filtered deck - still has real
        rated answers, so its retention falls back to those instead of dropping
        the card and its whole note.
        """
        row = mw.col.db.first(
            """
        select
        sum(case when ease == 1 and type == 1 then 1 else 0 end), /* flunked */
        sum(case when ease > 1 and type == 1 then 1 else 0 end), /* passed */
        sum(case when ease == 1 then 1 else 0 end), /* flunked, any step */
        sum(case when ease > 1 then 1 else 0 end) /* passed, any step */
        from revlog
        where cid = ?
        and ease > 0
        and not (type == 3 and factor == 0)""",
            card_id,
        )
        flunked, passed, flunked_any, passed_any = row or (0, 0, 0, 0)
        flunked = flunked or 0
        passed = passed or 0

        if passed + flunked == 0:
            flunked = flunked_any or 0
            passed = passed_any or 0

        total = passed + flunked

        if total == 0:
            return -1

        return int(passed * 100 / total)

    def upload_review_history(self, last_upload_date: int) -> Optional[int]:
        """Upload the review history recorded since *last_upload_date*.

        Returns the timestamp this upload covered, which the caller stores as
        the new ``last_stats_timestamp``. ``None`` means nothing reached the
        server and the old timestamp has to be kept, otherwise the reviews in
        that window are skipped forever.
        """

        # Defence in depth: no caller may upload without a recorded, current
        # consent for this deck.
        if not consent.has_granted_consent(self.deck_hash):
            return None

        # Captured before querying, so answers given while the request is in
        # flight are still picked up by the next upload.
        covered_until = int(datetime.now(timezone.utc).timestamp())

        review_history = self.get_card_data(last_upload_date)

        if not any(review_history.values()):
            return covered_until

        token = auth_manager.get_token()
        if not token:
            return None
        from .api_client import api_client

        data = {
            "deck_hash": self.deck_hash,
            "review_history": review_history,
            "consent_version": consent.CURRENT_WORDING_VERSION,
        }
        try:
            response = api_client.post_gzip("/UploadDeckStats", data, timeout=30)
        except Exception as exc:
            return None

        if not response.ok:
            return None

        return covered_until

    def dump_review_history(self):
        review_history = self.get_card_data(0)
        user_hash = get_user_hash()
        if not user_hash:
            return None
        data = {"user_hash": user_hash, "review_history": review_history}
        return data


def update_stats_timestamp(deck_hash: str, timestamp: Optional[int] = None) -> None:
    with DeckManager() as decks:
        details = decks.get_by_hash(deck_hash)

        if details:
            details["last_stats_timestamp"] = (
                int(datetime.now(timezone.utc).timestamp())
                if timestamp is None
                else timestamp
            )


def on_stats_upload_done(done, deck_hash: Optional[str] = None) -> None:
    mw.progress.finish()

    # Only move the watermark once the server has the data, otherwise the
    # reviews that were just collected would never be sent.
    if deck_hash is not None and done is not None:
        update_stats_timestamp(deck_hash, done)
