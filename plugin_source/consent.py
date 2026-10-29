"""Review-data consent records and the erasure queue."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config_manager
from .var_defs import WEBSITE_BASE_URL

logger = logging.getLogger(__name__)

# Bump whenever the wording users agree to changes materially. Records made
# under an older version are stale: uploads pause and the user is asked once.
CURRENT_WORDING_VERSION = 2

PRIVACY_POLICY_URL = f"{WEBSITE_BASE_URL}/privacy"

CONSENT_KEY = "review_data_consent"
LEGACY_KEY = "share_stats"
SETTINGS_KEY = "settings"
ERASURE_KEY = "pending_erasure"
# Where "reviews up to here are already on the server" is remembered per deck.
STATS_TIMESTAMP_KEY = "last_stats_timestamp"

DELETE_ENDPOINT = "/DeleteDeckStats"

_account_cache: Dict[str, Optional[str]] = {}


@dataclass(frozen=True)
class Consent:
    granted: bool
    wording_version: int
    decided_at: Optional[str] = None
    account: Optional[str] = None

    @property
    def is_legacy(self) -> bool:
        """Made before account binding and wording versions existed."""
        return self.account is None

    @property
    def is_stale(self) -> bool:
        return self.wording_version < CURRENT_WORDING_VERSION


def reset_account_cache() -> None:
    """Drop the cached account hash (tests, and after a login change)."""
    _account_cache.clear()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_account() -> Optional[str]:
    """The logged-in account's backend hash, or ``None``.

    Fails closed: no token, a refresh failure, or any error resolving the hash
    all return ``None``, which makes every consent check report "not granted".
    """
    try:
        from .auth_manager import auth_manager

        token = auth_manager.get_token()
    except Exception as exc:
        logger.warning("AnkiCollab: could not read the auth token: %s", exc)
        return None

    if not token:
        return None

    if token in _account_cache:
        return _account_cache[token]

    try:
        from .identifier import get_user_hash

        account = get_user_hash() or None
    except Exception as exc:
        logger.warning("AnkiCollab: could not resolve the account hash: %s", exc)
        return None

    if account is None:
        # Do not cache a failure: the next attempt may succeed.
        return None

    _account_cache.clear()
    _account_cache[token] = account
    return account


def can_record_consent() -> bool:
    """Whether a decision would be verifiable, i.e. an account hash is known."""
    return current_account() is not None


def _deck_details(deck_hash: str) -> Optional[Dict[str, Any]]:
    details = config_manager.get_stored_config().get(deck_hash)
    return details if isinstance(details, dict) else None


def _as_version(value: Any) -> int:
    try:
        version = int(value)
    except (TypeError, ValueError):
        return 1
    return version if version > 0 else 1


@dataclass(frozen=True)
class ChangePlan:
    """What to do when the Configure tab's checkbox is saved.

    ``write`` is the value to record, or ``None`` when nothing should change.
    """

    write: Optional[bool]
    confirm_erasure: bool
    queue_erasure: bool


def plan_change(rendered: Optional[bool], checked: bool) -> ChangePlan:
    """Decide what a Save should do, given what was displayed and what is set.

    Writing on every save would let someone record a decline just by opening
    the window to change a deck, so an untouched checkbox writes nothing. A
    deck that has never been asked records only an explicit opt-in: leaving
    the box clear is not a decision, and must not become a permanent "no".
    """
    if rendered is None:
        if checked:
            return ChangePlan(write=True, confirm_erasure=False, queue_erasure=False)
        return ChangePlan(write=None, confirm_erasure=False, queue_erasure=False)
    if rendered == checked:
        return ChangePlan(write=None, confirm_erasure=False, queue_erasure=False)
    if checked:
        return ChangePlan(write=True, confirm_erasure=False, queue_erasure=False)
    return ChangePlan(write=False, confirm_erasure=True, queue_erasure=True)


def get_consent(deck_hash: str) -> Optional[Consent]:
    """The recorded consent for *deck_hash*, or ``None`` if never asked."""
    details = _deck_details(deck_hash)
    if details is None:
        return None

    raw = details.get(CONSENT_KEY)
    if isinstance(raw, dict):
        decided_at = raw.get("decided_at")
        account = raw.get("account")
        return Consent(
            granted=bool(raw.get("granted", False)),
            wording_version=_as_version(raw.get("wording_version")),
            decided_at=decided_at if isinstance(decided_at, str) else None,
            account=account if isinstance(account, str) and account else None,
        )

    legacy = details.get(LEGACY_KEY)
    if isinstance(legacy, bool):
        # Version 1 is the implicit "Remember my decision" era.
        return Consent(granted=legacy, wording_version=1)

    return None


def _belongs_to_current_account(consent: Consent) -> bool:
    """Whether *consent* was given by the account currently signed in.

    Legacy records carry no account and are treated as belonging to whoever is
    signed in: a legacy decline stays a decline (never re-ask), while a legacy
    grant is stale on wording version alone and is re-asked anyway.
    """
    if consent.is_legacy:
        return True
    return consent.account == current_account()


def needs_prompt(deck_hash: str) -> bool:
    """Whether the consent dialog should be shown for *deck_hash*."""
    consent = get_consent(deck_hash)
    if consent is None:
        return True
    if not _belongs_to_current_account(consent):
        return True
    if not consent.granted:
        return False
    return consent.is_stale


def has_granted_consent(deck_hash: str) -> bool:
    """Whether review data may be uploaded for *deck_hash* right now."""
    consent = get_consent(deck_hash)
    if consent is None or not consent.granted or consent.is_stale:
        return False
    if consent.is_legacy:
        return False
    return consent.account == current_account()


def build_record(granted: bool) -> Optional[Dict[str, Any]]:
    """A fresh consent record, or ``None`` when no account can be identified."""
    account = current_account()
    if account is None:
        return None
    return {
        "granted": bool(granted),
        "wording_version": CURRENT_WORDING_VERSION,
        "decided_at": _utc_now(),
        "account": account,
    }


def reset_stats_watermark(details: Dict[str, Any]) -> None:
    """Make the next upload cover the deck's entire review history.

    Called whenever sharing starts from scratch (first subscription) or after
    the user withdrew it: a watermark from an earlier decision would silently
    skip everything that was reviewed before it.
    """
    if isinstance(details, dict):
        details[STATS_TIMESTAMP_KEY] = 0


def record_consent(deck_hash: str, granted: bool) -> bool:
    """Store a decision for *deck_hash*. Returns False if it could not be made."""
    stored = config_manager.get_stored_config()
    details = stored.get(deck_hash)
    if not isinstance(details, dict):
        logger.warning("AnkiCollab: cannot record consent, unknown deck %r", deck_hash)
        return False

    record = build_record(granted)
    if record is None:
        logger.warning(
            "AnkiCollab: consent not recorded for %r - no account hash available",
            deck_hash,
        )
        return False

    details[CONSENT_KEY] = record
    details.pop(LEGACY_KEY, None)
    if not granted:
        reset_stats_watermark(details)
    try:
        config_manager.write_config(stored)
    except Exception as exc:
        logger.warning("AnkiCollab: could not save consent for %r: %s", deck_hash, exc)
        return False
    return True


# ── Erasure queue ──────────────────────────────────────────────────────


def pending_erasures() -> List[str]:
    """Deck hashes whose uploaded review data still needs deleting."""
    settings = config_manager.get_stored_config().get(SETTINGS_KEY)
    if not isinstance(settings, dict):
        return []
    raw = settings.get(ERASURE_KEY)
    if not isinstance(raw, list):
        return []
    return [deck_hash for deck_hash in raw if isinstance(deck_hash, str)]


def add_to_queue(config: Dict[str, Any], deck_hash: str) -> None:
    """Queue *deck_hash* in *config* in place, for callers writing config once."""
    settings = config.get(SETTINGS_KEY)
    if not isinstance(settings, dict):
        settings = {}
        config[SETTINGS_KEY] = settings
    queue = settings.get(ERASURE_KEY)
    if not isinstance(queue, list):
        queue = []
    if deck_hash not in queue:
        queue.append(deck_hash)
    settings[ERASURE_KEY] = queue


def clear_erasure(deck_hash: str) -> None:
    """Drop *deck_hash* from the queue once the server accepted the request."""
    stored = config_manager.get_stored_config()
    settings = stored.get(SETTINGS_KEY)
    if not isinstance(settings, dict):
        return
    queue = settings.get(ERASURE_KEY)
    if not isinstance(queue, list) or deck_hash not in queue:
        return
    settings[ERASURE_KEY] = [h for h in queue if h != deck_hash]
    if not settings[ERASURE_KEY]:
        settings.pop(ERASURE_KEY, None)
    config_manager.write_config(stored)


def enqueue_erasure(deck_hash: str) -> None:
    """Queue *deck_hash* for deletion, durably, before any network attempt."""
    stored = config_manager.get_stored_config()
    add_to_queue(stored, deck_hash)
    try:
        config_manager.write_config(stored)
    except Exception as exc:
        logger.warning("AnkiCollab: could not queue erasure for %r: %s", deck_hash, exc)


def request_erasure(deck_hash: str) -> bool:
    """Ask the backend to delete this account's review data for *deck_hash*."""
    try:
        from .api_client import api_client

        response = api_client.post_json(
            DELETE_ENDPOINT, {"deck_hash": deck_hash}, timeout=15
        )
    except Exception as exc:
        logger.warning("AnkiCollab: could not erase stats for %r: %s", deck_hash, exc)
        return False

    if response.status_code in (200, 202):
        return True

    logger.warning(
        "AnkiCollab: erase stats for %r returned %s; keeping it queued",
        deck_hash,
        response.status_code,
    )
    return False


def flush_pending_erasures() -> None:
    """Retry queued erasures. Safe to call often; a no-op when the queue is empty."""
    for deck_hash in pending_erasures():
        if request_erasure(deck_hash):
            try:
                clear_erasure(deck_hash)
            except Exception as exc:
                logger.warning(
                    "AnkiCollab: erasure accepted for %r but the queue was not "
                    "cleared: %s",
                    deck_hash,
                    exc,
                )
                return
