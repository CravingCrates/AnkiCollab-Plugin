"""Local record of which notes exist on AnkiCollab.

Lets the editor show whether a note is already on AnkiCollab without asking
the server about individual notes (issue #98). Each deck's record is filled
once from the server, then kept up to date by every pull, which already
carries the notes that changed and the notes that were deleted.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

from . import config_manager

logger = logging.getLogger(__name__)

DB_FILE_NAME = "collab_notes.sqlite3"
NOTE_GUIDS_ENDPOINT = "/GetDeckNoteGuids"

# "decks" lists the decks whose record is complete. Until a deck is listed, a
# note missing from "notes" may still be on the server, so its status is
# unknown rather than "not on AnkiCollab".
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    guid TEXT PRIMARY KEY,
    deck_hash TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS notes_by_deck ON notes (deck_hash);
CREATE TABLE IF NOT EXISTS decks (
    deck_hash TEXT PRIMARY KEY
);
"""


class NoteStatus(Enum):
    ON_COLLAB = "on_collab"
    NOT_ON_COLLAB = "not_on_collab"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class CollabNoteStore:
    """The record for one profile, kept in that profile's ``user_files``.

    Every call opens its own connection, so the store can be used from the
    main thread and from background tasks alike.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_current_profile(cls) -> Optional[CollabNoteStore]:
        name = config_manager.current_profile_name()
        if name is None:
            return None
        return cls(config_manager.profile_dir(name) / DB_FILE_NAME)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.executescript(_SCHEMA)
        return conn

    def add_notes(self, deck_hash: str, guids: Iterable[str]) -> None:
        self._upsert(deck_hash, guids, deleted=False)

    def mark_deleted(self, deck_hash: str, guids: Iterable[str]) -> None:
        self._upsert(deck_hash, guids, deleted=True)

    def _upsert(self, deck_hash: str, guids: Iterable[str], deleted: bool) -> None:
        rows = [(guid, deck_hash, int(deleted)) for guid in guids if guid]
        if not rows:
            return
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO notes (guid, deck_hash, deleted) VALUES (?, ?, ?) "
                "ON CONFLICT(guid) DO UPDATE SET "
                "deck_hash = excluded.deck_hash, deleted = excluded.deleted",
                rows,
            )

    def mark_complete(self, deck_hash: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO decks (deck_hash) VALUES (?)", (deck_hash,)
            )

    def complete_decks(self) -> Set[str]:
        with closing(self._connect()) as conn:
            return {row[0] for row in conn.execute("SELECT deck_hash FROM decks")}

    def forget_decks_except(self, deck_hashes: Iterable[str]) -> None:
        """Drops the record of every deck that is no longer subscribed."""
        keep = list(deck_hashes)
        placeholders = ",".join("?" for _ in keep)
        condition = f"deck_hash NOT IN ({placeholders})" if keep else "1"
        with closing(self._connect()) as conn, conn:
            conn.execute(f"DELETE FROM notes WHERE {condition}", keep)
            conn.execute(f"DELETE FROM decks WHERE {condition}", keep)

    def status(self, guid: str, deck_hash: str) -> NoteStatus:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT deleted FROM notes WHERE guid = ?", (guid,)
            ).fetchone()
            if row is not None:
                return NoteStatus.DELETED if row[0] else NoteStatus.ON_COLLAB
            complete = conn.execute(
                "SELECT 1 FROM decks WHERE deck_hash = ?", (deck_hash,)
            ).fetchone()
        return NoteStatus.NOT_ON_COLLAB if complete else NoteStatus.UNKNOWN


def _guids_in_deck_json(deck_json: Dict[str, Any]) -> Iterator[str]:
    for note in deck_json.get("notes", []):
        guid = note.get("guid") if isinstance(note, dict) else None
        if guid:
            yield guid
    for child in deck_json.get("children", []):
        if isinstance(child, dict):
            yield from _guids_in_deck_json(child)


def record_pull(
    store: CollabNoteStore,
    deck_hash: str,
    deck_json: Dict[str, Any],
    deleted_guids: Iterable[str],
    full_deck: bool,
) -> None:
    """Applies one pull to the record.

    ``full_deck`` is true for a new subscription, whose payload holds every
    note of the deck, so the record is complete without asking the server.
    """
    store.add_notes(deck_hash, _guids_in_deck_json(deck_json))
    store.mark_deleted(deck_hash, deleted_guids)
    if full_deck:
        store.mark_complete(deck_hash)


def fill_missing_decks(store: CollabNoteStore, deck_hashes: List[str], post) -> None:
    """Fills the record of every subscribed deck that is not complete yet.

    Runs in the background. ``post`` is ``api_client.post_json``. Any failure,
    including a server that does not have the endpoint yet, leaves those decks
    incomplete, so their notes keep showing as unknown and the next profile
    open tries again.
    """
    store.forget_decks_except(deck_hashes)
    complete = store.complete_decks()
    missing = [h for h in deck_hashes if h not in complete]
    if not missing:
        return

    try:
        response = post(NOTE_GUIDS_ENDPOINT, {"deck_hashes": missing}, timeout=30)
        if response.status_code == 404:
            logger.info("AnkiCollab: the server does not list deck notes yet")
            return
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning(f"AnkiCollab: could not fetch the deck note lists: {exc}")
        return

    if not isinstance(payload, dict):
        logger.warning("AnkiCollab: unexpected deck note list response")
        return

    for deck_hash in missing:
        guids = payload.get(deck_hash)
        if not isinstance(guids, list):
            continue
        store.add_notes(deck_hash, (g for g in guids if isinstance(g, str)))
        store.mark_complete(deck_hash)
