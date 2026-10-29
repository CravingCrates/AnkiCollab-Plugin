"""Tests for consent.py — the review-data consent state machine.

These lock in the decisions a privacy feature cannot afford to regress:
a refusal is remembered, an unverifiable account fails closed, and consent
granted under older wording stops uploads until it is confirmed again.
"""

from unittest.mock import MagicMock, patch

import pytest

import config_manager
import consent
from consent import CURRENT_WORDING_VERSION

ACCOUNT = "account-hash-1"
OTHER_ACCOUNT = "account-hash-2"


def _granted(deck_hash="deck", version=CURRENT_WORDING_VERSION, account=ACCOUNT):
    return {
        "settings": {},
        deck_hash: {
            "deckId": 1,
            "review_data_consent": {
                "granted": True,
                "wording_version": version,
                "decided_at": "2026-10-29T11:50:41+00:00",
                "account": account,
            },
        },
    }


def _declined(deck_hash="deck", account=ACCOUNT):
    return {
        "settings": {},
        deck_hash: {
            "deckId": 1,
            "review_data_consent": {
                "granted": False,
                "wording_version": CURRENT_WORDING_VERSION,
                "decided_at": "2026-10-29T11:50:41+00:00",
                "account": account,
            },
        },
    }


@pytest.fixture(autouse=True)
def _account(request, monkeypatch):
    """Default every test to a resolved account; individual tests override.

    ``TestCurrentAccount`` exercises the real resolver, so it is left alone.
    """
    if request.cls is TestCurrentAccount:
        yield
        return
    monkeypatch.setattr(consent, "current_account", lambda: ACCOUNT)
    consent.reset_account_cache()
    yield


class TestNeverAsked:
    def test_no_record(self, mw_mock):
        config_manager.write_config({"settings": {}, "deck": {"deckId": 1}})
        assert consent.get_consent("deck") is None
        assert consent.needs_prompt("deck") is True
        assert consent.has_granted_consent("deck") is False

    def test_unknown_deck(self, mw_mock):
        config_manager.write_config({"settings": {}})
        assert consent.get_consent("nope") is None
        assert consent.has_granted_consent("nope") is False


class TestGranted:
    def test_current_consent_grants(self, mw_mock):
        config_manager.write_config(_granted())
        assert consent.needs_prompt("deck") is False
        assert consent.has_granted_consent("deck") is True

    def test_other_account_is_not_bound(self, mw_mock, monkeypatch):
        # User A granted on this profile; user B is signed in now.
        config_manager.write_config(_granted(account=OTHER_ACCOUNT))
        monkeypatch.setattr(consent, "current_account", lambda: ACCOUNT)
        assert consent.needs_prompt("deck") is True
        assert consent.has_granted_consent("deck") is False

    def test_unknown_account_fails_closed(self, mw_mock, monkeypatch):
        config_manager.write_config(_granted())
        monkeypatch.setattr(consent, "current_account", lambda: None)
        assert consent.has_granted_consent("deck") is False

    def test_old_wording_is_stale(self, mw_mock):
        config_manager.write_config(_granted(version=CURRENT_WORDING_VERSION - 1))
        assert consent.needs_prompt("deck") is True
        assert consent.has_granted_consent("deck") is False


class TestDeclined:
    def test_decline_is_never_asked_again(self, mw_mock):
        config_manager.write_config(_declined())
        assert consent.needs_prompt("deck") is False
        assert consent.has_granted_consent("deck") is False

    def test_decline_survives_a_wording_change(self, mw_mock):
        config = _declined()
        config["deck"]["review_data_consent"]["wording_version"] = 1
        config_manager.write_config(config)
        assert consent.needs_prompt("deck") is False

    def test_decline_from_another_account_is_asked_again(self, mw_mock, monkeypatch):
        # Someone else's "no" is not this user's decision.
        config_manager.write_config(_declined(account=OTHER_ACCOUNT))
        monkeypatch.setattr(consent, "current_account", lambda: ACCOUNT)
        assert consent.needs_prompt("deck") is True


class TestLegacyMigration:
    def test_legacy_yes_is_stale_not_granted(self, mw_mock):
        config_manager.write_config(
            {"settings": {}, "deck": {"deckId": 1, "share_stats": True}}
        )
        record = consent.get_consent("deck")
        assert record is not None
        assert record.is_legacy
        assert consent.needs_prompt("deck") is True
        assert consent.has_granted_consent("deck") is False

    def test_legacy_no_is_honoured(self, mw_mock):
        config_manager.write_config(
            {"settings": {}, "deck": {"deckId": 1, "share_stats": False}}
        )
        assert consent.needs_prompt("deck") is False
        assert consent.has_granted_consent("deck") is False


class TestRecordConsent:
    def test_records_answer_with_version_and_account(self, mw_mock):
        config_manager.write_config({"settings": {}, "deck": {"deckId": 1}})
        assert consent.record_consent("deck", True) is True

        record = config_manager.get_stored_config()["deck"]["review_data_consent"]
        assert record["granted"] is True
        assert record["wording_version"] == CURRENT_WORDING_VERSION
        assert record["account"] == ACCOUNT
        assert record["decided_at"]
        assert consent.has_granted_consent("deck") is True

    def test_records_a_decline(self, mw_mock):
        config_manager.write_config({"settings": {}, "deck": {"deckId": 1}})
        assert consent.record_consent("deck", False) is True
        assert consent.needs_prompt("deck") is False
        assert consent.has_granted_consent("deck") is False

    def test_drops_the_legacy_key(self, mw_mock):
        config_manager.write_config(
            {"settings": {}, "deck": {"deckId": 1, "share_stats": True}}
        )
        consent.record_consent("deck", True)
        details = config_manager.get_stored_config()["deck"]
        assert "share_stats" not in details

    def test_refuses_without_an_account(self, mw_mock, monkeypatch):
        # Fail closed: an unverifiable decision must not be written.
        monkeypatch.setattr(consent, "current_account", lambda: None)
        config_manager.write_config({"settings": {}, "deck": {"deckId": 1}})
        assert consent.record_consent("deck", True) is False
        assert "review_data_consent" not in config_manager.get_stored_config()["deck"]

    def test_unknown_deck_is_refused(self, mw_mock):
        config_manager.write_config({"settings": {}})
        assert consent.record_consent("missing", True) is False


class TestStatsWatermark:
    """Revoking must not leave a watermark that would skip old reviews."""

    def _with_watermark(self):
        config_manager.write_config(
            {
                "settings": {},
                "deck": {"deckId": 1, "last_stats_timestamp": 1793283241},
            }
        )

    def test_revoking_clears_the_watermark(self, mw_mock):
        self._with_watermark()

        assert consent.record_consent("deck", False) is True

        assert (
            config_manager.get_stored_config()["deck"][consent.STATS_TIMESTAMP_KEY] == 0
        )

    def test_granting_keeps_the_watermark(self, mw_mock):
        self._with_watermark()

        assert consent.record_consent("deck", True) is True

        assert (
            config_manager.get_stored_config()["deck"][consent.STATS_TIMESTAMP_KEY]
            == 1793283241
        )

    def test_reset_ignores_missing_entries(self, mw_mock):
        details = {}
        consent.reset_stats_watermark(details)
        assert details == {consent.STATS_TIMESTAMP_KEY: 0}

        consent.reset_stats_watermark(None)  # type: ignore[arg-type]


class TestErasureQueue:
    def test_enqueue_is_idempotent(self, mw_mock):
        config_manager.write_config({"settings": {}})
        consent.enqueue_erasure("deck")
        consent.enqueue_erasure("deck")
        assert consent.pending_erasures() == ["deck"]

    def test_clear_removes_the_key_when_empty(self, mw_mock):
        config_manager.write_config({"settings": {}})
        consent.enqueue_erasure("deck")
        consent.clear_erasure("deck")
        assert consent.pending_erasures() == []
        assert "pending_erasure" not in config_manager.get_stored_config()["settings"]

    def test_add_to_queue_preserves_other_keys(self, mw_mock):
        config = {"settings": {"pull_on_startup": True}, "deck": {"deckId": 1}}
        consent.add_to_queue(config, "deck")
        assert config["settings"]["pull_on_startup"] is True
        assert config["settings"]["pending_erasure"] == ["deck"]

    def test_queue_survives_subscription_deletion(self, mw_mock):
        # The queue is account-level: dropping a subscription must not lose it.
        config_manager.write_config(_granted())
        consent.enqueue_erasure("deck")
        stored = config_manager.get_stored_config()
        stored.pop("deck")
        config_manager.write_config(stored)
        assert consent.pending_erasures() == ["deck"]


class TestRequestErasure:
    def test_accepted_on_200(self, mw_mock):
        response = MagicMock(status_code=200)
        with patch("api_client.api_client") as api:
            api.post_json.return_value = response
            assert consent.request_erasure("deck") is True
            api.post_json.assert_called_once_with(
                "/DeleteDeckStats", {"deck_hash": "deck"}, timeout=15
            )

    def test_scheduled_on_202_is_accepted(self, mw_mock):
        # Deletion is asynchronous server-side: 202 means "queued", and that
        # is the success path, not a partial one.
        with patch("api_client.api_client") as api:
            api.post_json.return_value = MagicMock(status_code=202)
            assert consent.request_erasure("deck") is True

    def test_missing_route_is_not_success(self, mw_mock):
        with patch("api_client.api_client") as api:
            api.post_json.return_value = MagicMock(status_code=404)
            assert consent.request_erasure("deck") is False

    def test_failure_keeps_item_queued(self, mw_mock):
        with patch("api_client.api_client") as api:
            api.post_json.return_value = MagicMock(status_code=500)
            assert consent.request_erasure("deck") is False

    def test_network_error_is_not_success(self, mw_mock):
        with patch("api_client.api_client") as api:
            api.post_json.side_effect = RuntimeError("Not logged in")
            assert consent.request_erasure("deck") is False


class TestFlushPendingErasures:
    def test_accepted_flush_clears_the_queue(self, mw_mock):
        config_manager.write_config({"settings": {}})
        consent.enqueue_erasure("deck")
        with patch.object(consent, "request_erasure", return_value=True):
            consent.flush_pending_erasures()
        assert consent.pending_erasures() == []

    def test_failed_flush_keeps_the_item(self, mw_mock):
        config_manager.write_config({"settings": {}})
        consent.enqueue_erasure("deck")
        with patch.object(consent, "request_erasure", return_value=False):
            consent.flush_pending_erasures()
        assert consent.pending_erasures() == ["deck"]

    def test_empty_queue_does_nothing(self, mw_mock):
        config_manager.write_config({"settings": {}})
        with patch.object(consent, "request_erasure") as erase:
            consent.flush_pending_erasures()
        erase.assert_not_called()


class TestPlanChange:
    """What the Configure tab's Save does with the Review data checkbox."""

    def test_untouched_writes_nothing(self):
        # Opening the window to move a deck must not record a decision.
        assert consent.plan_change(True, True).write is None
        assert consent.plan_change(False, False).write is None

    def test_never_asked_and_left_clear_is_not_a_decline(self):
        plan = consent.plan_change(None, False)
        assert plan.write is None
        assert plan.queue_erasure is False

    def test_never_asked_and_ticked_grants(self):
        assert consent.plan_change(None, True).write is True

    def test_turning_off_asks_for_confirmation_and_queues_erasure(self):
        plan = consent.plan_change(True, False)
        assert plan.write is False
        assert plan.confirm_erasure is True
        assert plan.queue_erasure is True

    def test_turning_on_grants_without_erasing(self):
        plan = consent.plan_change(False, True)
        assert plan.write is True
        assert plan.confirm_erasure is False
        assert plan.queue_erasure is False


class TestCurrentAccount:
    def test_no_token_means_no_account(self, mw_mock, monkeypatch):
        consent.reset_account_cache()
        with patch("auth_manager.auth_manager") as auth:
            auth.get_token.return_value = ""
            assert consent.current_account() is None

    def test_resolution_is_cached_per_token(self, mw_mock, monkeypatch):
        consent.reset_account_cache()
        with (
            patch("auth_manager.auth_manager") as auth,
            patch("identifier.get_user_hash", return_value=ACCOUNT) as user_hash,
        ):
            auth.get_token.return_value = "token-1"
            assert consent.current_account() == ACCOUNT
            assert consent.current_account() == ACCOUNT
        assert user_hash.call_count == 1

    def test_a_different_token_is_re_resolved(self, mw_mock, monkeypatch):
        consent.reset_account_cache()
        with (
            patch("auth_manager.auth_manager") as auth,
            patch("identifier.get_user_hash", return_value=ACCOUNT) as user_hash,
        ):
            auth.get_token.return_value = "token-1"
            consent.current_account()
            auth.get_token.return_value = "token-2"
            consent.current_account()
        assert user_hash.call_count == 2
