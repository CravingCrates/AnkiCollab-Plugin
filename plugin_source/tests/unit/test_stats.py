"""Tests for stats.py — ReviewHistory, retention calculation, timestamps."""

import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from collections import defaultdict
from datetime import datetime, timezone

import config_manager
import consent
from stats import (
    DECK_ID_CHUNK_SIZE,
    ReviewHistory,
    on_stats_upload_done,
    update_stats_timestamp,
)


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
    """``calc_retention`` returns (flunked, passed, flunked_any, passed_any)."""

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
        mw_mock.col.db.first.return_value = (0, 0, 0, 0)
        assert rh.calc_retention(1) == -1

    def test_none_values_returns_minus_one(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (None, None, None, None)
        assert rh.calc_retention(1) == -1

    def test_no_rows_returns_minus_one(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = None
        assert rh.calc_retention(1) == -1

    def test_all_passed(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (0, 10, 0, 10)
        result = rh.calc_retention(1)
        assert result == 100

    def test_all_failed(self, rh, mw_mock):
        mw_mock.col.db.first.return_value = (10, 0, 10, 0)
        result = rh.calc_retention(1)
        assert result == 0

    def test_partial_retention_two_thirds(self, rh, mw_mock):
        # 2 passed, 1 failed → 66 — locks `int(passed * 100 / total)` behavior.
        # A regression to `int(passed / total) * 100` would silently yield 0
        # here, which is exactly the kind of bug this mid-range case catches.
        mw_mock.col.db.first.return_value = (1, 2, 1, 2)
        assert rh.calc_retention(1) == 66

    def test_partial_retention_one_third(self, rh, mw_mock):
        # 1 passed, 2 failed → 33
        mw_mock.col.db.first.return_value = (2, 1, 2, 1)
        assert rh.calc_retention(1) == 33

    def test_partial_retention_rounds_down_not_up(self, rh, mw_mock):
        # 1 passed, 1 failed → 50 (exact); 1 passed, 5 failed → 16
        mw_mock.col.db.first.return_value = (1, 1, 1, 1)
        assert rh.calc_retention(1) == 50
        mw_mock.col.db.first.return_value = (5, 1, 5, 1)
        assert rh.calc_retention(1) == 16

    def test_review_answers_win_over_learning_answers(self, rh, mw_mock):
        # 1 flunked / 2 passed on review steps, while the learning steps were
        # 1 passed / 1 flunked. The review numbers must be used.
        mw_mock.col.db.first.return_value = (1, 2, 2, 3)
        assert rh.calc_retention(1) == 66

    def test_falls_back_to_rated_answers_before_first_review(self, rh, mw_mock):
        # Card graduated out of learning today: no review-step answers yet,
        # but it does have real answers, so it must not be dropped.
        mw_mock.col.db.first.return_value = (0, 0, 1, 2)
        assert rh.calc_retention(1) == 66


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
            (100, 5, 1, "guid_a", 1, 0),
            (101, 3, 0, "guid_a", 1, 0),
        ]
        mw_mock.col.decks.name.return_value = "TestDeck"
        mw_mock.col.db.first.return_value = (0, 10, 0, 10)  # 100% retention

        result = rh.get_card_data(0)
        assert "TestDeck" in result
        assert "guid_a" in result["TestDeck"]

    def test_filters_on_home_deck_of_filtered_cards(self, rh, mw_mock):
        # A card borrowed by a filtered deck keeps its home deck in odid; the
        # query has to match (and the payload group) on that deck.
        mw_mock.col.db.execute.return_value = [
            (100, 5, 1, "guid_f", 999, 1),
        ]
        mw_mock.col.decks.name.return_value = "TestDeck"
        mw_mock.col.db.first.return_value = (0, 10, 0, 10)

        result = rh.get_card_data(0)

        mw_mock.col.decks.name.assert_called_once_with(1)
        assert "TestDeck" in result

    def test_query_matches_filtered_deck_cards(self, rh, mw_mock):
        mw_mock.col.db.execute.return_value = []
        rh.get_card_data(1234)

        query, *args = mw_mock.col.db.execute.call_args.args
        assert "cards.odid IN" in query
        # deck id twice (did + odid), then the timestamp
        assert args == [1, 1, 1234]

    def test_zero_retention_note_is_kept(self, rh, mw_mock):
        # A note whose cards were always answered "Again" is real data.
        mw_mock.col.db.execute.return_value = [
            (100, 4, 2, "guid_fail", 1, 0),
        ]
        mw_mock.col.decks.name.return_value = "TestDeck"
        mw_mock.col.db.first.return_value = (2, 0, 2, 0)  # 0% retention

        result = rh.get_card_data(0)

        assert result["TestDeck"]["guid_fail"]["retention"] == 0

    def test_card_without_answers_is_dropped_without_its_note(self, rh, mw_mock):
        mw_mock.col.db.execute.return_value = [
            (100, 4, 0, "guid_ok", 1, 0),
            (101, 0, 0, "guid_silent", 1, 0),
        ]
        mw_mock.col.decks.name.return_value = "TestDeck"
        mw_mock.col.db.first.side_effect = [(0, 2, 0, 2), (0, 0, 0, 0)]

        result = rh.get_card_data(0)

        assert "guid_ok" in result["TestDeck"]
        assert "guid_silent" not in result["TestDeck"]

    def test_deck_without_notes_stays_empty(self, rh, mw_mock):
        mw_mock.col.db.execute.return_value = [(100, 0, 0, "guid", 1, 0)]
        mw_mock.col.decks.name.return_value = "TestDeck"
        mw_mock.col.db.first.return_value = (0, 0, 0, 0)

        result = rh.get_card_data(0)

        assert result["TestDeck"] == {}

    def test_empty_deck_ids_skips_query(self, rh, mw_mock):
        rh.deck_ids = []

        result = rh.get_card_data(0)

        assert result == {}
        mw_mock.col.db.execute.assert_not_called()

    def test_chunks_large_deck_id_lists_within_sqlite_limit(self, rh, mw_mock):
        rh.deck_ids = list(range(1, DECK_ID_CHUNK_SIZE * 2 + 2))
        mw_mock.col.db.execute.side_effect = [
            [(101, 4, 0, "guid_1", 1, 0)],
            [(102, 6, 1, "guid_2", DECK_ID_CHUNK_SIZE + 1, 0)],
            [(103, 8, 2, "guid_3", DECK_ID_CHUNK_SIZE * 2 + 1, 0)],
        ]
        mw_mock.col.decks.name.side_effect = lambda did: f"Deck_{did}"
        mw_mock.col.db.first.return_value = (0, 10, 0, 10)

        result = rh.get_card_data(1234)

        assert mw_mock.col.db.execute.call_count == 3
        chunk_sizes = []
        for call in mw_mock.col.db.execute.call_args_list:
            query, *args = call.args
            assert len(args) <= 999
            assert args[-1] == 1234
            chunk_len = (len(args) - 1) // 2
            assert args[:chunk_len] == args[chunk_len:-1]
            chunk_sizes.append(chunk_len)

        assert chunk_sizes == [DECK_ID_CHUNK_SIZE, DECK_ID_CHUNK_SIZE, 1]
        assert result["Deck_1"]["guid_1"]["reps"] == 4
        assert result[f"Deck_{DECK_ID_CHUNK_SIZE + 1}"]["guid_2"]["reps"] == 6
        assert result[f"Deck_{DECK_ID_CHUNK_SIZE * 2 + 1}"]["guid_3"]["reps"] == 8

    def test_deduplicates_card_matching_across_chunks(self, rh, mw_mock):
        home_did = DECK_ID_CHUNK_SIZE + 1
        rh.deck_ids = list(range(1, home_did + 1))
        shared_card = (100, 10, 2, "guid_dup", 1, home_did)
        other_card = (101, 2, 0, "guid_dup", home_did, 0)
        mw_mock.col.db.execute.side_effect = [
            [shared_card],
            [shared_card, other_card],
        ]
        mw_mock.col.decks.name.side_effect = lambda did: f"Deck_{did}"

        with patch.object(rh, "calc_retention", side_effect=[100, 40]) as mock_ret:
            result = rh.get_card_data(0)

        assert mock_ret.call_count == 2
        assert mock_ret.call_args_list[0].args == (100,)
        assert mock_ret.call_args_list[1].args == (101,)
        assert result[f"Deck_{home_did}"]["guid_dup"] == {
            "retention": 70,
            "lapses": 1,
            "reps": 6,
        }

    def test_large_subdeck_tree_executes_against_real_sqlite(self, rh, mw_mock):
        total_decks = 1200
        rh.deck_ids = list(range(1, total_decks + 1))
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("""
                CREATE TABLE notes (
                    id INTEGER PRIMARY KEY,
                    guid TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE cards (
                    id INTEGER PRIMARY KEY,
                    nid INTEGER NOT NULL,
                    did INTEGER NOT NULL,
                    odid INTEGER NOT NULL,
                    mod INTEGER NOT NULL,
                    type INTEGER NOT NULL,
                    reps INTEGER NOT NULL,
                    lapses INTEGER NOT NULL
                )
                """)
            conn.executemany(
                "INSERT INTO notes (id, guid) VALUES (?, ?)",
                [(1, "guid_c1"), (2, "guid_c2"), (3, "guid_cross")],
            )
            conn.executemany(
                """
                INSERT INTO cards (id, nid, did, odid, mod, type, reps, lapses)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (10, 1, 1, 0, 500, 2, 4, 1),
                    (20, 2, 500, 0, 500, 2, 6, 0),
                    (30, 3, 2, 1100, 500, 2, 8, 2),
                ],
            )

            def real_execute(sql, *params):
                # Enforce SQLite's conservative pre-3.32 variable limit.
                assert len(params) <= 999
                return conn.execute(sql, params).fetchall()

            mw_mock.col.db.execute.side_effect = real_execute
            mw_mock.col.decks.name.side_effect = lambda did: f"Deck_{did}"
            mw_mock.col.db.first.return_value = (0, 10, 0, 10)

            result = rh.get_card_data(100)
        finally:
            conn.close()

        assert result["Deck_1"]["guid_c1"]["reps"] == 4
        assert result["Deck_500"]["guid_c2"]["reps"] == 6
        assert result["Deck_1100"]["guid_cross"]["reps"] == 8


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

    @pytest.fixture
    def ok_response(self):
        response = MagicMock()
        response.ok = True
        response.status_code = 200
        return response

    def _mock_answers(self, mw_mock):
        mw_mock.col.db.execute.return_value = [(100, 5, 1, "guid", 1, 0)]
        mw_mock.col.decks.name.return_value = "Deck"
        mw_mock.col.db.first.return_value = (0, 10, 0, 10)

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_upload_sends_compressed_data(
        self, mock_am, mock_api, rh, mw_mock, ok_response
    ):
        mock_am.get_token.return_value = "valid_token"
        self._mock_answers(mw_mock)
        mock_api.post_gzip.return_value = ok_response

        with patch("stats.consent.has_granted_consent", return_value=True):
            rh.upload_review_history(0)

        mock_api.post_gzip.assert_called_once()
        call_args = mock_api.post_gzip.call_args
        assert call_args[0][0] == "/UploadDeckStats"
        assert call_args[0][1]["consent_version"] == consent.CURRENT_WORDING_VERSION

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_successful_upload_reports_covered_timestamp(
        self, mock_am, mock_api, rh, mw_mock, ok_response
    ):
        mock_am.get_token.return_value = "valid_token"
        self._mock_answers(mw_mock)
        mock_api.post_gzip.return_value = ok_response

        with patch("stats.consent.has_granted_consent", return_value=True):
            covered_until = rh.upload_review_history(0)

        assert isinstance(covered_until, int)
        assert covered_until > 0

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_rejected_upload_reports_failure(self, mock_am, mock_api, rh, mw_mock):
        mock_am.get_token.return_value = "valid_token"
        self._mock_answers(mw_mock)
        response = MagicMock()
        response.ok = False
        response.status_code = 500
        mock_api.post_gzip.return_value = response

        with patch("stats.consent.has_granted_consent", return_value=True):
            assert rh.upload_review_history(0) is None

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_network_error_reports_failure(self, mock_am, mock_api, rh, mw_mock):
        mock_am.get_token.return_value = "valid_token"
        self._mock_answers(mw_mock)
        mock_api.post_gzip.side_effect = RuntimeError("offline")

        with patch("stats.consent.has_granted_consent", return_value=True):
            assert rh.upload_review_history(0) is None

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_empty_history_sends_nothing_but_covers_window(
        self, mock_am, mock_api, rh, mw_mock
    ):
        mock_am.get_token.return_value = "valid_token"
        mw_mock.col.db.execute.return_value = []

        with patch("stats.consent.has_granted_consent", return_value=True):
            covered_until = rh.upload_review_history(0)

        mock_api.post_gzip.assert_not_called()
        assert isinstance(covered_until, int)

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_upload_without_consent_never_sends(self, mock_am, mock_api, rh, mw_mock):
        mock_am.get_token.return_value = "valid_token"
        self._mock_answers(mw_mock)

        with patch("stats.consent.has_granted_consent", return_value=False):
            assert rh.upload_review_history(0) is None

        mock_api.post_gzip.assert_not_called()

    @patch("api_client.api_client")
    @patch("stats.auth_manager")
    def test_upload_skips_without_token(self, mock_am, mock_api, rh, mw_mock):
        mock_am.get_token.return_value = ""
        self._mock_answers(mw_mock)
        with patch("stats.consent.has_granted_consent", return_value=True):
            assert rh.upload_review_history(0) is None
        mock_api.post_gzip.assert_not_called()


class TestOnStatsUploadDone:
    def _config(self, timestamp=7):
        config_manager.write_config(
            {
                "settings": {},
                "hash_done": {
                    "deckId": 1,
                    "timestamp": "2025-01-01 00:00:00",
                    "last_stats_timestamp": timestamp,
                },
            }
        )

    def test_persists_covered_timestamp(self, mw_mock):
        self._config()

        on_stats_upload_done(4242, "hash_done")

        stored = config_manager.get_stored_config()
        assert stored["hash_done"]["last_stats_timestamp"] == 4242

    def test_keeps_timestamp_when_nothing_was_uploaded(self, mw_mock):
        self._config()

        on_stats_upload_done(None, "hash_done")

        stored = config_manager.get_stored_config()
        assert stored["hash_done"]["last_stats_timestamp"] == 7

    def test_finishes_progress(self, mw_mock):
        self._config()

        on_stats_upload_done(None, "hash_done")

        mw_mock.progress.finish.assert_called_once()


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
