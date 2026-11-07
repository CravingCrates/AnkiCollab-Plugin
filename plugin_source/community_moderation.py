"""
community_moderation.py - Client logic for democratic voting,
eligibility checks, and optimistic local adoption of deck suggestions.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from aqt import mw

from .api_client import api_client
from .utils import get_logger, get_did_from_hash, get_deck_and_subdecks

logger = get_logger("ankicollab.community_moderation")

EARLY_ADOPTION_STORE_KEY = "ankicollab_early_adopted_notes"


def get_deck_review_count(deck_id: int) -> int:
    """Calculate the number of reviewed cards for a given deck from Anki's local revlog."""
    if not mw or not mw.col:
        return 0
    try:
        deck_ids = get_deck_and_subdecks(deck_id)
        if not deck_ids:
            return 0
        placeholders = ", ".join("?" for _ in deck_ids)
        query = (
            f"SELECT count(1) FROM revlog WHERE cid IN "
            f"(SELECT id FROM cards WHERE did IN ({placeholders}))"
        )
        count = mw.col.db.scalar(query, *deck_ids)
        return int(count or 0)
    except Exception as exc:
        logger.warning("Failed to count reviews for deck %s: %s", deck_id, exc)
        return 0


def sync_reviewer_eligibility(deck_hash: str) -> Dict[str, Any]:
    """Report local review counts to backend to check moderator qualification."""
    try:
        did = get_did_from_hash(deck_hash)
        count = get_deck_review_count(did) if did else 0
        resp = api_client.post_json(
            "/v2/moderation/sync_eligibility",
            payload={"deck_hash": deck_hash, "cards_reviewed_count": count},
            auth=True,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"success": False, "is_eligible": False, "reason": resp.text}
    except Exception as exc:
        logger.warning("Error syncing reviewer eligibility: %s", exc)
        return {"success": False, "is_eligible": False, "reason": str(exc)}


def get_moderator_status(deck_hash: str) -> Dict[str, Any]:
    """Get the current user's eligibility and weekly vote quota balance."""
    try:
        resp = api_client.get(f"/v2/moderation/status/{deck_hash}", auth=True)
        if resp.status_code == 200:
            return resp.json()
        return {
            "is_eligible": False,
            "reason": resp.text,
            "weekly_votes_limit": 20,
            "remaining_weekly_votes": 0,
            "mode": "consensus_strict",
            "approval_threshold": 5,
            "require_unanimity": True,
            "notifications_enabled": False,
        }
    except Exception as exc:
        logger.warning("Error fetching moderator status: %s", exc)
        return {
            "is_eligible": False,
            "reason": str(exc),
            "weekly_votes_limit": 20,
            "remaining_weekly_votes": 0,
            "mode": "consensus_strict",
            "approval_threshold": 5,
            "require_unanimity": True,
            "notifications_enabled": False,
        }


def get_moderation_queue(deck_hash: str) -> List[Dict[str, Any]]:
    """Retrieve the queue of suggestions waiting for moderation."""
    try:
        resp = api_client.get(f"/v2/moderation/queue/{deck_hash}", auth=True)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception as exc:
        logger.warning("Error fetching moderation queue: %s", exc)
        return []


def submit_vote(
    deck_hash: str,
    suggestion_type: str,
    suggestion_id: int,
    vote: str,
    early_adopted: bool = False,
) -> Dict[str, Any]:
    """Submit a vote (approve, reject, flag) to the backend."""
    try:
        resp = api_client.post_json(
            "/v2/moderation/vote",
            payload={
                "deck_hash": deck_hash,
                "suggestion_type": suggestion_type,
                "suggestion_id": suggestion_id,
                "vote": vote,
                "early_adopted": early_adopted,
            },
            auth=True,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"success": False, "message": resp.text}
    except Exception as exc:
        logger.error("Error submitting vote: %s", exc)
        return {"success": False, "message": str(exc)}


def apply_suggestion_locally(
    note_guid: str,
    field_position: Optional[int],
    new_content: Optional[str],
    tag: Optional[str],
    tag_action: Optional[bool],
    old_content: Optional[str] = None,
) -> bool:
    """Optimistic Local Adoption: immediately apply the approved suggestion

    to the user's local collection, without waiting for global quorum.
    Preserves the user's explicit choice for their local deck while pending.
    """
    if not mw or not mw.col:
        return False
    try:
        nid = mw.col.db.scalar("SELECT id FROM notes WHERE guid = ?", note_guid)
        if not nid:
            logger.info("Note guid %s not found in local collection, skipping local apply", note_guid)
            return False

        note = mw.col.get_note(nid)
        modified = False
        captured_old = old_content

        if field_position is not None and new_content is not None:
            if 0 <= field_position < len(note.fields):
                if captured_old is None:
                    captured_old = note.fields[field_position]
                note.fields[field_position] = new_content
                modified = True

        if tag:
            if tag_action is False:
                note.remove_tag(tag)
                modified = True
            else:
                note.add_tag(tag)
                modified = True

        if modified:
            mw.col.update_note(note)
            record_local_early_adoption(note_guid, {
                "field_position": field_position,
                "old_content": captured_old,
                "new_content": new_content,
                "tag": tag,
                "tag_action": tag_action,
            })
            logger.info("Successfully applied suggestion locally to note %s", note_guid)
            return True
        return False
    except Exception as exc:
        logger.error("Failed to apply suggestion locally to note %s: %s", note_guid, exc)
        return False


def get_early_adoption_store() -> Dict[str, Any]:
    """Retrieve the set of notes early-adopted by this user."""
    if not mw or not mw.pm:
        return {}
    val = mw.pm.profile.get(EARLY_ADOPTION_STORE_KEY)
    if isinstance(val, dict):
        return val
    return {}


def record_local_early_adoption(note_guid: str, detail: Dict[str, Any]) -> None:
    """Record an early adopted note so the user's choice is preserved while pending."""
    if not mw or not mw.pm:
        return
    store = get_early_adoption_store()
    existing = store.get(note_guid)
    if isinstance(existing, dict):
        entries = [existing]
    elif isinstance(existing, list):
        entries = list(existing)
    else:
        entries = []

    key = (detail.get("field_position"), detail.get("tag"))
    entries = [
        e for e in entries
        if isinstance(e, dict) and (e.get("field_position"), e.get("tag")) != key
    ]
    entries.append(detail)
    store[note_guid] = entries
    mw.pm.profile[EARLY_ADOPTION_STORE_KEY] = store


def reconcile_early_adoptions() -> None:
    """Re-apply pending early adoptions after a deck sync, and prune entries
    once the server has either merged the suggestion or updated the field/tag further.
    """
    if not mw or not mw.col or not mw.pm:
        return
    store = get_early_adoption_store()
    if not store:
        return

    updated_store: Dict[str, Any] = {}
    for guid, raw_entries in store.items():
        entries = (
            [raw_entries]
            if isinstance(raw_entries, dict)
            else (raw_entries if isinstance(raw_entries, list) else [])
        )
        if not entries:
            continue

        nid = mw.col.db.scalar("SELECT id FROM notes WHERE guid = ?", guid)
        if not nid:
            continue

        note = mw.col.get_note(nid)
        note_modified = False
        remaining_entries = []

        for detail in entries:
            if not isinstance(detail, dict):
                continue
            fpos = detail.get("field_position")
            old_c = detail.get("old_content")
            new_c = detail.get("new_content")
            tag = detail.get("tag")
            tag_action = detail.get("tag_action")

            if fpos is not None and new_c is not None and 0 <= fpos < len(note.fields):
                synced_val = note.fields[fpos]
                if synced_val == new_c:
                    continue
                if old_c is None or synced_val == old_c:
                    note.fields[fpos] = new_c
                    note_modified = True
                    remaining_entries.append(detail)
            elif tag:
                has_tag = note.has_tag(tag)
                if tag_action is False:
                    if has_tag:
                        note.remove_tag(tag)
                        note_modified = True
                        remaining_entries.append(detail)
                else:
                    if not has_tag:
                        note.add_tag(tag)
                        note_modified = True
                        remaining_entries.append(detail)

        if note_modified:
            mw.col.update_note(note)
        if remaining_entries:
            updated_store[guid] = remaining_entries

    mw.pm.profile[EARLY_ADOPTION_STORE_KEY] = updated_store


def is_locally_adopted(note_guid: str) -> bool:
    """Check if a note has been early-adopted by the user."""
    store = get_early_adoption_store()
    return note_guid in store


MODERATOR_SPECIALIZATION_KEY = "ankicollab_moderator_specializations"


def get_self_specialization(deck_hash: str) -> List[str]:
    """Retrieve self-assigned specialized tags for a deck."""
    if not mw or not mw.pm:
        return []
    store = mw.pm.profile.get(MODERATOR_SPECIALIZATION_KEY)
    if isinstance(store, dict):
        tags = store.get(deck_hash, [])
        if isinstance(tags, list):
            return [str(t).strip() for t in tags if str(t).strip()]
    return []


def save_self_specialization(deck_hash: str, tags: List[str]) -> Dict[str, Any]:
    """Save self-assigned specialized tags both locally and on the backend."""
    cleaned_tags = [str(t).strip() for t in tags if str(t).strip()]
    if mw and mw.pm:
        store = mw.pm.profile.get(MODERATOR_SPECIALIZATION_KEY)
        if not isinstance(store, dict):
            store = {}
        store[deck_hash] = cleaned_tags
        mw.pm.profile[MODERATOR_SPECIALIZATION_KEY] = store

    try:
        resp = api_client.post_json(
            "/v2/moderation/self_specialization",
            payload={"deck_hash": deck_hash, "tags": cleaned_tags},
            auth=True,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"success": True, "tags": cleaned_tags, "backend_synced": False}
    except Exception as exc:
        logger.warning("Failed to sync self-specialization to backend: %s", exc)
        return {"success": True, "tags": cleaned_tags, "backend_synced": False}

