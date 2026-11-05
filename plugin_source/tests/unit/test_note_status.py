"""Tests for the local record of which notes exist on AnkiCollab (issue #98).

The record decides what the editor tells a user about a note. Calling a note
"not on AnkiCollab" when it is (or the reverse) would push people into
suggesting duplicates, so these tests pin down when a deck's record counts as
complete and how pulls and the server's note list keep it up to date.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import export_manager
import hooks
import import_manager
import note_status
from note_status import (
    CollabNoteStore,
    NoteStatus,
    fill_missing_decks,
    record_pull,
)


@pytest.fixture
def store(tmp_path):
    return CollabNoteStore(tmp_path / "profile" / note_status.DB_FILE_NAME)


def _response(status_code=200, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    if status_code >= 400:
        response.raise_for_status.side_effect = RuntimeError(f"HTTP {status_code}")
    return response


class TestStatus:
    def test_note_from_a_pull_is_on_collab(self, store):
        store.add_notes("deck1", ["a"])

        assert store.status("a", "deck1") is NoteStatus.ON_COLLAB

    def test_unseen_note_is_unknown_until_the_deck_is_complete(self, store):
        store.add_notes("deck1", ["a"])

        assert store.status("b", "deck1") is NoteStatus.UNKNOWN

        store.mark_complete("deck1")

        assert store.status("b", "deck1") is NoteStatus.NOT_ON_COLLAB

    def test_deleted_note_is_deleted(self, store):
        store.add_notes("deck1", ["a"])
        store.mark_deleted("deck1", ["a"])

        assert store.status("a", "deck1") is NoteStatus.DELETED

    def test_a_note_that_comes_back_is_on_collab_again(self, store):
        store.mark_deleted("deck1", ["a"])
        store.add_notes("deck1", ["a"])

        assert store.status("a", "deck1") is NoteStatus.ON_COLLAB

    def test_completeness_is_per_deck(self, store):
        store.mark_complete("deck1")

        assert store.status("x", "deck2") is NoteStatus.UNKNOWN

    def test_record_survives_reopening(self, store):
        store.add_notes("deck1", ["a"])
        store.mark_complete("deck1")

        reopened = CollabNoteStore(store.path)

        assert reopened.status("a", "deck1") is NoteStatus.ON_COLLAB
        assert reopened.complete_decks() == {"deck1"}

    def test_forgetting_a_deck_removes_its_notes_and_completeness(self, store):
        store.add_notes("keep", ["a"])
        store.add_notes("gone", ["b"])
        store.mark_complete("keep")
        store.mark_complete("gone")

        store.forget_decks_except(["keep"])

        assert store.complete_decks() == {"keep"}
        assert store.status("a", "keep") is NoteStatus.ON_COLLAB
        assert store.status("b", "gone") is NoteStatus.UNKNOWN

    def test_forgetting_with_no_subscriptions_clears_everything(self, store):
        store.add_notes("deck1", ["a"])
        store.mark_complete("deck1")

        store.forget_decks_except([])

        assert store.complete_decks() == set()
        assert store.status("a", "deck1") is NoteStatus.UNKNOWN

    def test_note_status_is_scoped_to_its_deck(self, store):
        store.add_notes("deck1", ["shared"])
        store.mark_complete("deck1")
        store.mark_complete("deck2")

        assert store.status("shared", "deck1") is NoteStatus.ON_COLLAB
        assert store.status("shared", "deck2") is NoteStatus.NOT_ON_COLLAB

    def test_deleting_in_one_deck_does_not_affect_another_deck(self, store):
        store.add_notes("deck1", ["shared"])
        store.add_notes("deck2", ["shared"])
        store.mark_deleted("deck1", ["shared"])

        assert store.status("shared", "deck1") is NoteStatus.DELETED
        assert store.status("shared", "deck2") is NoteStatus.ON_COLLAB

    def test_older_schema_version_is_rebuilt(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(store.path) as conn:
            conn.executescript(
                "CREATE TABLE notes (guid TEXT PRIMARY KEY, deck_hash TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE decks (deck_hash TEXT PRIMARY KEY);"
                "INSERT INTO notes (guid, deck_hash, deleted) VALUES ('a', 'deck1', 0);"
            )

        store.add_notes("deck1", ["a"])
        store.add_notes("deck2", ["a"])

        assert store.status("a", "deck1") is NoteStatus.ON_COLLAB
        assert store.status("a", "deck2") is NoteStatus.ON_COLLAB


class TestRecordPull:
    DECK = {
        "notes": [{"guid": "a"}, {"guid": "b"}],
        "children": [
            {"notes": [{"guid": "c"}], "children": [{"notes": [{"guid": "d"}]}]}
        ],
    }

    def test_notes_in_every_subdeck_are_recorded(self, store):
        record_pull(store, "deck1", self.DECK, [], full_deck=False)

        for guid in "abcd":
            assert store.status(guid, "deck1") is NoteStatus.ON_COLLAB

    def test_deleted_notes_are_marked(self, store):
        record_pull(store, "deck1", self.DECK, ["a", "z"], full_deck=False)

        assert store.status("a", "deck1") is NoteStatus.DELETED
        assert store.status("z", "deck1") is NoteStatus.DELETED

    def test_an_update_does_not_complete_the_deck(self, store):
        record_pull(store, "deck1", self.DECK, [], full_deck=False)

        assert store.status("unseen", "deck1") is NoteStatus.UNKNOWN

    def test_a_new_subscription_completes_the_deck(self, store):
        record_pull(store, "deck1", self.DECK, [], full_deck=True)

        assert store.status("unseen", "deck1") is NoteStatus.NOT_ON_COLLAB

    def test_notes_without_a_guid_are_skipped(self, store):
        deck = {"notes": [{"guid": ""}, {}, "junk", {"guid": "a"}], "children": []}

        record_pull(store, "deck1", deck, [], full_deck=False)

        assert store.status("a", "deck1") is NoteStatus.ON_COLLAB


class TestFillMissingDecks:
    def test_only_incomplete_decks_are_requested(self, store):
        store.mark_complete("done")
        post = MagicMock(return_value=_response(payload={"new": ["a"]}))

        fill_missing_decks(store, ["done", "new"], post)

        post.assert_called_once_with(
            note_status.NOTE_GUIDS_ENDPOINT, {"deck_hashes": ["new"]}, timeout=30
        )
        assert store.status("a", "new") is NoteStatus.ON_COLLAB
        assert store.status("b", "new") is NoteStatus.NOT_ON_COLLAB

    def test_nothing_is_requested_when_every_deck_is_complete(self, store):
        store.mark_complete("deck1")
        post = MagicMock()

        fill_missing_decks(store, ["deck1"], post)

        post.assert_not_called()

    def test_server_without_the_endpoint_leaves_decks_unknown(self, store):
        post = MagicMock(return_value=_response(status_code=404))

        fill_missing_decks(store, ["deck1"], post)

        assert store.complete_decks() == set()
        assert store.status("a", "deck1") is NoteStatus.UNKNOWN

    @pytest.mark.parametrize(
        "post",
        [
            MagicMock(side_effect=ConnectionError("offline")),
            MagicMock(return_value=_response(status_code=500)),
            MagicMock(return_value=_response(payload=["not", "a", "dict"])),
        ],
    )
    def test_failures_leave_decks_unknown(self, store, post):
        fill_missing_decks(store, ["deck1"], post)

        assert store.complete_decks() == set()

    def test_deck_missing_from_the_response_stays_unknown(self, store):
        post = MagicMock(return_value=_response(payload={"deck1": ["a"]}))

        fill_missing_decks(store, ["deck1", "deck2"], post)

        assert store.complete_decks() == {"deck1"}

    def test_unsubscribed_decks_are_forgotten(self, store):
        store.add_notes("old", ["a"])
        store.mark_complete("old")
        post = MagicMock(return_value=_response(payload={"new": []}))

        fill_missing_decks(store, ["new"], post)

        assert store.complete_decks() == {"new"}
        assert store.status("a", "old") is NoteStatus.UNKNOWN

    def test_existing_deleted_marks_are_kept_for_notes_not_in_the_list(self, store):
        store.mark_deleted("deck1", ["gone"])
        post = MagicMock(return_value=_response(payload={"deck1": ["a"]}))

        fill_missing_decks(store, ["deck1"], post)

        assert store.status("gone", "deck1") is NoteStatus.DELETED


class TestPullUpdatesTheRecord:
    def _install(self, store, subscription, input_hash=None):
        deck = MagicMock()
        deck.anki_dict = {"name": "Deck"}
        manager = MagicMock()
        manager.__enter__.return_value = manager
        manager.get_by_hash.return_value = None
        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("import_manager.is_collection_available", return_value=True),
            patch("import_manager.DeckManager", return_value=manager),
            patch("import_manager.get_noteids_from_uuids", return_value=[]),
            patch("import_manager.ask_for_rating"),
            patch("import_manager._handle_stats_sharing_after_import"),
        ):
            return import_manager._on_deck_installed(
                (1, {"success": True}), deck, subscription, input_hash=input_hash
            )

    def test_an_update_records_incoming_and_deleted_notes(self, store, mw_mock):
        subscription = {
            "deck_hash": "deck1",
            "stats_enabled": False,
            "deleted_notes": ["old"],
            "deck": {"notes": [{"guid": "new"}], "children": []},
        }

        self._install(store, subscription)

        assert store.status("new", "deck1") is NoteStatus.ON_COLLAB
        assert store.status("old", "deck1") is NoteStatus.DELETED
        assert store.status("other", "deck1") is NoteStatus.UNKNOWN

    def test_a_new_subscription_completes_the_record(self, store, mw_mock):
        subscription = {
            "deck_hash": "deck1",
            "stats_enabled": False,
            "deleted_notes": [],
            "deck": {"notes": [{"guid": "a"}], "children": []},
        }

        self._install(store, subscription, input_hash="deck1")

        assert store.status("other", "deck1") is NoteStatus.NOT_ON_COLLAB

    def test_a_broken_record_does_not_break_the_import(self, mw_mock):
        broken = MagicMock()
        broken.add_notes.side_effect = OSError("disk full")
        subscription = {
            "deck_hash": "deck1",
            "stats_enabled": False,
            "deleted_notes": [],
            "deck": {"notes": [{"guid": "a"}], "children": []},
        }

        assert self._install(broken, subscription) == "Deck"
        mw_mock.reset.assert_called_once()


class TestEditorButtons:
    def _editor(self, guid="a"):
        note = MagicMock()
        note.id = 1
        note.guid = guid
        note.cards.return_value = [MagicMock()]
        return SimpleNamespace(note=note, web=MagicMock())

    def _scripts(self, store, editor, deck_hash="deck1"):
        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("hooks.get_deck_hash_from_card", return_value=(deck_hash, None)),
        ):
            hooks.update_editor_note_status(editor)
        return [c.args[0] for c in editor.web.eval.call_args_list]

    @staticmethod
    def _button(scripts, button_id):
        [script] = [s for s in scripts if json.dumps(button_id) in s]
        return script

    def test_note_not_on_collab_offers_a_new_suggestion_and_disables_open(self, store):
        store.mark_complete("deck1")

        scripts = self._scripts(store, self._editor("a"))

        suggest = self._button(scripts, hooks._SUGGEST_BUTTON_ID)
        assert '"AC: Suggest new note"' in suggest
        assert "b.disabled = false" in suggest
        assert "b.disabled = true" in self._button(scripts, hooks._OPEN_BUTTON_ID)

    def test_note_on_collab_keeps_the_usual_buttons(self, store):
        store.add_notes("deck1", ["a"])
        store.mark_complete("deck1")

        scripts = self._scripts(store, self._editor("a"))

        suggest = self._button(scripts, hooks._SUGGEST_BUTTON_ID)
        assert '"AC: Suggest changes"' in suggest
        assert "b.disabled = false" in suggest
        assert "b.disabled = false" in self._button(scripts, hooks._OPEN_BUTTON_ID)

    def test_deleted_note_disables_suggesting_and_open(self, store):
        store.mark_deleted("deck1", ["a"])

        scripts = self._scripts(store, self._editor("a"))

        assert "b.disabled = true" in self._button(scripts, hooks._SUGGEST_BUTTON_ID)
        assert "b.disabled = true" in self._button(scripts, hooks._OPEN_BUTTON_ID)

    def test_suggest_from_editor_uses_new_card_rationale_when_not_on_collab(
        self, store
    ):
        store.mark_complete("deck1")
        editor = self._editor("new-note")

        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("hooks.get_deck_hash_from_card", return_value=("deck1", None)),
            patch("hooks.suggest_notes") as suggest,
        ):
            hooks._suggest_from_editor(editor)

        suggest.assert_called_once_with([1], 6, editor=editor)

    def test_suggest_from_editor_uses_updated_content_rationale_when_on_collab(
        self, store
    ):
        store.add_notes("deck1", ["a"])
        store.mark_complete("deck1")
        editor = self._editor("a")

        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("hooks.get_deck_hash_from_card", return_value=("deck1", None)),
            patch("hooks.suggest_notes") as suggest,
        ):
            hooks._suggest_from_editor(editor)

        suggest.assert_called_once_with([1], 2, editor=editor)

    def test_new_card_from_editor_still_opens_commit_dialog(self, mw_mock):
        editor = self._editor("new-note")
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch(
                "export_manager.get_commit_info", return_value=(6, "added new card")
            ) as commit_info,
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._on_suggest_media_optimized") as optimized,
        ):
            export_manager._on_suggest_deck_prepared(
                ("deck", []), 7, "deck1", 6, editor
            )
            query_op.call_args.kwargs["success"]("opt")

        assert commit_info.call_args.args[0] == 6
        optimized.assert_called_once_with(
            "opt", "deck", [], 7, 6, "added new card", editor
        )

    def test_unknown_status_resets_the_buttons(self, store):
        scripts = self._scripts(store, self._editor("a"))

        suggest = self._button(scripts, hooks._SUGGEST_BUTTON_ID)
        assert '"AC: Suggest changes"' in suggest
        assert "b.disabled = false" in suggest
        assert "b.disabled = false" in self._button(scripts, hooks._OPEN_BUTTON_ID)

    def test_note_outside_a_subscribed_deck_resets_the_buttons(self, store):
        store.mark_complete("deck1")

        scripts = self._scripts(store, self._editor("a"), deck_hash=None)

        assert '"AC: Suggest changes"' in self._button(
            scripts, hooks._SUGGEST_BUTTON_ID
        )

    def test_a_failing_lookup_resets_the_buttons(self, store):
        broken = MagicMock()
        broken.status.side_effect = OSError("locked")

        scripts = self._scripts(broken, self._editor("a"))

        assert '"AC: Suggest changes"' in self._button(
            scripts, hooks._SUGGEST_BUTTON_ID
        )


class TestRefreshAndExportUpdates:
    def test_refresh_requests_missing_decks_with_auth_when_logged_in(
        self, store, mw_mock
    ):
        mw_mock.taskman.run_in_background.side_effect = lambda fn: fn()
        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("hooks.DeckManager", return_value=[("deck1", {})]),
            patch("hooks.auth_manager.is_logged_in", return_value=True),
            patch(
                "api_client.api_client.post_json",
                return_value=_response(payload={"deck1": ["a"]}),
            ) as post_json,
        ):
            hooks.refresh_note_status_record()

        post_json.assert_called_once_with(
            note_status.NOTE_GUIDS_ENDPOINT,
            {"deck_hashes": ["deck1"]},
            timeout=30,
            auth=True,
        )
        assert store.status("a", "deck1") is NoteStatus.ON_COLLAB
        assert store.status("b", "deck1") is NoteStatus.NOT_ON_COLLAB

    def test_refresh_skips_network_and_forgets_unsubscribed_decks_when_logged_out(
        self, store, mw_mock
    ):
        store.add_notes("old", ["a"])
        store.mark_complete("old")
        mw_mock.taskman.run_in_background.side_effect = lambda fn: fn()
        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("hooks.DeckManager", return_value=[("deck1", {})]),
            patch("hooks.auth_manager.is_logged_in", return_value=False),
            patch("api_client.api_client.post_json") as post_json,
        ):
            hooks.refresh_note_status_record()

        post_json.assert_not_called()
        assert store.complete_decks() == set()
        assert store.status("a", "old") is NoteStatus.UNKNOWN

    def test_publishing_a_deck_completes_its_record(self, store, mw_mock):
        deck = SimpleNamespace(
            notes=[SimpleNamespace(get_uuid=lambda: "n1")],
            children=[
                SimpleNamespace(
                    notes=[SimpleNamespace(get_uuid=lambda: "n2")], children=[]
                )
            ],
        )
        resp = _response(
            payload={"status": 1, "message": "new-deck", "bulk_operation_id": "op1"}
        )
        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("export_manager.get_personal_tags", return_value=[]),
            patch("export_manager.deck_initializer"),
            patch.object(
                export_manager.Deck,
                "default_json",
                staticmethod(lambda _obj: {"name": "Deck"}),
            ),
            patch("api_client.api_client.post_gzip", return_value=resp),
        ):
            export_manager._create_deck_op(deck)

        assert store.status("n1", "new-deck") is NoteStatus.ON_COLLAB
        assert store.status("n2", "new-deck") is NoteStatus.ON_COLLAB
        assert store.status("other", "new-deck") is NoteStatus.NOT_ON_COLLAB

    def test_maintainer_overwrite_records_submitted_notes(self, store, mw_mock):
        mw_mock.col.decks.name.return_value = "Deck"
        deck = SimpleNamespace(
            notes=[SimpleNamespace(get_uuid=lambda: "n1")],
            children=[],
        )
        store.mark_complete("deck1")
        with (
            patch.object(
                note_status.CollabNoteStore,
                "for_current_profile",
                return_value=store,
            ),
            patch("export_manager.get_deck_hash_from_did", return_value="deck1"),
            patch("export_manager.get_personal_tags", return_value=[]),
            patch("export_manager.get_local_deck_from_hash", return_value="Deck"),
            patch("export_manager.get_maintainer_data", return_value=("tok", True)),
            patch("export_manager.deck_initializer"),
            patch.object(
                export_manager.Deck,
                "default_json",
                staticmethod(lambda _obj: {"name": "Deck"}),
            ),
            patch("api_client.api_client.post_gzip", return_value=_response()),
        ):
            export_manager._submit_deck_op(deck, 1, 10, "", [], {})

        assert store.status("n1", "deck1") is NoteStatus.ON_COLLAB
