"""Tests for stats.py — ReviewHistory, retention calculation, timestamps."""

import pytest
from unittest.mock import MagicMock, patch
from collections import defaultdict
from datetime import datetime, timezone

import config_manager
from stats import ReviewHistory, update_stats_timestamp


class TestReviewHistoryInit:
    @pytest.fixture(autouse=True)
    def _setup_config(self, mw_mock):
        config = {
            "settings": {},
            "hash_stats": {
                "deckId": 7,
                "timestamp": "2025-01-01 00:00:00",
                "last_stats_timestamp": 0,
            },
        }
        config_manager.write_config(config)
        mw_mock.col.decks.children.return_value = []

    def test_stores_deck_hash(self, mw_mock):
        rh = ReviewHistory("hash_stats")
        assert rh.deck_hash == "hash_stats"
        assert rh.deck_id == 7

    def test_deck_ids_includes_self(self, mw_mock):
        rh = ReviewHistory("hash_stats")
        assert 7 in rh.deck_ids


class TestCalcRetention:
    @pytest.fixture
    def rh(self, mw_mock):
        config = {
            "settings": {},
            "hash_r": {"deckId": 1, "timestamp": "2025-01-01 00:00:00"},
        }
        config_manager.write_config(config)
        mw_mock.col.decks.children.return_value = []
        return ReviewHistory("hash_r")

    def test_no_reviews_returns_minus_one(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (0, 0)
        assert rh.calc_retention(1) == -1

    def test_none_values_returns_minus_one(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (None, None)
        assert rh.calc_retention(1) == -1

    def test_all_passed(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (0, 10)
        result = rh.calc_retention(1)
        assert result == 100

    def test_all_failed(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (10, 0)
        result = rh.calc_retention(1)
        assert result == 0

    def test_partial_retention_two_thirds(self, rh, mw_mock):
        # 2 passed, 1 failed → 66 — locks `int(passed * 100 / total)` behavior.
        # A regression to `int(passed / total) * 100` would silently yield 0
        # here, which is exactly the kind of bug this mid-range case catches.
        mw_mock.col.db.first.return_value = (1, 2)
        assert rh.calc_retention(1) == 66

    def test_partial_retention_one_third(self, rh, mw_mock):
        # 1 passed, 2 failed → 33
        mw_mock.col.db.first.return_value = (2, 1)
        assert rh.calc_retention(1) == 33

    def test_partial_retention_rounds_down_not_up(self, rh, mw_mock):
        # 1 passed, 1 failed → 50 (exact); 1 passed, 5 failed → 16
        mw_mock.col.db.first.return_value = (1, 1)
        assert rh.calc_retention(1) == 50
        mw_mock.col.db.first.return_value = (5, 1)
        assert rh.calc_retention(1) == 16


class TestGetCardData:
    @pytest.fixture
    def rh(self, mw_mock):
        config = {
            "settings": {},
            "hash_cd": {"deckId": 1, "timestamp": "2025-01-01 00:00:00"},
        }
        config_manager.write_config(config)
        mw_mock.col.decks.children.return_value = []
        return ReviewHistory("hash_cd")

    def test_empty_result(self, rh, mw_mock):
        mw_mock.col.db.execute.return_value = []
        result = rh.get_card_data(0)
        assert len(result) == 0

    def test_groups_by_deck_and_guid(self, rh, mw_mock):
        # Simulate 2 cards in same deck, same note
        mw_mock.col.db.execute.return_value = [
            (100, 5, 1, "guid_a", 1),
            (101, 3, 0, "guid_a", 1),
        ]
        mw_mock.col.decks.name.return_value = "TestDeck"
        mw_mock.col.db.first.return_value = (0, 10)  # 100% retention

        result = rh.get_card_data(0)
        assert "TestDeck" in result
        assert "guid_a" in result["TestDeck"]


class TestUploadReviewHistory:
    @pytest.fixture
    def rh(self, mw_mock):
        config = {
            "settings": {},
            "hash_up": {"deckId": 1, "timestamp": "2025-01-01 00:00:00"},
        }
        config_manager.write_config(config)
        mw_mock.col.decks.children.return_value = []
        return ReviewHistory("hash_up")

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_upload_sends_compressed_data(self, mock_am, mock_api, rh, mw_mock):
        mock_am.get_token.return_value = "valid_token"
        mw_mock.col.db.execute.return_value = [
            (100, 5, 1, "guid", 1),
        ]
        mw_mock.col.decks.name.return_value = "Deck"
        mw_mock.col.db.first.return_value = (0, 10)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_api.post_gzip.return_value = mock_resp

        rh.upload_review_history(0)
        mock_api.post_gzip.assert_called_once()
        call_args = mock_api.post_gzip.call_args
        assert call_args[0][0] == "/UploadDeckStats"

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_upload_skips_without_token(self, mock_am, mock_api, rh, mw_mock):
        mock_am.get_token.return_value = ""
        mw_mock.col.db.execute.return_value = [(100, 5, 1, "guid", 1)]
        mw_mock.col.decks.name.return_value = "Deck"
        mw_mock.col.db.first.return_value = (0, 10)
        rh.upload_review_history(0)
        mock_api.post_gzip.assert_not_called()


class TestUpdateStatsTimestamp:
    def test_updates_timestamp(self, mw_mock):
        config = {
            "settings": {},
            "hash_ts": {
                "deckId": 1,
                "timestamp": "2025-01-01 00:00:00",
                "last_stats_timestamp": 0,
            },
        }
        config_manager.write_config(config)

        update_stats_timestamp("hash_ts")

        stored = config_manager.get_stored_config()
        assert stored["hash_ts"]["last_stats_timestamp"] > 0
