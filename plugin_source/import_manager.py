from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
import json
import os
import webbrowser
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import Future

from pprint import pp
from typing import List

import aqt
import aqt.utils
from aqt.operations import QueryOp
import anki
from anki.utils import point_version
from aqt.qt import *
from aqt import mw

from .var_defs import API_BASE_URL

from .dialogs import ChangelogDialog, DeletedNotesDialog, OptionalTagsDialog, AskShareStatsDialog, RateAddonDialog

from .crowd_anki.anki.adapters.note_model_file_provider import NoteModelFileProvider
from .crowd_anki.representation.note import Note
from .crowd_anki.config.config_settings import ConfigSettings
from .crowd_anki.export.note_sorter import NoteSorter
from .crowd_anki.utils.disambiguate_uuids import disambiguate_note_model_uuids

from .crowd_anki.representation import *
from .crowd_anki.representation import deck_initializer
from .crowd_anki.anki.adapters.anki_deck import AnkiDeck
from .crowd_anki.representation.deck import Deck
from .crowd_anki.importer.import_dialog import ImportConfig

from .utils import get_deck_hash_from_did

from .stats import ReviewHistory

from . import main

import base64
import gzip

from .thread import run_function_in_thread, run_async_function_in_thread, sync_run_async
import logging
logger = logging.getLogger("ankicollab")

def do_nothing(count: int):
    pass

def on_stats_upload_done(data) -> None:
    mw.progress.finish()
    #aqt.utils.tooltip("Review History upload done. Thanks for sharing!", parent=QApplication.focusWidget())
     
def update_optional_tag_config(deck_hash, optional_tags):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == deck_hash:
                details["optional_tags"] = optional_tags
    mw.addonManager.writeConfig(__name__, strings_data)


def get_optional_tags(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for hash, details in strings_data.items():
            if hash == deck_hash:
                if "optional_tags" not in details:
                    return {}
                return details["optional_tags"]
    return {}

def check_optional_tag_changes(deck_hash, optional_tags):
    sorted_old = sorted(get_optional_tags(deck_hash).keys())
    sorted_new = sorted(optional_tags)
    return sorted_old != sorted_new


def update_timestamp(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for sub, details in strings_data.items():
            if sub == deck_hash:
                details["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                break
        mw.addonManager.writeConfig(__name__, strings_data)


def get_noteids_from_uuids(guids):
    noteids = []
    for guid in guids:
        query = "select id from notes where guid=?"
        note_id = aqt.mw.col.db.scalar(query, guid)
        if note_id:
            noteids.append(note_id)
    return noteids


def get_guids_from_noteids(nids):
    guids = []
    for nid in nids:
        query = "select guid from notes where id=?"
        guid = aqt.mw.col.db.scalar(query, nid)
        if guid:
            guids.append(guid)
    return guids


def open_browser_with_nids(nids):
    if not nids:
        return
    browser = aqt.dialogs.open("Browser", aqt.mw)
    browser.form.searchEdit.lineEdit().setText(
        "nid:" + " or nid:".join(str(nid) for nid in nids)
    )
    browser.onSearchActivated()

def delete_notes(nids):
    if not nids:
        return
    aqt.mw.col.remove_notes(nids)
    aqt.mw.col.reset() # deprecated
    mw.reset()
    aqt.mw.taskman.run_on_main(
        lambda: aqt.utils.tooltip(
            "Deleted %d notes." % len(nids), parent=QApplication.focusWidget()
        )
    )
    
def update_stats_timestamp(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data:
        for sub, details in strings_data.items():
            if sub == deck_hash:
                details["last_stats_timestamp"] = int(datetime.now(timezone.utc).timestamp())
                break
        mw.addonManager.writeConfig(__name__, strings_data)
        
def wants_to_share_stats(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    stats_enabled = False
    last_stats_timestamp = 0
    if strings_data:
        for sub, details in strings_data.items():
            if sub == deck_hash:
                if "last_stats_timestamp" in details:
                    last_stats_timestamp = details["last_stats_timestamp"]                
                if "share_stats" in details:
                    stats_enabled = details["share_stats"]
                else:
                    dialog = AskShareStatsDialog()
                    choice = dialog.exec()
                    if choice == QDialog.DialogCode.Accepted:
                        stats_enabled = True
                    else:
                        stats_enabled = False
                    if dialog.isChecked():
                        details["share_stats"] = stats_enabled                    
                        mw.addonManager.writeConfig(__name__, strings_data)
                break
    return (stats_enabled, last_stats_timestamp)

def install_update(subscription, is_new = False):
    deckHash = subscription["deck_hash"]
    if check_optional_tag_changes(
        deckHash, subscription["optional_tags"]
    ):
        dialog = OptionalTagsDialog(
            get_optional_tags(deckHash), subscription["optional_tags"]
        )
        dialog.exec()
        update_optional_tag_config(
            deckHash, dialog.get_selected_tags()
        )
    subscribed_tags = get_optional_tags(deckHash)

    stats_enabled = subscription["stats_enabled"]

    deck = deck_initializer.from_json(subscription["deck"])
    config = prep_config(
        subscription["protected_fields"],
        [tag for tag, value in subscribed_tags.items() if value],
        True if subscription["optional_tags"] else False,
        deckHash
    )
    config.home_deck = get_home_deck(deckHash)
        
    map_cache = defaultdict(dict)
    note_type_data = {}
    deck.handle_notetype_changes(aqt.mw.col, map_cache, note_type_data)
    deck.save_to_collection(aqt.mw.col, map_cache, note_type_data, import_config=config)
    
    # Handle deleted Notes
    deleted_nids = get_noteids_from_uuids(subscription["deleted_notes"])
    if deleted_nids:
        del_notes_dialog = DeletedNotesDialog(deleted_nids, deckHash)
        del_notes_choice = del_notes_dialog.exec()

        if del_notes_choice == QDialog.DialogCode.Accepted:
            delete_notes(deleted_nids)
        elif del_notes_choice == QDialog.DialogCode.Rejected:
            open_browser_with_nids(deleted_nids)
    # Upload stats if the maintainer wants them, don't bother for new decks
    if stats_enabled and not is_new:
        # Only upload stats if the user wants to share them
        (share_data, last_stats_timestamp) = wants_to_share_stats(deckHash)
        if share_data:
            rh = ReviewHistory(deckHash)
            op = QueryOp(
                parent=mw,
                op=lambda _: rh.upload_review_history(last_stats_timestamp),
                success=on_stats_upload_done
            )
            op.with_progress(
                "Uploading Review History..."
            ).run_in_background()
            update_stats_timestamp(deckHash)
        
    return deck.anki_dict["name"]


def abort_update(deck_hash):
    update_timestamp(deck_hash)


def postpone_update():
    pass


def prep_config(protected_fields, optional_tags, has_optional_tags, deck_hash):
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


def show_changelog_popup(subscription):
    changelog = subscription["changelog"]
    deck_hash = subscription["deck_hash"]

    if changelog:
        dialog = ChangelogDialog(changelog, deck_hash)
        choice = dialog.exec()

        if choice == QDialog.DialogCode.Accepted:
            install_update(subscription)
            update_timestamp(deck_hash)
        elif choice == QDialog.DialogCode.Rejected:
            postpone_update()
        else:
            abort_update(deck_hash)
    else: # Skip changelog window if there is no message for the user
        install_update(subscription)
        update_timestamp(deck_hash)

def ask_for_rating():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and "settings" in strings_data:
        if "pull_counter" in strings_data["settings"]:
            pull_counter = strings_data["settings"]["pull_counter"]
            strings_data["settings"]["pull_counter"] = pull_counter + 1
            if pull_counter % 30 == 0: # every 30 pulls
                last_ratepls = strings_data["settings"]["last_ratepls"]                
                last_ratepls_dt = datetime.strptime(last_ratepls, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_ratepls_dt).days > 30:
                    if not strings_data["settings"]["rated_addon"]: # only ask if they haven't rated the addon yet
                        strings_data["settings"]["last_ratepls"] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                        dialog = RateAddonDialog()
                        dialog.exec()
            mw.addonManager.writeConfig(__name__, strings_data)


def import_webresult(webresult, input_hash, silent=False):
    # if webresult is empty, tell user that there are no updates
    if not webresult:
        if silent:
            aqt.utils.tooltip("You're already up-to-date!", parent=mw)
        else:
            msg_box = QMessageBox()
            msg_box.setWindowTitle("AnkiCollab")
            msg_box.setText("You're already up-to-date!")
            msg_box.exec()
        return

    # Create a backup for the user before updating!
    QueryOp(
            parent=mw,
            op=lambda _: aqt.mw.create_backup_now(),
            success=lambda _: 1,
        ).with_progress().run_in_background()

    for subscription in webresult:
        if input_hash:  # New deck
            deck_name = install_update(subscription, True)
            strings_data = mw.addonManager.getConfig(__name__)
            for hash, details in strings_data.items():
                if (
                    hash == input_hash and details["deckId"] == 0
                ):  # should only be the case once when they add a new subscription and never ambiguous
                    details["deckId"] = aqt.mw.col.decks.id(deck_name)
                    # large decks use cached data that may be a day old, so we need to update the timestamp to force a refresh
                    details["timestamp"] = (
                        datetime.now(timezone.utc) - timedelta(days=1)
                    ).strftime("%Y-%m-%d %H:%M:%S")

            mw.addonManager.writeConfig(__name__, strings_data)
        else:  # Update deck
            show_changelog_popup(subscription)
    
    if not input_hash: # Only ask for a rating if they are updating a deck and not adding a new deck to avoid spam popups
        ask_for_rating()
        infot = "AnkiCollab: Updated deck(s) successfully!"
        aqt.mw.taskman.run_on_main(
            lambda: aqt.utils.tooltip(infot, parent=mw)
        )

def get_card_suspension_status():
    strings_data = mw.addonManager.getConfig(__name__)
    val = False
    if strings_data is not None and strings_data["settings"] is not None:
        val = bool(strings_data["settings"]["suspend_new_cards"])
    return val

def get_deck_movement_status():
    strings_data = mw.addonManager.getConfig(__name__)
    val = True
    if strings_data is not None and strings_data["settings"] is not None:
        val = bool(strings_data["settings"]["auto_move_cards"])
    return val

def get_home_deck(deck_hash):
    strings_data = mw.addonManager.getConfig(__name__)
    for hash, details in strings_data.items():
        if (hash == deck_hash and details["deckId"] != 0):  # Local Deck is set
            return mw.col.decks.name_if_exists(details["deckId"])
    return None

def remove_nonexistent_decks():
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
def handle_pull(input_hash, silent=False):
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
            webresult = json.loads(decompressed_data.decode("utf-8"))
            aqt.mw.taskman.run_on_main(lambda: import_webresult(webresult, input_hash, silent))
        else:
            infot = "A Server Error occurred. Please notify us!"
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
            )