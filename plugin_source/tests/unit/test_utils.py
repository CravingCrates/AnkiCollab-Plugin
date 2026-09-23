"""Tests for utils.py — DeckManager, collection helpers, backup, logging."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

from utils import (
    DeckManager,
    CollectionUnavailableError,
    OperationAbortedError,
    BackupFailedError,
    ensure_collection,
    is_collection_available,
    check_collection_or_abort,
    get_logger,
    get_timestamp,
    get_hash_from_local_id,
    get_did_from_hash,
    get_local_deck_from_hash,
    get_deck_hash_from_did,
    get_deck_hash_from_card,
    get_deck_and_subdecks,
    create_backup,
    get_noteids_from_uuids,
    get_guids_from_noteids,
)

# ──────────────────────────────────────────────────────────────────────
# Exception classes
# ──────────────────────────────────────────────────────────────────────


class TestExceptions:
    def test_collection_unavailable_is_exception(self):
        with pytest.raises(CollectionUnavailableError):
            raise CollectionUnavailableError("gone")

    def test_operation_aborted_stores_phase(self):
        err = OperationAbortedError("aborted", phase="importing")
        assert err.phase == "importing"
        assert "importing" in str(err)

    def test_backup_failed_is_exception(self):
        with pytest.raises(BackupFailedError):
            raise BackupFailedError("failed")


# ──────────────────────────────────────────────────────────────────────
# ensure_collection / is_collection_available / check_collection_or_abort
# ──────────────────────────────────────────────────────────────────────


class TestCollectionAvailability:
    def test_ensure_collection_success(self, mw_mock):
        col = ensure_collection()
        assert col is mw_mock.col

    def test_ensure_collection_raises_when_no_col(self, mw_no_col):
        with pytest.raises(CollectionUnavailableError):
            ensure_collection()

    def test_is_collection_available_true(self, mw_mock):
        assert is_collection_available() is True

    def test_is_collection_available_false(self, mw_no_col):
        assert is_collection_available() is False

    def test_check_collection_or_abort_success(self, mw_mock):
        col = check_collection_or_abort(phase="test")
        assert col is mw_mock.col

    def test_check_collection_or_abort_raises(self, mw_no_col):
        with pytest.raises(OperationAbortedError, match="importing"):
            check_collection_or_abort(phase="importing")


# ──────────────────────────────────────────────────────────────────────
# DeckManager
# ──────────────────────────────────────────────────────────────────────


class TestDeckManager:
    @pytest.fixture
    def deck_config(self):
        return {
            "settings": {"push_counter": 0},
            "auth": {"token": "tok"},
            "hash_abc": {"deckId": 42, "timestamp": "2025-06-01 12:00:00"},
            "hash_xyz": {"deckId": 99, "timestamp": "2025-01-01 00:00:00"},
        }

    @pytest.fixture
    def mw_with_deck_config(self, mw_mock, deck_config):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(deck_config)
        return mw_mock

    def test_filters_settings_and_auth(self, mw_with_deck_config):
        dm = DeckManager()
        hashes = [h for h, _ in dm]
        assert "settings" not in hashes
        assert "auth" not in hashes
        assert "hash_abc" in hashes
        assert "hash_xyz" in hashes

    def test_get_by_hash(self, mw_with_deck_config):
        dm = DeckManager()
        details = dm.get_by_hash("hash_abc")
        assert details is not None
        assert details["deckId"] == 42

    def test_get_by_hash_missing(self, mw_with_deck_config):
        dm = DeckManager()
        assert dm.get_by_hash("nonexistent") is None

    def test_context_manager_saves(self, mw_with_deck_config):
        with DeckManager() as dm:
            pass
        mw_with_deck_config.addonManager.writeConfig.assert_called()

    def test_iter_yields_tuples(self, mw_with_deck_config):
        dm = DeckManager()
        items = list(dm)
        assert all(isinstance(h, str) and isinstance(d, dict) for h, d in items)

    def test_none_config_yields_empty(self, mw_mock):
        """getConfig returning None should not crash DeckManager."""
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: None
        dm = DeckManager()
        assert list(dm) == []
        assert dm.get_by_hash("anything") is None


# ──────────────────────────────────────────────────────────────────────
# Lookup helpers
# ──────────────────────────────────────────────────────────────────────


class TestLookupHelpers:
    @pytest.fixture(autouse=True)
    def _setup_deck_config(self, mw_mock):
        config = {
            "settings": {},
            "hash_a": {"deckId": 10, "timestamp": "2025-06-01 12:00:00"},
            "hash_b": {"deckId": 20, "timestamp": "2025-01-15 08:30:00"},
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

    def test_get_timestamp_returns_float(self, mw_mock):
        ts = get_timestamp("hash_a")
        assert isinstance(ts, float)
        assert ts > 0

    def test_get_timestamp_missing_hash(self, mw_mock):
        assert get_timestamp("nonexistent") is None

    def test_get_timestamp_returns_none_implicitly(self, mw_mock):
        """get_timestamp returns None when details exist but timestamp parsing fails."""
        config = {
            "settings": {},
            "hash_bad": {"deckId": 99, "timestamp": "not-a-date"},
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)
        with pytest.raises(ValueError):
            get_timestamp("hash_bad")

    def test_get_hash_from_local_id(self, mw_mock):
        assert get_hash_from_local_id(10) == "hash_a"
        assert get_hash_from_local_id(20) == "hash_b"
        assert get_hash_from_local_id(999) is None

    def test_get_did_from_hash(self, mw_mock):
        assert get_did_from_hash("hash_a") == 10
        assert get_did_from_hash("hash_b") == 20
        assert get_did_from_hash("nope") is None

    def test_get_local_deck_from_hash(self, mw_mock):
        mw_mock.col.decks.name.return_value = "My Deck"
        name = get_local_deck_from_hash("hash_a")
        assert name == "My Deck"

    def test_get_local_deck_from_hash_missing(self, mw_mock):
        result = get_local_deck_from_hash("missing")
        assert result == "None"


class TestGetDeckHashFromDid:
    @pytest.fixture(autouse=True)
    def _setup(self, mw_mock):
        config = {
            "settings": {},
            "hash_parent": {"deckId": 1, "timestamp": "2025-01-01 00:00:00"},
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)
        mw_mock.col.decks.parents.return_value = []

    def test_direct_match(self, mw_mock):
        assert get_deck_hash_from_did(1) == "hash_parent"

    def test_no_match_no_parent(self, mw_mock):
        assert get_deck_hash_from_did(999) is None

    def test_falls_back_to_parent(self, mw_mock):
        mw_mock.col.decks.parents.return_value = [{"id": 1, "name": "Parent"}]
        assert get_deck_hash_from_did(999) == "hash_parent"


class TestGetDeckHashFromCard:
    @pytest.fixture(autouse=True)
    def _setup(self, mw_mock):
        config = {
            "settings": {},
            "hash_deck": {"deckId": 5, "timestamp": "2025-01-01 00:00:00"},
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)
        mw_mock.col.decks.parents.return_value = []
        mw_mock.col.decks.get.return_value = {"dyn": False}

    def test_normal_card(self, mw_mock):
        card = MagicMock()
        card.odid = 0
        card.did = 5
        deck_hash, err = get_deck_hash_from_card(card)
        assert deck_hash == "hash_deck"
        assert err is None

    def test_filtered_deck_uses_odid(self, mw_mock):
        card = MagicMock()
        card.odid = 5
        card.did = 100
        mw_mock.col.decks.get.return_value = {"dyn": False}
        deck_hash, err = get_deck_hash_from_card(card)
        assert deck_hash == "hash_deck"

    def test_unknown_card_returns_error(self, mw_mock):
        card = MagicMock()
        card.odid = 0
        card.did = 9999
        deck_hash, err = get_deck_hash_from_card(card)
        assert deck_hash is None
        assert err is not None
        assert "Cannot find" in err

    def test_dynamic_deck_returns_filtered_error(self, mw_mock):
        """Card resolved to a dynamic/filtered deck (dyn=True) with no valid original."""
        card = MagicMock()
        card.odid = 0
        card.did = 5
        mw_mock.col.decks.get.return_value = {"dyn": True}
        deck_hash, err = get_deck_hash_from_card(card)
        assert deck_hash is None
        assert "filtered deck" in err


# ──────────────────────────────────────────────────────────────────────
# get_deck_and_subdecks
# ──────────────────────────────────────────────────────────────────────


class TestGetDeckAndSubdecks:
    def test_returns_empty_for_invalid_ids(self, mw_mock):
        assert get_deck_and_subdecks(None) == []
        assert get_deck_and_subdecks(-1) == []
        assert get_deck_and_subdecks(0) == []

    def test_returns_self_when_no_children(self, mw_mock):
        mw_mock.col.decks.children.return_value = []
        assert get_deck_and_subdecks(1) == [1]

    def test_includes_children(self, mw_mock):
        mw_mock.col.decks.children.side_effect = [
            [("Sub1", 2), ("Sub2", 3)],  # children of 1
            [],  # children of 2
            [],  # children of 3
        ]
        result = get_deck_and_subdecks(1)
        assert 1 in result
        assert 2 in result
        assert 3 in result


# ──────────────────────────────────────────────────────────────────────
# get_logger
# ──────────────────────────────────────────────────────────────────────


class TestGetLogger:
    def test_returns_logger(self):
        lg = get_logger("test_module")
        assert lg.name == "test_module"

    def test_default_name(self):
        lg = get_logger()
        assert lg.name == "ankicollab"


# ──────────────────────────────────────────────────────────────────────
# create_backup
# ──────────────────────────────────────────────────────────────────────


class TestCreateBackup:
    def test_raises_value_error_bg_and_critical(self, mw_mock):
        with pytest.raises(ValueError, match="Cannot use background"):
            create_backup(background=True, critical=True)

    def test_returns_false_when_no_collection(self, mw_no_col):
        assert create_backup() is False

    def test_critical_raises_when_no_collection(self, mw_no_col):
        with pytest.raises(BackupFailedError):
            create_backup(critical=True)


# ──────────────────────────────────────────────────────────────────────
# Note ID / GUID lookups
# ──────────────────────────────────────────────────────────────────────


class TestNoteIdLookups:
    def test_get_noteids_empty_input(self, mw_mock):
        assert get_noteids_from_uuids(None, []) == []

    def test_get_noteids_no_collection(self, mw_no_col):
        assert get_noteids_from_uuids(None, ["guid1"]) == []

    def test_get_noteids_batch(self, mw_mock):
        mw_mock.col.db.list.return_value = [100, 101]
        result = get_noteids_from_uuids(None, ["g1", "g2"])
        assert result == [100, 101]

    def test_get_guids_empty_input(self, mw_mock):
        assert get_guids_from_noteids(None, []) == []

    def test_get_guids_no_collection(self, mw_no_col):
        assert get_guids_from_noteids(None, [1, 2]) == []

    def test_get_guids_batch(self, mw_mock):
        mw_mock.col.db.list.return_value = ["g1", "g2"]
        result = get_guids_from_noteids(None, [100, 101])
        assert result == ["g1", "g2"]
