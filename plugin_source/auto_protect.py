from aqt import mw
from typing import Literal
import aqt.utils
from anki.cards import Card

from .var_defs import PREFIX_PROTECTED_FIELDS
from .utils import get_logger

logger = get_logger("ankicollab.auto_protect")

AUTO_PROTECT_TAG = f"{PREFIX_PROTECTED_FIELDS}::All"


def _is_auto_protect_on_review_enabled() -> bool:
    config = mw.addonManager.getConfig(__name__)
    if not config:
        return False
    return bool(config.get("settings", {}).get("auto_protect_on_review", False))


def _is_auto_protect_learned_enabled() -> bool:
    config = mw.addonManager.getConfig(__name__)
    if not config:
        return False
    return bool(config.get("settings", {}).get("auto_protect_learned", False))


def on_card_reviewed(reviewer, card: Card, ease: Literal[1, 2, 3, 4]) -> None:
    if not _is_auto_protect_on_review_enabled():
        return
    if card.type != 2:
        return

    note = card.note()
    if any(t.lower() == AUTO_PROTECT_TAG.lower() for t in note.tags):
        return

    note.tags.append(AUTO_PROTECT_TAG)
    try:
        undo_id = mw.col.add_custom_undo_entry("Auto-protect note")
        mw.col.update_note(note)
        mw.col.merge_undo_entries(undo_id)
        logger.info(f"Auto-protected note {note.id}")
    except Exception as e:
        logger.error(f"Failed to auto-protect note {note.id}: {e}")


def protect_all_learned() -> None:
    if not mw.col:
        aqt.utils.showInfo("Collection not available.")
        return

    def _task():
        card_ids = mw.col.find_cards("is:review")
        note_ids = set()
        for cid in card_ids:
            card = mw.col.get_card(cid)
            note_ids.add(card.nid)

        notes_to_update = []
        for nid in note_ids:
            note = mw.col.get_note(nid)
            if not any(t.lower() == AUTO_PROTECT_TAG.lower() for t in note.tags):
                note.tags.append(AUTO_PROTECT_TAG)
                notes_to_update.append(note)

        if notes_to_update:
            undo_id = mw.col.add_custom_undo_entry("Protect all learned notes")
            mw.col.update_notes(notes_to_update)
            mw.col.merge_undo_entries(undo_id)

        return len(notes_to_update)

    def _on_done(future):
        try:
            count = future.result()
            aqt.utils.showInfo(
                f"{count} notes protected with {AUTO_PROTECT_TAG}.\n"
                f"the notes won't be erased with new card update."
            )
        except Exception as e:
            aqt.utils.showInfo(f"Error : {e}")
            logger.exception("Error in protect_all_learned")

    mw.taskman.with_progress(
        task=_task,
        on_done=_on_done,
        label="Protecting learned card",
    )
