"""Tests for suggesting deck changes from export_manager.py.

Covers the two halves of a suggestion:

* ``suggest_notes`` — the guards that stop a suggestion from being started
  (no collection, nothing selected, notes from different cloud decks, too many
  notes) and the background operation it hands off to.
* ``_on_suggest_deck_prepared`` / ``_submit_deck_op`` — the commit-info step and
  the request that carries the deck to the server, including every way the
  server can refuse it.

Assertions target the decisions that decide whether a suggestion is accepted,
rejected locally, or rejected by the server.  Message wording is not pinned.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
import requests

import export_manager
from export_manager import (
    _on_suggest_deck_prepared,
    _submit_deck_op,
    suggest_notes,
)
from utils import OperationAbortedError
from crowd_anki.representation.deck import Deck, DeckMetadata


def _enter(patchers):
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


def _deck(mw_mock):
    """A real Deck whose serialization collapses to a plain dict."""
    deck = Deck(
        lambda *a, **kw: None, {"name": "Deck", "crowdanki_uuid": "d1", "id": 1}
    )
    deck.metadata = DeckMetadata(models={}, deck_configs={})
    deck.collection = mw_mock.col
    deck.notes = []
    deck.children = []
    return deck


def _resp(status=200, text="", json_payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.raise_for_status = MagicMock()
    if json_payload is not None:
        resp.json.return_value = json_payload
    return resp


def _http_error(response):
    err = requests.exceptions.HTTPError("http error")
    err.response = response
    return err


# Keep the mock collection/metadata away from json.dumps.
_DEFAULT_JSON = patch.object(
    export_manager.Deck,
    "default_json",
    staticmethod(lambda obj: {"name": "Deck", "id": 1}),
)


def _submit_env(deck_hash="h1", token="tok", force=False, personal_tags=None):
    return (
        patch("export_manager.get_deck_hash_from_did", return_value=deck_hash),
        patch(
            "export_manager.get_personal_tags",
            return_value=[] if personal_tags is None else personal_tags,
        ),
        patch("export_manager.get_local_deck_from_hash", return_value="LocalName"),
        patch("export_manager.get_maintainer_data", return_value=(token, force)),
        _DEFAULT_JSON,
        patch("export_manager.deck_initializer"),
    )


def _note_with_card():
    note = MagicMock()
    note.cards.return_value = [MagicMock()]
    return note


class _CheapNote:
    """A note stand-in cheap enough to build 2000 of them."""

    __slots__ = ("_cards",)

    def __init__(self):
        self._cards = [object()]

    def cards(self):
        return self._cards


# ──────────────────────────────────────────────────────────────────────
# _submit_deck_op
# ──────────────────────────────────────────────────────────────────────


class TestSubmitDeckOpGuards:
    def test_requires_a_collection(self, mw_mock):
        with patch.object(export_manager, "mw", MagicMock(col=None)):
            with pytest.raises(AssertionError):
                _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})

    def test_an_unknown_deck_hash_fails_before_any_network_call(self, mw_mock):
        with (
            _enter(_submit_env(deck_hash=None)),
            patch("api_client.api_client.post_gzip") as post,
        ):
            with pytest.raises(ValueError):
                _submit_deck_op(_deck(mw_mock), 7, 5, "c", [], {})
        post.assert_not_called()

    def test_an_expired_login_with_auto_approve_is_rejected(self, mw_mock):
        with _enter(_submit_env(token=None, force=True)):
            with pytest.raises(ValueError):
                _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})

    def test_media_cannot_be_submitted_without_a_login(self, mw_mock):
        with _enter(_submit_env(token=None, force=False)):
            with pytest.raises(ValueError):
                _submit_deck_op(_deck(mw_mock), 1, 5, "c", [{"filename": "a.mp3"}], {})

    def test_a_missing_rationale_is_rejected(self, mw_mock):
        with _enter(_submit_env()):
            with pytest.raises(ValueError):
                _submit_deck_op(_deck(mw_mock), 1, None, "c", [], {})

    def test_a_maintainer_overwrite_does_not_need_a_rationale(self, mw_mock):
        with (
            _enter(_submit_env(token="tok", force=True)),
            patch("api_client.api_client.post_gzip", return_value=_resp()) as post,
        ):
            _submit_deck_op(_deck(mw_mock), 1, None, "c", [], {})

        assert post.call_args.args[1]["rationale"] == 10


class TestSubmitDeckOpRequest:
    def test_the_deck_is_gzipped_to_submitcard_with_a_long_timeout(self, mw_mock):
        mw_mock.col.decks.name.return_value = "Parent::Child"
        deck = _deck(mw_mock)
        with (
            _enter(_submit_env(personal_tags=["personal"])),
            patch("api_client.api_client.post_gzip", return_value=_resp()) as post,
            patch("export_manager.deck_initializer") as deck_initializer,
        ):
            _submit_deck_op(deck, 1, 5, "my commit", [], {})

        assert post.call_args.args[0] == "/submitCard"
        assert post.call_args.kwargs["timeout"] == 120
        payload = post.call_args.args[1]
        assert payload == {
            "remote_deck": "h1",
            "deck_path": "Parent::Child",
            "new_name": "LocalName",
            "deck": payload["deck"],
            "rationale": 5,
            "commit_text": "my commit",
            "force_overwrite": False,
        }
        # Personal tags must never leave the machine.
        deck_initializer.remove_tags_from_notes.assert_called_once_with(
            deck, ["personal"]
        )

    def test_without_personal_tags_nothing_is_scrubbed(self, mw_mock):
        with (
            _enter(_submit_env(personal_tags=[])),
            patch("api_client.api_client.post_gzip", return_value=_resp()),
            patch("export_manager.deck_initializer") as deck_initializer,
        ):
            _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})
        deck_initializer.remove_tags_from_notes.assert_not_called()

    def test_every_lookup_is_scoped_to_the_deck_hash_and_id(self, mw_mock):
        deck = _deck(mw_mock)
        with (
            patch(
                "export_manager.get_deck_hash_from_did", return_value="hash-x"
            ) as hash_for,
            patch("export_manager.get_personal_tags", return_value=[]) as tags,
            patch("export_manager.get_local_deck_from_hash", return_value="L") as local,
            patch(
                "export_manager.get_maintainer_data", return_value=("tok", False)
            ) as maintainer,
            _DEFAULT_JSON,
            patch("api_client.api_client.post_gzip", return_value=_resp()),
            patch("export_manager.deck_initializer"),
        ):
            _submit_deck_op(deck, 7, 5, "c", [], {})

        # The deck id selects the cloud deck; a wrong argument here would
        # submit the suggestion against the wrong deck.
        assert hash_for.call_args.args == (7,)
        tags.assert_called_once_with("hash-x")
        maintainer.assert_called_once_with("hash-x")
        local.assert_called_once_with("hash-x")
        assert mw_mock.col.decks.name.call_args.args == (7,)

    def test_a_stale_deck_representation_is_refreshed_inside_the_request(self, mw_mock):
        deck = _deck(mw_mock)
        deck.refresh_notes = MagicMock()
        with (
            _enter(_submit_env()),
            patch("api_client.api_client.post_gzip", return_value=_resp()),
        ):
            _submit_deck_op(deck, 1, 5, "c", [], {}, media_files_refresh=[("a", "g")])
        deck.refresh_notes.assert_called_once_with([("a", "g")])

    def test_a_failed_refresh_does_not_block_the_submission(self, mw_mock):
        deck = _deck(mw_mock)
        deck.refresh_notes = MagicMock(side_effect=RuntimeError("stale"))
        with (
            _enter(_submit_env()),
            patch("api_client.api_client.post_gzip", return_value=_resp()) as post,
        ):
            _submit_deck_op(deck, 1, 5, "c", [], {}, media_files_refresh=[("a", "g")])

        post.assert_called_once()

    def test_without_a_refresh_list_the_deck_is_left_alone(self, mw_mock):
        deck = _deck(mw_mock)
        deck.refresh_notes = MagicMock()
        with (
            _enter(_submit_env()),
            patch("api_client.api_client.post_gzip", return_value=_resp()),
        ):
            _submit_deck_op(deck, 1, 5, "c", [], {}, media_files_refresh=None)
        deck.refresh_notes.assert_not_called()

    def test_media_returns_the_upload_instructions(self, mw_mock):
        with (
            _enter(_submit_env()),
            patch("api_client.api_client.post_gzip", return_value=_resp()),
        ):
            result = _submit_deck_op(
                _deck(mw_mock),
                1,
                5,
                "c",
                [{"filename": "a.mp3"}],
                {"a.mp3": "/a.mp3"},
            )

        assert result == (
            "h1",
            "",
            [{"filename": "a.mp3"}],
            {"a.mp3": "/a.mp3"},
            False,
        )

    def test_without_media_the_pipeline_stops(self, mw_mock):
        with (
            _enter(_submit_env()),
            patch("api_client.api_client.post_gzip", return_value=_resp()),
        ):
            assert _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {}) is None

    def test_the_serialized_deck_is_utf8_and_readable(self, mw_mock):
        with (
            _enter(_submit_env()),
            patch.object(
                export_manager.Deck,
                "default_json",
                staticmethod(lambda obj: {"name": "Ünïcode", "id": 3}),
            ),
            patch("api_client.api_client.post_gzip", return_value=_resp()) as post,
        ):
            _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})

        raw = post.call_args.args[1]["deck"]
        assert "Ünïcode" in raw
        assert '\n    "name"' in raw


class TestSubmitDeckOpServerErrors:
    def _raise(self, exc):
        return patch("api_client.api_client.post_gzip", side_effect=exc)

    def test_a_missing_response_is_reported_as_a_network_error(self, mw_mock):
        err = requests.exceptions.HTTPError("no response")
        err.response = None
        with _enter(_submit_env()), self._raise(err):
            with pytest.raises(RuntimeError):
                _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})

    @pytest.mark.parametrize(
        "server_text,expected_type",
        [
            # Errors the user can act on are ValueErrors …
            ("Notetype Error: abc-123", ValueError),
            ("Subdecks are not allowed", ValueError),
            ("Deck does not exist", ValueError),
            # … an unknown server failure is not.
            ("Something else", RuntimeError),
        ],
    )
    def test_server_refusals_map_to_the_right_error_class(
        self, mw_mock, server_text, expected_type
    ):
        resp = _resp(400, server_text)
        with _enter(_submit_env()), self._raise(_http_error(resp)):
            with pytest.raises(expected_type):
                _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})

    def test_an_empty_error_body_is_still_an_error(self, mw_mock):
        resp = _resp(500, "")
        with _enter(_submit_env()), self._raise(_http_error(resp)):
            with pytest.raises(RuntimeError):
                _submit_deck_op(_deck(mw_mock), 1, 5, "c", [], {})

    def test_an_unexpected_failure_is_reported_and_reraised(self, mw_mock):
        with (
            _enter(_submit_env()),
            self._raise(RuntimeError("kaboom")),
            patch("export_manager.capture_media_exception") as capture,
            pytest.raises(RuntimeError),
        ):
            _submit_deck_op(_deck(mw_mock), 3, 5, "c", [], {})

        capture.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# suggest_notes
# ──────────────────────────────────────────────────────────────────────


class TestSuggestNotesGuards:
    def test_without_a_collection_nothing_is_suggested(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=False),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([1], 5)

        warning.assert_called_once()
        query_op.assert_not_called()

    def test_an_empty_selection_is_rejected(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([], 5)

        warning.assert_called_once()
        query_op.assert_not_called()

    def test_a_note_that_no_longer_exists_is_reported(self, mw_mock):
        mw_mock.col.get_note.return_value = None
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.show_exception") as show_exception,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([42], 5)

        assert isinstance(show_exception.call_args.kwargs["exception"], ValueError)
        query_op.assert_not_called()

    def test_more_than_two_thousand_notes_is_rejected(self, mw_mock):
        mw_mock.col.get_note.side_effect = lambda nid: MagicMock()
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.aqt.utils.showInfo") as info,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes(list(range(2001)), 5)

        info.assert_called_once()
        query_op.assert_not_called()

    def test_exactly_two_thousand_notes_is_allowed(self, mw_mock):
        notes = [_CheapNote() for _ in range(2000)]
        mw_mock.col.get_note.side_effect = lambda nid: notes[nid]
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card", return_value=("hash", None)
            ),
            patch("export_manager.get_did_from_hash", return_value=7),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager.aqt.utils.showInfo") as info,
        ):
            suggest_notes(list(range(2000)), 5)

        info.assert_not_called()
        query_op.assert_called_once()

    def test_notes_that_are_not_in_a_cloud_deck_are_rejected(self, mw_mock):
        mw_mock.col.get_note.return_value = _note_with_card()
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card", return_value=(None, "reason")
            ),
            patch("export_manager.aqt.utils.showInfo") as info,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([1], 5)

        info.assert_called_once()
        query_op.assert_not_called()

    def test_notes_from_different_cloud_decks_are_rejected(self, mw_mock):
        notes = [_note_with_card(), _note_with_card()]
        mw_mock.col.get_note.side_effect = lambda nid: notes[nid]
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card",
                side_effect=[("hash-a", None), ("hash-b", None)],
            ),
            patch("export_manager.aqt.utils.showInfo") as info,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([0, 1], 5)

        info.assert_called_once()
        query_op.assert_not_called()

    def test_a_cloud_deck_without_a_local_deck_is_rejected(self, mw_mock):
        mw_mock.col.get_note.return_value = _note_with_card()
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card", return_value=("hash", None)
            ),
            patch("export_manager.get_did_from_hash", return_value=None),
            patch("export_manager.aqt.utils.showInfo") as info,
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([1], 5)

        info.assert_called_once()
        query_op.assert_not_called()


class TestSuggestNotesPreparation:
    def _run(self, mw_mock, nids=(11, 12), invoke_success=False):
        mw_mock.col.get_note.side_effect = lambda nid: _note_with_card()
        editor = MagicMock(name="editor")
        with (
            patch("export_manager.QApplication.focusWidget", return_value=None),
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card", return_value=("hash", None)
            ),
            patch("export_manager.get_did_from_hash", return_value=7),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager.QTimer") as timer,
            patch("export_manager._on_suggest_deck_prepared") as prepared,
        ):
            suggest_notes(list(nids), 5, editor=editor)
            if invoke_success:
                query_op.call_args.kwargs["success"]("prepare-result")
                timer.singleShot.call_args.args[1]()
        return query_op, timer, prepared, editor

    def test_preparation_runs_in_the_background_for_the_selected_notes(self, mw_mock):
        query_op, _timer, _prepared, editor = self._run(mw_mock)
        kwargs = query_op.call_args.kwargs
        assert kwargs["parent"] is editor.widget

        with patch("export_manager._prepare_deck_for_suggestion") as prepare:
            prepare.return_value = "prepared"
            assert kwargs["op"]("col") == "prepared"
        prepare.assert_called_once_with(7, [11, 12], "hash")

        op_result = query_op.return_value.failure.return_value
        op_result.with_progress.assert_called_once()
        op_result.run_in_background.assert_called_once()

    def test_the_prepared_deck_is_handed_to_the_next_step(self, mw_mock):
        _query_op, timer, prepared, editor = self._run(mw_mock, invoke_success=True)

        assert timer.singleShot.call_args.args[0] == 0
        prepared.assert_called_once_with("prepare-result", 7, "hash", 5, editor)

    def test_a_connection_error_is_reported(self, mw_mock):
        query_op, _timer, _prepared, _editor = self._run(mw_mock)
        failure = query_op.return_value.failure.call_args.args[0]
        with (
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager._handle_operation_aborted") as aborted,
        ):
            failure(export_manager.ApiConnectionError("offline"))

        warning.assert_called_once()
        aborted.assert_not_called()

    def test_an_aborted_preparation_is_swallowed(self, mw_mock):
        query_op, _timer, _prepared, _editor = self._run(mw_mock)
        failure = query_op.return_value.failure.call_args.args[0]
        with patch("export_manager._handle_operation_aborted", return_value=True):
            failure(OperationAbortedError("closed", phase="suggestion"))

    def test_an_unexpected_preparation_failure_is_reraised(self, mw_mock):
        query_op, _timer, _prepared, _editor = self._run(mw_mock)
        failure = query_op.return_value.failure.call_args.args[0]
        with (
            patch("export_manager._handle_operation_aborted", return_value=False),
            pytest.raises(ValueError),
        ):
            failure(ValueError("boom"))

    def test_a_setup_failure_is_reported_instead_of_crashing(self, mw_mock):
        mw_mock.col.get_note.return_value = _note_with_card()
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card",
                side_effect=ValueError("boom"),
            ),
            patch("export_manager.show_exception") as show_exception,
        ):
            suggest_notes([1], 5)

        show_exception.assert_called_once()

    def test_a_setup_connection_error_is_reported_directly(self, mw_mock):
        mw_mock.col.get_note.return_value = _note_with_card()
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card",
                side_effect=export_manager.ApiConnectionError("offline"),
            ),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.show_exception") as show_exception,
        ):
            suggest_notes([1], 5)

        warning.assert_called_once()
        show_exception.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# _on_suggest_deck_prepared
# ──────────────────────────────────────────────────────────────────────


class TestOnSuggestDeckPrepared:
    def test_without_a_collection_nothing_happens(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=False),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.get_maintainer_data") as maintainer,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, None)

        warning.assert_called_once()
        maintainer.assert_not_called()

    def test_a_missing_login_stops_the_suggestion(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=(None, False)),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.QueryOp") as query_op,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, None)

        warning.assert_called_once()
        query_op.assert_not_called()

    def test_a_cancelled_commit_dialog_stops_the_suggestion(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.get_commit_info", return_value=None) as commit_info,
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager.aqt.utils.tooltip") as tooltip,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, "ED")

        assert commit_info.call_args.args[0] == 5
        tooltip.assert_called_once()
        query_op.assert_not_called()

    def test_a_commit_dialog_without_a_rationale_stops_the_suggestion(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.get_commit_info", return_value=(None, "text")),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager.aqt.utils.tooltip") as tooltip,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, None)

        tooltip.assert_called_once()
        query_op.assert_not_called()

    def test_the_commit_dialog_values_are_used_for_the_submission(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_maintainer_data", return_value=("tok", False)
            ) as maintainer,
            patch(
                "export_manager.get_commit_info", return_value=(7, "my reason")
            ) as commit_info,
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._on_suggest_media_optimized") as optimized,
        ):
            _on_suggest_deck_prepared(("deck", ["m"]), 7, "hash", 5, "ED")
            query_op.call_args.kwargs["success"]("opt")

        assert maintainer.call_args.args == ("hash",)
        assert commit_info.call_args.args[0] == 5
        optimized.assert_called_once_with("opt", "deck", ["m"], 7, 7, "my reason", "ED")

        op_obj = query_op.return_value
        op_obj.with_progress.assert_called_once()
        op_obj.run_in_background.assert_called_once()

    def test_a_new_card_skips_the_dialog_and_the_progress_bar(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.get_commit_info") as commit_info,
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._on_suggest_media_optimized") as optimized,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 6, None)
            query_op.call_args.kwargs["success"]("opt")

        commit_info.assert_not_called()
        optimized.assert_called_once_with("opt", "deck", [], 7, 6, "", None)
        op_obj = query_op.return_value
        op_obj.with_progress.assert_not_called()
        op_obj.run_in_background.assert_called_once()

    def test_a_maintainer_overwrite_forces_the_other_rationale(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", True)),
            patch("export_manager.get_commit_info") as commit_info,
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._on_suggest_media_optimized") as optimized,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, None)
            query_op.call_args.kwargs["success"]("opt")

        commit_info.assert_not_called()
        optimized.assert_called_once_with("opt", "deck", [], 7, 10, "", None)

    def test_a_connection_error_is_reported(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_maintainer_data",
                side_effect=export_manager.ApiConnectionError("offline"),
            ),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.show_exception") as show_exception,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, None)

        warning.assert_called_once()
        show_exception.assert_not_called()

    def test_an_unexpected_failure_is_reported(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", side_effect=ValueError("boom")),
            patch("export_manager.show_exception") as show_exception,
        ):
            _on_suggest_deck_prepared(("deck", []), 7, "hash", 5, None)

        show_exception.assert_called_once()
