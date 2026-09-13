"""Tests for hooks.py — field-protection tag parsing, linked-deck config
resolution, and the destructive note-removal request construction.

``remove_notes()``/``request_note_removal()`` ask a remote server to delete
notes from a published deck.  A bug that included the *wrong* note GUIDs would
silently destroy the wrong user data, so these tests pin down exactly which
note IDs are included in the removal request — same rigor as the path-traversal
test for import destinations.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as rmock

import auth_manager as auth_manager_module
import hooks
from hooks import (
    _get_linked_base_hashes,
    _get_protected_fields_from_tags,
    _get_shortcut,
    _shortcut_display,
    remove_notes,
    request_note_removal,
)

# Matches var_defs.PREFIX_PROTECTED_FIELDS
PROTECT_PREFIX = "AnkiCollab_Protect"


@pytest.fixture(autouse=True)
def _valid_token():
    """remove_notes() posts through api_client, which requires a real token."""
    with patch.object(
        auth_manager_module.auth_manager, "get_token", return_value="test_token"
    ):
        yield


class _FakeNote:
    def __init__(self, tags, fields=("Front", "Back")):
        self.tags = list(tags)
        self._fields = list(fields)

    def keys(self):
        return self._fields


class _FakeSeq:
    """Minimal stand-in for the QKeySequence returned by ``fromString``."""

    def __init__(self, native="", empty=False):
        self._native = native
        self._empty = empty

    def isEmpty(self):
        return self._empty

    def toString(self, _format):
        return self._native


def _fake_qkeysequence(native="", empty=False):
    """Build a QKeySequence stand-in whose ``fromString`` yields a fixed seq."""

    class _QKS:
        SequenceFormat = SimpleNamespace(NativeText="native")

        @staticmethod
        def fromString(raw):
            return _FakeSeq(native=native, empty=empty)

    return _QKS


class TestGetProtectedFieldsFromTags:
    def test_correct_extraction(self):
        note = _FakeNote([f"{PROTECT_PREFIX}::Field_One", "normal_tag"])
        assert _get_protected_fields_from_tags(note) == ["Field One"]

    def test_multiple_fields_and_tags_protection(self):
        note = _FakeNote(
            [
                f"{PROTECT_PREFIX}::Tags",
                f"{PROTECT_PREFIX}::My_Field",
                "other",
            ]
        )
        assert _get_protected_fields_from_tags(note) == ["Tags", "My Field"]

    def test_all_tag_returns_all_fields_plus_tags(self):
        note = _FakeNote([f"{PROTECT_PREFIX}::All"], fields=["Front", "Back", "Extra"])
        assert _get_protected_fields_from_tags(note) == [
            "Front",
            "Back",
            "Extra",
            "Tags",
        ]

    def test_no_protection_tags_returns_empty(self):
        assert _get_protected_fields_from_tags(_FakeNote(["leech", "marked"])) == []
        assert _get_protected_fields_from_tags(_FakeNote([])) == []

    def test_case_insensitive_prefix(self):
        note = _FakeNote([f"{PROTECT_PREFIX.lower()}::field_x"])
        assert _get_protected_fields_from_tags(note) == ["field x"]

    def test_malformed_tag_syntax_does_not_crash(self):
        # Bare prefix (no ::), empty field part, and unrelated prefixes.
        note = _FakeNote(
            [PROTECT_PREFIX, f"{PROTECT_PREFIX}::", "Other_Prefix::Whatever"]
        )
        result = _get_protected_fields_from_tags(note)
        assert isinstance(result, list)


class TestGetLinkedBaseHashes:
    def test_chain_resolution_from_list(self, mw_mock):
        config = {
            "settings": {},
            "abc": {"linked_deck_hashes": ["base1", "base2"]},
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)
        assert _get_linked_base_hashes("abc") == ["base1", "base2"]

    def test_legacy_single_hash_migrates_and_writes(self, mw_mock):
        config = {"settings": {}, "abc": {"linked_deck_hash": "legacy_hash"}}
        writes = []
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: config
        mw_mock.addonManager.writeConfig.side_effect = lambda aid, cfg: writes.append(
            cfg
        )

        result = _get_linked_base_hashes("abc")
        assert result == ["legacy_hash"]
        # Legacy key migrated to list form and persisted
        assert writes and "linked_deck_hashes" in writes[0]["abc"]

    def test_empty_config_returns_empty_list(self, mw_mock):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: None
        assert _get_linked_base_hashes("abc") == []

    def test_missing_subscriber_returns_empty(self, mw_mock):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: {"settings": {}}
        assert _get_linked_base_hashes("nope") == []

    def test_non_dict_details_returns_empty(self, mw_mock):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: {
            "abc": "not-a-dict"
        }
        assert _get_linked_base_hashes("abc") == []

    def test_empty_hashes_list_falls_through(self, mw_mock):
        config = {"abc": {"linked_deck_hashes": []}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)
        assert _get_linked_base_hashes("abc") == []


def _note_with_card(nid, deck_hash):
    """A fake anki note whose first card maps to *deck_hash*."""
    card = MagicMock()
    note = MagicMock()
    note.id = nid
    note.cards.return_value = [card]

    def _hash_from_card(_card):
        return deck_hash, None

    return note, _hash_from_card


class TestRemoveNotes:
    def _setup(self, mw_mock, nids, deck_hash="shared_hash"):
        """Wire aqt.mw.col so each nid resolves to a note with a card."""
        notes = {nid: _note_with_card(nid, deck_hash)[0] for nid in nids}
        mw_mock.col.get_note.side_effect = lambda nid: notes.get(nid)

    @patch("hooks.delete_notes")
    @patch("hooks.get_commit_info")
    @patch("hooks.get_guids_from_noteids")
    @patch("hooks.get_deck_hash_from_card")
    @patch("hooks.auth_manager")
    @patch("hooks.askUser")
    def test_removal_request_contains_only_intended_guids(
        self,
        mock_ask,
        mock_am,
        mock_hash,
        mock_guids,
        mock_commit,
        mock_delete,
        mw_mock,
    ):
        mock_am.is_logged_in.return_value = True
        nids = [101, 102]
        self._setup(mw_mock, nids, deck_hash="target_hash")
        mock_hash.return_value = ("target_hash", None)
        mock_guids.return_value = ["guid_101", "guid_102"]
        mock_commit.return_value = (11, "remove these")
        mock_ask.return_value = False  # do not delete locally

        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok", status_code=200)
            remove_notes(nids)

        req = m.last_request
        assert req.method == "POST"
        assert "/requestRemoval" in req.url
        payload = req.json()
        assert payload["remote_deck"] == "target_hash"
        assert payload["note_guids"] == ["guid_101", "guid_102"]
        assert payload["commit_text"] == "remove these"
        assert payload["force_overwrite"] is False
        # No local deletion when the user declines
        mock_delete.assert_not_called()

    @patch("hooks.delete_notes")
    @patch("hooks.get_commit_info")
    @patch("hooks.get_guids_from_noteids")
    @patch("hooks.get_deck_hash_from_card")
    @patch("hooks.auth_manager")
    @patch("hooks.askUser")
    def test_no_request_when_not_logged_in(
        self,
        mock_ask,
        mock_am,
        mock_hash,
        mock_guids,
        mock_commit,
        mock_delete,
        mw_mock,
    ):
        mock_am.is_logged_in.return_value = False
        remove_notes([101])
        mock_guids.assert_not_called()
        mock_delete.assert_not_called()

    @patch("hooks.delete_notes")
    @patch("hooks.get_commit_info")
    @patch("hooks.get_guids_from_noteids")
    @patch("hooks.get_deck_hash_from_card")
    @patch("hooks.auth_manager")
    @patch("hooks.askUser")
    def test_mixed_deck_notes_are_refused(
        self,
        mock_ask,
        mock_am,
        mock_hash,
        mock_guids,
        mock_commit,
        mock_delete,
        mw_mock,
    ):
        """Notes from different published decks must not be removed together."""
        mock_am.is_logged_in.return_value = True
        self._setup(mw_mock, [101, 102], deck_hash="ignored")
        # First note maps to deck_a, second note maps to deck_b → mismatch
        mock_hash.side_effect = [("deck_a", None), ("deck_b", None)]
        mock_commit.return_value = (11, "x")
        mock_ask.return_value = False

        with rmock.Mocker() as m:
            m.post(rmock.ANY, json="ok")
            remove_notes([101, 102])

        # No request should have been sent for a mixed-deck selection
        assert m.last_request is None or "/requestRemoval" not in m.last_request.url
        mock_guids.assert_not_called()
        mock_delete.assert_not_called()

    def test_request_note_removal_delegates_to_remove_notes(self, mw_mock):
        with (
            patch("hooks.remove_notes") as mock_remove,
            patch("hooks.showInfo") as mock_show,
        ):
            request_note_removal(None, [5])
            mock_remove.assert_called_once_with([5], None)
            mock_show.assert_not_called()

    def test_request_note_removal_empty_selection(self, mw_mock):
        with (
            patch("hooks.remove_notes") as mock_remove,
            patch("hooks.showInfo") as mock_show,
        ):
            request_note_removal(None, [])
            mock_remove.assert_not_called()
            mock_show.assert_called_once()


class TestShortcutConfigHelpers:
    """Config lookup and display formatting for the keyboard shortcuts."""

    def _configure(self, mw_mock, settings):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: {
            "settings": settings
        }

    def test_get_shortcut_returns_configured_value(self, mw_mock):
        self._configure(mw_mock, {"shortcut_update_decks": "Ctrl+Alt+U"})
        assert _get_shortcut("update_decks") == "Ctrl+Alt+U"

    def test_get_shortcut_empty_when_missing(self, mw_mock):
        self._configure(mw_mock, {})
        assert _get_shortcut("update_decks") == ""

    def test_get_shortcut_empty_when_no_config(self, mw_mock):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: None
        assert _get_shortcut("bulk_suggest") == ""

    def test_shortcut_display_empty_without_config(self, mw_mock):
        self._configure(mw_mock, {})
        assert _shortcut_display("update_decks") == ""

    def test_shortcut_display_native_text(self, mw_mock, monkeypatch):
        self._configure(mw_mock, {"shortcut_update_decks": "Ctrl+Alt+U"})
        monkeypatch.setattr(hooks, "QKeySequence", _fake_qkeysequence(native="⌃⌥U"))
        assert _shortcut_display("update_decks") == "⌃⌥U"

    def test_shortcut_display_empty_when_unparseable(self, mw_mock, monkeypatch):
        self._configure(mw_mock, {"shortcut_update_decks": "not-a-shortcut"})
        monkeypatch.setattr(hooks, "QKeySequence", _fake_qkeysequence(empty=True))
        assert _shortcut_display("update_decks") == ""
