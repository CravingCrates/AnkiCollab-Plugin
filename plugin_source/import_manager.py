from collections import defaultdict
import json
import logging
from typing import Sequence

import requests
from datetime import datetime, timedelta, timezone

import aqt
import aqt.utils
from aqt.operations import QueryOp
from anki.errors import NotFoundError
from anki.notes import NoteId

from aqt.qt import *
from aqt import mw
from anki.decks import DeckId

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


def do_nothing(count: int):
    pass

def update_optional_tag_config(given_deck_hash, optional_tags):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["optional_tags"] = optional_tags


def get_optional_tags(given_deck_hash) -> dict:
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details is None:
        return {}

    return details.get("optional_tags", {})


def check_optional_tag_changes(deck_hash, optional_tags):
    sorted_old = sorted(get_optional_tags(deck_hash).keys())
    sorted_new = sorted(optional_tags)
    return sorted_old != sorted_new


def update_timestamp(given_deck_hash):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def update_deck_stats_enabled(given_deck_hash, stats_enabled):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["stats_enabled"] = stats_enabled
            if not stats_enabled:
                details["last_stats_timestamp"] = 0  # Reset last stats timestamp if stats are disabled

def get_noteids_from_uuids(guids):
    """Get note IDs from GUIDs using prepared statements for better performance."""
    if not mw.col or not guids:
        return []
    
    noteids = []
    try:
        # Process in batches to avoid memory issues with large GUID lists
        batch_size = 1000  # Process 1000 GUIDs at a time
        
        for i in range(0, len(guids), batch_size):
            batch = guids[i:i + batch_size]
            
            # Use prepared statement with IN clause for batch processing
            placeholders = ','.join(['?' for _ in batch])
            query = f"SELECT id FROM notes WHERE guid IN ({placeholders})"
            
            # Pass parameters using *batch to unpack the list
            batch_results = mw.col.db.list(query, *batch)
            noteids.extend(batch_results)
            
    except Exception as e:
        logger.error(f"Error getting note IDs from GUIDs using prepared statements: {e}")
        # Fallback to individual queries if batch processing fails
        for guid in guids:
            try:
                query = "SELECT id FROM notes WHERE guid = ?"
                note_id = mw.col.db.scalar(query, guid)
                if note_id:
                    noteids.append(note_id)
            except Exception as e2:
                logger.error(f"Error getting note ID for GUID {guid}: {e2}")
                continue
    
    return noteids

def delete_notes(nids):
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

def get_guids_from_noteids(nids):
    """Get GUIDs from note IDs using prepared statements for better performance."""
    if not mw.col or not nids:
        return []
    
    guids = []
    try:
        # Process in batches to avoid memory issues with large note ID lists
        batch_size = 1000  # Process 1000 note IDs at a time
        
        for i in range(0, len(nids), batch_size):
            batch = nids[i:i + batch_size]
            
            # Use prepared statement with IN clause for batch processing
            placeholders = ','.join(['?' for _ in batch])
            query = f"SELECT guid FROM notes WHERE id IN ({placeholders})"
            
            # Pass parameters using *batch to unpack the list
            batch_results = mw.col.db.list(query, *batch)
            guids.extend(batch_results)
            
    except Exception as e:
        logger.error(f"Error getting GUIDs from note IDs using prepared statements: {e}")
        # Fallback to individual queries if batch processing fails
        for nid in nids:
            try:
                query = "SELECT guid FROM notes WHERE id = ?"
                guid = mw.col.db.scalar(query, nid)
                if guid:
                    guids.append(guid)
            except Exception as e2:
                logger.error(f"Error getting GUID for note ID {nid}: {e2}")
                continue
    
    return guids


def open_browser_with_nids(nids):
    if not nids:
        return
    browser = aqt.dialogs.open("Browser", aqt.mw)
    browser.form.searchEdit.lineEdit().setText(
        "nid:" + " or nid:".join(str(nid) for nid in nids)
    )
    browser.onSearchActivated()

def update_stats() -> None:
    """Update stats for decks where stats sharing is already enabled."""
    decks = DeckManager()

    for deck_hash, details in decks:
        if details.get("stats_enabled", False):
            # Only upload stats if the user has already agreed to share them
            share_stats = details.get("share_stats", False)
            if share_stats:
                last_stats_timestamp = details.get("last_stats_timestamp", 0)
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


def wants_to_share_stats(deck_hash) -> (bool, int):
    """Get stats sharing preference without showing dialog."""
    with DeckManager() as decks:
        details = decks.get_by_hash(deck_hash)
        if details is None:
            return False, 0

        last_stats_timestamp = details.get("last_stats_timestamp", 0)
        stats_enabled = details.get("share_stats", False)
        return stats_enabled, last_stats_timestamp


def _install_deck_op(deck, config, map_cache=None, note_type_data=None):
    """Background operation to install deck updates."""
    assert mw.col is not None, "Collection must be available for deck installation"
        
    logger.info("Saving metadata.")
    deck.save_metadata(mw.col, config.home_deck)
    
    logger.info("Saving decks and notes.")
    # Create media result structure
    med_res = {
        "success": True, 
        "message": f"Unknown Media download error",
        "downloaded": 0,
        "skipped": 0
    }
    
    total_notes = deck.calculate_total_work()
    logger.info(f"Total notes: {total_notes}")
    progress_tracker = deck.create_unified_progress_tracker(total_notes)
    logger.info("Workload calculated, starting bulk save.")
    return deck.save_decks_and_notes_bulk(
        collection=mw.col,
        progress_tracker=progress_tracker,  # Use unified tracker
        import_config=config
    )
    
def _on_deck_installed(install_result, deck, subscription, input_hash=None, update_timestamp_after=False):
    """Success callback after deck installation."""
    # Runs on Main Thread
    
    deck.on_success_wrapper(install_result)
    
    deleted_notes = subscription.get("deleted_notes", [])
    deck_name = deck.anki_dict["name"]
    deck_hash = subscription["deck_hash"]
    
    # unfortunately, the db scalar shits itself when called from the success callback after the deck has been installed
    if deleted_notes:
        print(f"Processing {len(deleted_notes)} deleted notes...")
        # Handle deleted Notes
        deleted_nids = get_noteids_from_uuids(subscription["deleted_notes"])
        print(f"Found {len(deleted_nids)} note IDs for deleted notes.")
        if deleted_nids:
            del_notes_dialog = DeletedNotesDialog(deleted_nids, deck_hash)
            del_notes_choice = del_notes_dialog.exec()

            if del_notes_choice == QDialog.DialogCode.Accepted:
                delete_notes(deleted_nids)
            elif del_notes_choice == QDialog.DialogCode.Rejected:
                open_browser_with_nids(deleted_nids)
            
    # Handle new deck registration if input_hash is provided
    if input_hash:
        with DeckManager() as decks:
            details = decks.get_by_hash(input_hash)
            if details and details["deckId"] == 0 and mw.col:  # should only be the case once when they add a new subscription and never ambiguous
                details["deckId"] = aqt.mw.col.decks.id(deck_name)
                # large decks use cached data that may be a day old, so we need to update the timestamp to force a refresh
                details["timestamp"] = (
                        datetime.now(timezone.utc) - timedelta(days=1)
                ).strftime("%Y-%m-%d %H:%M:%S")
                details["stats_enabled"] = subscription["stats_enabled"]
    else:
        # Only ask for a rating if they are updating a deck and not adding a new deck to avoid spam popups
        ask_for_rating()

    # Update timestamp if requested (for changelog updates)
    if update_timestamp_after:
        update_timestamp(subscription["deck_hash"])

    # Now handle stats sharing dialog AFTER import is complete
    deck_hash = subscription["deck_hash"]
    if subscription["stats_enabled"]:
        _handle_stats_sharing_after_import(deck_hash, deck_name)

    mw.reset()  # Reset the main window to reflect changes
    
    return deck_name

def _handle_stats_sharing_after_import(deck_hash, deck_name=None):
    """Handle stats sharing dialog after import is complete."""
    try:
        with DeckManager() as decks:
            details = decks.get_by_hash(deck_hash)
            if details is None:
                return
            
            stats_enabled = details.get("share_stats")
            if stats_enabled is None:
                # Only show dialog if not already decided
                if deck_name:
                    dialog = AskShareStatsDialog(deck_name)
                    choice = dialog.exec()
                    if choice == QDialog.DialogCode.Accepted:
                        stats_enabled = True
                    else:
                        stats_enabled = False
                    
                    if dialog.isChecked():
                        details["share_stats"] = stats_enabled
    except Exception as e:
        logger.error(f"Error handling stats sharing dialog: {e}")
        # Don't let this error break the import process

def install_update(subscription, input_hash=None, update_timestamp_after=False):
    print(f"Installing update for deck: {subscription['deck_hash']}")
    deck_hash = subscription["deck_hash"]
    parent_widget = QApplication.focusWidget() or mw
    
    if check_optional_tag_changes(
            deck_hash, subscription["optional_tags"]
    ):
        dialog = OptionalTagsDialog(
            get_optional_tags(deck_hash), subscription["optional_tags"]
        )
        dialog.exec()
        update_optional_tag_config(
            deck_hash, dialog.get_selected_tags()
        )
    subscribed_tags = get_optional_tags(deck_hash)
    print("Optional Tags done.")
    deck = deck_initializer.from_json(subscription["deck"])
    print("Deck initialized.")
    config = prep_config(
        subscription["protected_fields"],
        [tag for tag, value in subscribed_tags.items() if value],
        True if subscription["optional_tags"] else False,
        deck_hash
    )
    print("Config prepared.")

    map_cache = defaultdict(dict)
    note_type_data = {}
    #deck.handle_notetype_changes(mw.col, map_cache, note_type_data)
    print("Handled note type changes.")
    
    # Start QueryOp for collection operations
    op = QueryOp(
        parent=parent_widget,
        op=lambda col: _install_deck_op(deck, config, map_cache, note_type_data),
        success=lambda res: _on_deck_installed(
            res, deck, subscription, input_hash, update_timestamp_after
        )
    )
    op.with_progress("Installing/Updating deck...")
    op.run_in_background()
        
    # Return the deck name (note: this will be returned before the operation completes)
    return deck.anki_dict["name"]


def abort_update(deck_hash):
    update_timestamp(deck_hash)


def postpone_update():
    pass


def prep_config(protected_fields, optional_tags, has_optional_tags, deck_hash):
    home_deck = get_home_deck(deck_hash)
    new_notes_home_deck = get_new_notes_home_deck(deck_hash)
    config = ImportConfig(
        add_tag_to_cards=[],
        optional_tags=optional_tags,
        has_optional_tags=has_optional_tags,
        use_notes=True,
        use_media=False,
        ignore_deck_movement=get_deck_movement_status(),
        suspend_new_cards=get_card_suspension_status(),
        home_deck=home_deck or "",  # Provide empty string as default
        new_notes_home_deck=new_notes_home_deck or "",  # Provide empty string as default
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

    update_deck_stats_enabled(deck_hash, subscription["stats_enabled"])
    
    if changelog:
        dialog = ChangelogDialog(changelog, deck_hash)
        choice = dialog.exec()

        if choice == QDialog.DialogCode.Accepted:
            install_update(subscription, update_timestamp_after=True)
        elif choice == QDialog.DialogCode.Rejected:
            postpone_update()
        else:
            abort_update(deck_hash)
    else:  # Skip changelog window if there is no message for the user
        install_update(subscription, update_timestamp_after=True)


def ask_for_rating():
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


def import_webresult(data):
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
            install_update(subscription, input_hash)
        else:  # Update deck
            show_changelog_popup(subscription)

    update_stats()


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


def get_home_deck(given_deck_hash):
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details and details["deckId"] != 0 and mw.col:
        return mw.col.decks.name_if_exists(details["deckId"])


def get_new_notes_home_deck(given_deck_hash):
    """Get the home deck for new notes (notes not in user's collection)"""
    try:
        decks = DeckManager()
        details = decks.get_by_hash(given_deck_hash)

        if details and mw.col:
            # Check if new_notes_home_deck is set
            new_notes_deck_id = details.get("new_notes_home_deck", None)
            if new_notes_deck_id and new_notes_deck_id != 0:
                deck_name = mw.col.decks.name_if_exists(new_notes_deck_id)
                if deck_name:
                    return deck_name
            
            # Fall back to regular home deck
            return get_home_deck(given_deck_hash)
    except Exception as e:
        logger.error(f"Error getting new notes home deck: {e}")
        # Fall back to regular home deck
        return get_home_deck(given_deck_hash)
    
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
def async_start_pull(input_hash, silent=False):
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
            webresult = json.loads(decompressed_data.decode("utf-8"))
            return (webresult, input_hash, silent)
        else:
            infot = "A Server Error occurred. Please notify us!"
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
            )
            return (None, None, silent)
        
def handle_pull(input_hash, silent=False):
    QueryOp(
        parent=mw,
        op=lambda _: async_start_pull(input_hash, silent),
        success=import_webresult,
    ).with_progress("Fetching Changes from AnkiCollab...").run_in_background()
    
