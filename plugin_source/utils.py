from __future__ import annotations
import datetime
from datetime import datetime
import aqt
import aqt.utils
from anki.errors import NotFoundError
from aqt.operations import QueryOp
from anki.collection import Collection
from aqt import mw
import aqt.utils
from contextlib import AbstractContextManager
import logging


class CollectionUnavailableError(Exception):
    """Raised when the Anki collection is not available."""
    pass


class OperationAbortedError(Exception):
    """Raised when an operation is aborted due to collection becoming unavailable.
    
    This exception is used to gracefully abort long-running import/export operations
    when the collection is closed mid-operation. It signals that the operation should
    stop immediately but data integrity is preserved.
    """
    def __init__(self, message: str = "Operation aborted", phase: str = "unknown"):
        self.phase = phase
        super().__init__(f"{message} (during {phase})")


class BackupFailedError(Exception):
    """Raised when a backup operation fails.
    
    This exception indicates that the collection backup could not be created,
    which may indicate a problem with the collection. Operations should abort
    rather than proceed without a backup.
    """
    pass


def ensure_collection() -> Collection:
    """Ensure the Anki collection is available and return it.
    
    Raises:
        CollectionUnavailableError: If mw or mw.col is None.
    
    Returns:
        The Anki collection object.
    """
    if mw is None or mw.col is None:
        raise CollectionUnavailableError(
            "Anki collection is not available. Please ensure Anki is fully loaded "
            "and a profile is open before performing this operation."
        )
    return mw.col


def is_collection_available() -> bool:
    """Check if the Anki collection is currently available.
    
    Returns:
        True if mw and mw.col are both available, False otherwise.
    """
    return mw is not None and mw.col is not None


def check_collection_or_abort(phase: str = "unknown") -> Collection:
    """Check collection availability and abort gracefully if unavailable.
    
    Use this function at key checkpoints during long-running operations to detect
    when the collection has been closed and abort gracefully without data loss.
    
    Args:
        phase: Description of the current operation phase for error reporting.
    
    Raises:
        OperationAbortedError: If the collection is not available.
    
    Returns:
        The Anki collection object if available.
    """
    if mw is None or mw.col is None:
        raise OperationAbortedError(
            "Collection closed during operation - aborting to prevent data loss",
            phase=phase
        )
    return mw.col


class _SentryBreadcrumbHandler(logging.Handler):
    """Emit log records as Sentry breadcrumbs without writing to stdio.

    Avoids Anki/Colorama stream issues and preserves breadcrumbs for our add-on.
    """

    level_map = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warning",
        logging.ERROR: "error",
        logging.CRITICAL: "fatal",
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            try:
                import sentry_sdk  # type: ignore
            except Exception:
                return
            level = self.level_map.get(record.levelno, "info")
            sentry_sdk.add_breadcrumb(
                category=record.name,
                message=self.format(record),
                level=level,
            )
        except Exception:
            # Never fail due to logging
            pass


def get_logger(name: str = "ankicollab") -> logging.Logger:
    """Return a namespaced logger for this add-on.

    Keeps logging consistent and produces good Sentry breadcrumbs.
    """
    logger = logging.getLogger(name)
    # Avoid writing to Anki's stderr; only add our Sentry breadcrumb handler
    logger.handlers = []
    handler = _SentryBreadcrumbHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger
from typing import Dict, Iterator, Optional, Tuple


def get_timestamp(given_deck_hash):
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details is not None:

        date_string = details["timestamp"]
        datetime_obj = datetime.strptime(date_string, '%Y-%m-%d %H:%M:%S')
        unix_timestamp = datetime_obj.timestamp()
        return unix_timestamp


def get_hash_from_local_id(deck_id) -> Optional[str]:
    decks = DeckManager()

    for deck_hash, details in decks:
        if details.get("deckId") == deck_id:
            return deck_hash


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


def get_deck_hash_from_card(card) -> Tuple[Optional[str], Optional[str]]:
    """
    Get the deck hash for a card, handling filtered decks via odid.
    
    Returns:
        Tuple of (deck_hash, error_message).
        If successful, deck_hash is set and error_message is None.
        If failed, deck_hash is None and error_message explains the issue.
    """
    # If card is in a filtered deck, odid contains the original deck id
    if card.odid and card.odid != 0:
        did = card.odid
    else:
        did = card.did
    
    # Check if the resolved deck is itself a filtered deck (edge case: odid=0 in filtered deck)
    deck_obj = mw.col.decks.get(did, default=False)
    if deck_obj and deck_obj.get('dyn', False):
        # Card is in a filtered deck with no valid original deck
        return None, "This card is in a filtered deck with no valid original deck. Cannot suggest changes."
    
    deck_hash = get_deck_hash_from_did(did)
    if deck_hash is None:
        return None, "Cannot find the Cloud Deck for this card. Ensure the parent deck is subscribed."
    
    return deck_hash, None


def get_did_from_hash(given_deck_hash):
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    return details and details.get("deckId")


def get_local_deck_from_hash(input_hash):
    deck_id = get_did_from_hash(input_hash)

    if deck_id is None:
        return "None"

    return mw.col.decks.name(deck_id)


def get_local_deck_from_id(deck_id):
    return mw.col.decks.name(deck_id)


def create_backup(background: bool = False, critical: bool = False) -> bool:
    """Create a backup of the Anki collection.
    
    Args:
        background: If True, run the backup operation in the background (non-blocking).
                   Cannot be used with critical=True.
        critical: If True, the backup is required for the operation to proceed.
                 Raises BackupFailedError if backup fails. Implies background=False.
    
    Returns:
        True if backup succeeded, False if it was skipped or failed (when not critical).
    
    Raises:
        BackupFailedError: If critical=True and the backup fails.
        ValueError: If both background=True and critical=True.
    """
    logger = get_logger("ankicollab.utils")
    
    if background and critical:
        raise ValueError("Cannot use background=True with critical=True")
    
    # Check collection availability before attempting backup
    if not is_collection_available():
        msg = "Cannot create backup: collection not available"
        logger.warning(msg)
        if critical:
            raise BackupFailedError(msg)
        return False
    
    logger.info("Creating backup...")

    def do_backup_sync(col: Collection) -> bool:
        """Synchronous backup that returns success/failure."""
        # Double-check collection is still available
        if col is None:
            logger.warning("Backup aborted: collection became unavailable")
            return False
        try:
            backup_folder = aqt.mw.pm.backupFolder() if aqt.mw and aqt.mw.pm else None
            if backup_folder is None:
                logger.warning("Backup aborted: could not determine backup folder")
                return False
            _ = col.create_backup(
                backup_folder=backup_folder,
                force=True,
                wait_for_completion=True,
            )
            logger.info("Backup created successfully")
            return True
        except Exception as e:
            logger.exception("Error creating backup")
            return False

    if background:
        # Non-blocking background backup (fire and forget)
        if mw is None:
            logger.warning("Skipping background backup: main window not available")
            return False
        
        def do_backup_bg(col: Collection):
            do_backup_sync(col)
        
        QueryOp(
            parent=mw,
            op=do_backup_bg,
            success=lambda _: 1,
        ).run_in_background()
        return True  # Assume success for background (we can't wait)
    else:
        # Synchronous/critical backup
        col = aqt.mw.col if aqt.mw else None
        if col is None:
            msg = "Cannot create backup: collection not available"
            logger.warning(msg)
            if critical:
                raise BackupFailedError(msg)
            return False
        
        success = do_backup_sync(col)
        
        if not success and critical:
            raise BackupFailedError(
                "Failed to create backup. The operation has been aborted to protect your data. "
                "Please check that your collection is not corrupted and try again."
            )
        
        return success


class DeckManager(AbstractContextManager):
    def __init__(self):
        self._raw_data = mw.addonManager.getConfig(__name__) or {}

        self._filtered_items = {
            deck_hash: details
            for deck_hash, details in self._raw_data.items()
            if deck_hash not in ['settings', 'auth']
        }

    def __enter__(self) -> DeckManager:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.save()

    def save(self):
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
