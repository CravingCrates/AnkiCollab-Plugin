from collections import defaultdict
import json
from typing import List, Sequence, Tuple, Optional, Any

import msgspec
import requests
from datetime import datetime, timedelta, timezone

import aqt
import aqt.utils
from anki.notes import NoteId
from aqt.operations import QueryOp

from aqt.qt import *
from aqt import mw

from .models import UpdateInfoResponse
from .var_defs import API_BASE_URL

from .dialogs import ChangelogDialog, DeletedNotesDialog, OptionalTagsDialog, AskShareStatsDialog, RateAddonDialog

from .crowd_anki.representation import deck_initializer
from .crowd_anki.importer.import_dialog import ImportConfig

from .utils import create_backup, get_local_deck_from_id, DeckManager

from .stats import ReviewHistory, on_stats_upload_done, update_stats_timestamp

import base64
import gzip

import logging

logger = logging.getLogger("ankicollab")


def update_optional_tag_config(given_deck_hash: str, optional_tags: dict[Any, Any]):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["optional_tags"] = optional_tags


def get_optional_tags(given_deck_hash: str) -> dict[Any, Any]:
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details is None:
        return {}

    return details.get("optional_tags", {})


def check_optional_tag_changes(deck_hash: str, optional_tags: List[str]) -> bool:
    sorted_old = sorted(get_optional_tags(deck_hash).keys())
    sorted_new = sorted(optional_tags)
    return sorted_old != sorted_new


def update_timestamp(given_deck_hash: str) -> None:
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def update_deck_stats_enabled(given_deck_hash: str, stats_enabled: bool) -> None:
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["stats_enabled"] = stats_enabled
            if not stats_enabled:
                details["last_stats_timestamp"] = 0  # Reset last stats timestamp if stats are disabled

def get_noteids_from_uuids(guids) -> Sequence[NoteId]:
    noteids = []
    for guid in guids:
        query = "select id from notes where guid=?"
        note_id = aqt.mw.col.db.scalar(query, guid)
        if note_id:
            noteids.append(note_id)
    return noteids


def get_guids_from_noteids(nids: Sequence[NoteId]) -> List[str]:
    guids = []
    for nid in nids:
        query = "select guid from notes where id=?"
        guid = aqt.mw.col.db.scalar(query, nid)
        if guid:
            guids.append(guid)
    return guids


def open_browser_with_nids(nids: Sequence[NoteId]) -> None:
    if not nids:
        return
    browser = aqt.dialogs.open("Browser", aqt.mw)
    browser.form.searchEdit.lineEdit().setText(
        "nid:" + " or nid:".join(str(nid) for nid in nids)
    )
    browser.onSearchActivated()


def delete_notes(nids: Sequence[NoteId]) -> None:
    if not nids:
        return
    aqt.mw.col.remove_notes(nids)
    aqt.mw.col.reset()  # deprecated
    mw.reset()
    aqt.mw.taskman.run_on_main(
        lambda: aqt.utils.tooltip(
            "Deleted %d notes." % len(nids), parent=QApplication.focusWidget()
        )
    )


def update_stats() -> None:
    decks = DeckManager()

    for deck_hash, details in decks:
        if details.get("stats_enabled", False):
            # Only upload stats if the user wants to share them
            (share_data, last_stats_timestamp) = wants_to_share_stats(deck_hash)
            if share_data:
                rh = ReviewHistory(deck_hash)
                op = QueryOp(
                    parent=mw,
                    op=lambda _: rh.upload_review_history(last_stats_timestamp),
                    success=on_stats_upload_done
                )
                op.with_progress(
                    "Uploading Review History..."
                ).run_in_background()
                update_stats_timestamp(deck_hash)


def wants_to_share_stats(deck_hash: str) -> Tuple[bool, int]:
    with DeckManager() as decks:
        details = decks.get_by_hash(deck_hash)
        if details is None:
            raise Exception

        last_stats_timestamp = details.get("last_stats_timestamp", 0)
        stats_enabled = details.get("share_stats")

        if stats_enabled is None:
            deck = get_local_deck_from_id(details["deckId"])
            dialog = AskShareStatsDialog(deck)
            choice = dialog.exec()
            if choice == QDialog.DialogCode.Accepted:
                stats_enabled = True
            else:
                stats_enabled = False
            if dialog.isChecked():
                details["share_stats"] = stats_enabled

        return stats_enabled, last_stats_timestamp


def install_update(subscription: UpdateInfoResponse):
    deck_hash = subscription.deck_hash

    if check_optional_tag_changes(
            deck_hash, subscription.optional_tags
    ):
        dialog = OptionalTagsDialog(
            get_optional_tags(deck_hash), subscription.optional_tags
        )
        dialog.exec()
        update_optional_tag_config(
            deck_hash, dialog.get_selected_tags()
        )
    subscribed_tags = get_optional_tags(deck_hash)

    deck = deck_initializer.from_json(subscription.deck)
    config = prep_config(
        subscription.protected_fields,
        [tag for tag, value in subscribed_tags.items() if value],
        True if subscription.optional_tags else False,
        deck_hash
    )
    config.home_deck = get_home_deck(deck_hash)

    map_cache = defaultdict(dict)
    note_type_data = {}
    deck.handle_notetype_changes(aqt.mw.col, map_cache, note_type_data)
    deck.save_to_collection(aqt.mw.col, map_cache, note_type_data, import_config=config)

    # Handle deleted Notes
    deleted_nids = get_noteids_from_uuids(subscription.deleted_notes)
    if deleted_nids:
        del_notes_dialog = DeletedNotesDialog(deleted_nids, subscription.deck_hash)
        del_notes_choice = del_notes_dialog.exec()

        if del_notes_choice == QDialog.DialogCode.Accepted:
            delete_notes(deleted_nids)
        elif del_notes_choice == QDialog.DialogCode.Rejected:
            open_browser_with_nids(deleted_nids)

    return deck.anki_dict["name"]


def abort_update(deck_hash: str) -> None:
    update_timestamp(deck_hash)


def prep_config(protected_fields, optional_tags, has_optional_tags: bool, deck_hash: str):
    config = ImportConfig(
        add_tag_to_cards=[],
        optional_tags=optional_tags,
        has_optional_tags=has_optional_tags,
        use_notes=True,
        use_media=False,
        ignore_deck_movement=get_deck_movement_status(),
        suspend_new_cards=get_card_suspension_status(),
        home_deck=None,
        deck_hash=deck_hash,
    )
    for protected_field in protected_fields:
        model_name = protected_field["name"]
        for field in protected_field["fields"]:
            field_name = field["name"]
            config.add_field(model_name, field_name)

    return config


def show_changelog_popup(subscription: UpdateInfoResponse) -> None:
    deck_hash = subscription.deck_hash

    if subscription.changelog:
        dialog = ChangelogDialog(subscription.changelog, deck_hash)
        choice = dialog.exec()

        if choice == QDialog.DialogCode.Accepted:
            install_update(subscription)
            update_timestamp(deck_hash)
        elif choice == QDialog.DialogCode.Rejected:
            pass
        else:
            abort_update(deck_hash)
    else:  # Skip changelog window if there is no message for the user
        install_update(subscription)
        update_timestamp(deck_hash)


def ask_for_rating() -> None:
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and "settings" in strings_data:
        if "pull_counter" in strings_data["settings"]:
            pull_counter = strings_data["settings"]["pull_counter"]
            strings_data["settings"]["pull_counter"] = pull_counter + 1
            if pull_counter % 30 == 0:  # every 30 pulls
                last_ratepls = strings_data["settings"]["last_ratepls"]
                last_ratepls_dt = datetime.strptime(last_ratepls, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_ratepls_dt).days > 30:
                    if not strings_data["settings"]["rated_addon"]:  # only ask if they haven't rated the addon yet
                        strings_data["settings"]["last_ratepls"] = datetime.now(timezone.utc).strftime(
                            '%Y-%m-%d %H:%M:%S')
                        dialog = RateAddonDialog()
                        dialog.exec()
            mw.addonManager.writeConfig(__name__, strings_data)


def import_webresult(data: Tuple[Optional[List[UpdateInfoResponse]], Optional[str], bool]) -> None:
    (webresult, input_hash, silent) = data # gotta unpack the tuple

    # if webresult is empty, tell user that there are no updates
    if not webresult:
        if silent:
            aqt.utils.tooltip("You're already up-to-date!", parent=mw)
        else:
            msg_box = QMessageBox()
            msg_box.setWindowTitle("AnkiCollab")
            msg_box.setText("You're already up-to-date!")
            msg_box.exec()

        update_stats()
        return

    # Create backup before doing anything
    create_backup(background=True)  # run in background

    for subscription in webresult:
        if input_hash:  # New deck
            deck_name = install_update(subscription)

            with DeckManager() as decks:
                details = decks.get_by_hash(input_hash)

                if details is not None and details["deckId"] == 0:  # should only be the case once when they add a new subscription and never ambiguous
                    details["deckId"] = aqt.mw.col.decks.id(deck_name)
                    # large decks use cached data that may be a day old, so we need to update the timestamp to force a refresh
                    details["timestamp"] = (
                            datetime.now(timezone.utc) - timedelta(days=1)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    details["stats_enabled"] = subscription.stats_enabled

        else:  # Update deck
            show_changelog_popup(subscription)

    if not input_hash:  # Only ask for a rating if they are updating a deck and not adding a new deck to avoid spam popups
        ask_for_rating()
        _info = "AnkiCollab: Updated deck(s) successfully!"
        aqt.mw.taskman.run_on_main(
            lambda: aqt.utils.tooltip(_info, parent=mw)
        )
    update_stats()


def get_card_suspension_status() -> bool:
    strings_data = mw.addonManager.getConfig(__name__)
    val = False
    if strings_data is not None and strings_data["settings"] is not None:
        val = bool(strings_data["settings"]["suspend_new_cards"])
    return val


def get_deck_movement_status() -> bool:
    strings_data = mw.addonManager.getConfig(__name__)
    val = True
    if strings_data is not None and strings_data["settings"] is not None:
        val = bool(strings_data["settings"]["auto_move_cards"])
    return val


def get_home_deck(given_deck_hash) -> Optional[str]:
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details and details["deckId"] != 0:
        return mw.col.decks.name_if_exists(details["deckId"])


def remove_nonexistent_decks() -> None:
    strings_data = mw.addonManager.getConfig(__name__)

    if strings_data is not None and len(strings_data) > 0:
        # Create a copy of the config data
        strings_data_copy = strings_data.copy()

        # backwards compatibility
        if "settings" in strings_data_copy:
            del strings_data_copy["settings"]

        if "auth" in strings_data_copy:
            del strings_data_copy["auth"]

        # Only include the specific hash if it exists
        strings_data_to_send = strings_data_copy

        payload = {"deck_hashes": list(strings_data_to_send.keys())}
        response = requests.post(f"{API_BASE_URL}/CheckDeckAlive", json=payload)
        if response.status_code == 200:
            if response.content == "Error":
                infot = "A Server Error occurred. Please notify us!"
                aqt.mw.taskman.run_on_main(
                    lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
                )
            else:
                webresult = json.loads(response.content)
                # we need to remove all the decks that don't exist anymore from the strings_data
                strings_data = mw.addonManager.getConfig(__name__)
                if strings_data is not None and len(strings_data) > 0:
                    for deck_hash in webresult:
                        if deck_hash in strings_data:
                            del strings_data[deck_hash]
                        else:
                            print("deck_hash not found in strings_data")
                    mw.addonManager.writeConfig(__name__, strings_data)
                else:
                    print("strings_data is None or empty")

# Kinda ugly, but for backwards compatibility we need to handle both the old and new format
def async_start_pull(input_hash: str, silent: bool = False) \
        -> Tuple[Optional[List[UpdateInfoResponse]], Optional[str], bool]:

    remove_nonexistent_decks()
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and len(strings_data) > 0:
        # Create a copy of the config data
        strings_data_copy = strings_data.copy()

        # backwards compatibility
        if "settings" in strings_data_copy:
            del strings_data_copy["settings"]

        if "auth" in strings_data_copy:
            del strings_data_copy["auth"]

        # Create the data to send based on whether input_hash is provided
        if input_hash is None:
            strings_data_to_send = strings_data_copy
        else:
            # Only include the specific hash if it exists
            strings_data_to_send = (
                {input_hash: strings_data_copy[input_hash]}
                if input_hash in strings_data_copy
                else {}
            )

        response = requests.post(
            f"{API_BASE_URL}/pullChanges", json=strings_data_to_send
        )
        if response.status_code == 200:
            compressed_data = base64.b64decode(response.content)
            decompressed_data = gzip.decompress(compressed_data)

            webresult = msgspec.json.decode(decompressed_data.decode("utf-8"), type=List[UpdateInfoResponse])
            return webresult, input_hash, silent
        else:
            infot = "A Server Error occurred. Please notify us!"
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
            )
            return None, None, silent

def handle_pull(input_hash: str, silent: bool =False) -> None:
    QueryOp(
        parent=mw,
        op=lambda _: async_start_pull(input_hash, silent),
        success=import_webresult,
    ).with_progress("Fetching Changes from AnkiCollab...").run_in_background()

