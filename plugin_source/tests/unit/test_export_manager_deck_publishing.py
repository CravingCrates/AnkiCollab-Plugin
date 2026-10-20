"""Tests for publishing a new deck from export_manager.py.

Covers ``handle_export`` (the guards that run before anything is modified, the
collection backup, and the preparation steps that build the deck representation)
and ``_on_export_deck_created`` (persisting the new cloud deck in the addon
config and deciding whether to continue into the media upload).

Only behaviour that can lose data or corrupt the published deck is asserted:
which guards run first, what reaches the config, and where the pipeline
continues.  User-facing wording and Qt properties are deliberately not pinned.
"""

from __future__ import annotations

import re
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

import config_manager
import export_manager
from export_manager import _on_export_deck_created, handle_export
from tests.mocks import spy_config_writes
from utils import BackupFailedError, OperationAbortedError


def _enter(patchers):
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


# ──────────────────────────────────────────────────────────────────────
# handle_export — guards
# ──────────────────────────────────────────────────────────────────────


class TestHandleExportGuards:
    def test_without_a_collection_nothing_is_exported(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=False),
            patch("export_manager.create_backup") as backup,
            patch("export_manager.aqt.utils.showWarning") as warning,
        ):
            handle_export(7)

        warning.assert_called_once()
        backup.assert_not_called()

    def test_a_missing_deck_is_rejected_as_filtered(self, mw_mock):
        mw_mock.col.decks.get.return_value = None
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.create_backup") as backup,
            patch("export_manager.aqt.utils.showInfo") as info,
        ):
            handle_export(7)

        info.assert_called_once()
        backup.assert_not_called()

    def test_a_filtered_deck_is_rejected(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "F", "dyn": True}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.create_backup") as backup,
            patch("export_manager.aqt.utils.showInfo") as info,
        ):
            handle_export(7)

        info.assert_called_once()
        backup.assert_not_called()

    def test_a_missing_deck_is_looked_up_without_a_fallback_stub(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "Deck", "dyn": False}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=(None, False)),
            patch("export_manager.aqt.utils.showWarning"),
        ):
            handle_export(7)

        assert mw_mock.col.decks.get.call_args.args == (7,)
        assert mw_mock.col.decks.get.call_args.kwargs == {"default": False}

    def test_publishing_requires_a_login_and_no_backup_is_taken(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "Deck", "dyn": False}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=(None, False)),
            patch("export_manager.create_backup") as backup,
            patch("export_manager.aqt.utils.showWarning") as warning,
        ):
            handle_export(7)

        warning.assert_called_once()
        backup.assert_not_called()

    def test_a_failed_backup_aborts_before_the_collection_is_read(self, mw_mock):
        """The backup is the last chance to protect user data — abort if it fails."""
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "Deck", "dyn": False}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch(
                "export_manager.create_backup", side_effect=BackupFailedError("disk")
            ),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.deck_initializer") as deck_initializer,
            patch("export_manager.disambiguate_note_model_uuids") as disambiguate,
        ):
            handle_export(7)

        warning.assert_called_once()
        deck_initializer.from_collection.assert_not_called()
        disambiguate.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# handle_export — preparation
# ──────────────────────────────────────────────────────────────────────


class TestHandleExportPreparation:
    def _run(self, mw_mock, media=None, protected=None):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "D", "dyn": False}
        mw_mock.col.decks.name.return_value = "Some::Deck"
        deck_repr = MagicMock(name="deck_repr")
        deck_repr.get_protected_fields.return_value = (
            ["F1"] if protected is None else protected
        )
        deck_repr.get_media_file_note_map.return_value = (
            [("a.mp3", "g")] if media is None else media
        )
        sorter = MagicMock(name="sorter")

        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup") as backup,
            patch("export_manager.disambiguate_note_model_uuids") as disambiguate,
            patch("export_manager.deck_initializer") as deck_initializer,
            patch("export_manager.NoteSorter", return_value=sorter) as note_sorter,
            patch("export_manager.ConfigSettings") as config_settings,
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager.show_exception") as show_exception,
        ):
            deck_initializer.from_collection.return_value = deck_repr
            handle_export(7)

        return {
            "backup": backup,
            "disambiguate": disambiguate,
            "deck_initializer": deck_initializer,
            "sorter": sorter,
            "note_sorter": note_sorter,
            "config_settings": config_settings,
            "query_op": query_op,
            "deck_repr": deck_repr,
            "show_exception": show_exception,
        }

    def test_a_critical_backup_is_taken_before_reading_the_deck(self, mw_mock):
        env = self._run(mw_mock)
        env["backup"].assert_called_once_with(critical=True)

    def test_the_deck_is_prepared_in_the_expected_order(self, mw_mock):
        env = self._run(mw_mock)

        # UUIDs must be disambiguated before the representation is serialized.
        env["disambiguate"].assert_called_once_with(mw_mock.col)
        env["deck_initializer"].from_collection.assert_called_once_with(
            mw_mock.col, "Some::Deck"
        )
        env["deck_initializer"].trim_empty_children.assert_called_once_with(
            env["deck_repr"]
        )
        env["note_sorter"].assert_called_once_with(
            env["config_settings"].get_instance.return_value
        )
        env["sorter"].sort_deck.assert_called_once_with(env["deck_repr"])

    def test_media_is_collected_without_a_deck_hash(self, mw_mock):
        env = self._run(mw_mock)
        env["deck_repr"].get_protected_fields.assert_called_once_with(None)
        env["deck_repr"].get_media_file_note_map.assert_called_once_with(["F1"])

    def test_media_optimization_runs_in_the_background(self, mw_mock):
        env = self._run(mw_mock)
        assert callable(env["query_op"].call_args.kwargs["op"])
        op_obj = env["query_op"].return_value.failure.return_value
        op_obj.with_progress.assert_called_once()
        op_obj.run_in_background.assert_called_once()

    def test_the_optimizer_receives_the_collected_media(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "D", "dyn": False}
        mw_mock.col.decks.name.return_value = "Some::Deck"
        deck_repr = MagicMock()
        deck_repr.get_protected_fields.return_value = ["F1"]
        deck_repr.get_media_file_note_map.return_value = [("a.mp3", "g")]
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup"),
            patch("export_manager.disambiguate_note_model_uuids"),
            patch("export_manager.deck_initializer") as deck_initializer,
            patch("export_manager.NoteSorter"),
            patch("export_manager.ConfigSettings"),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._sync_optimize_media_and_update_refs") as optimize,
        ):
            deck_initializer.from_collection.return_value = deck_repr
            handle_export(7)
            optimize.return_value = "opt"
            assert query_op.call_args.kwargs["op"](None) == "opt"

        optimize.assert_called_once_with([("a.mp3", "g")])

    def test_the_success_callback_continues_with_the_prepared_deck(self, mw_mock):
        env = self._run(mw_mock)
        with patch("export_manager._on_export_media_optimized") as optimized:
            env["query_op"].call_args.kwargs["success"]("opt-result")

        optimized.assert_called_once_with(
            "opt-result", env["deck_repr"], 7, [("a.mp3", "g")]
        )


class TestHandleExportFailures:
    def _env(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "D", "dyn": False}
        mw_mock.col.decks.name.return_value = "Some::Deck"

    def test_a_connection_error_is_reported_to_the_user(self, mw_mock):
        self._env(mw_mock)
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup"),
            patch("export_manager.disambiguate_note_model_uuids"),
            patch("export_manager.deck_initializer"),
            patch("export_manager.NoteSorter"),
            patch("export_manager.ConfigSettings"),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager.aqt.utils.showWarning") as warning,
        ):
            handle_export(7)
            failure = query_op.return_value.failure.call_args.args[0]
            failure(export_manager.ApiConnectionError("offline"))

        warning.assert_called_once()

    def test_an_aborted_export_is_swallowed(self, mw_mock):
        self._env(mw_mock)
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup"),
            patch("export_manager.disambiguate_note_model_uuids"),
            patch("export_manager.deck_initializer"),
            patch("export_manager.NoteSorter"),
            patch("export_manager.ConfigSettings"),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._handle_operation_aborted", return_value=True),
        ):
            handle_export(7)
            failure = query_op.return_value.failure.call_args.args[0]
            # Must not raise out of the QueryOp failure handler.
            failure(OperationAbortedError("closed", phase="export"))

    def test_an_unexpected_export_failure_is_reraised(self, mw_mock):
        self._env(mw_mock)
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup"),
            patch("export_manager.disambiguate_note_model_uuids"),
            patch("export_manager.deck_initializer"),
            patch("export_manager.NoteSorter"),
            patch("export_manager.ConfigSettings"),
            patch("export_manager.QueryOp") as query_op,
            patch("export_manager._handle_operation_aborted", return_value=False),
            pytest.raises(ValueError),
        ):
            handle_export(7)
            failure = query_op.return_value.failure.call_args.args[0]
            failure(ValueError("boom"))

    def test_a_preparation_error_is_reported_instead_of_crashing(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "D", "dyn": False}
        mw_mock.col.decks.name.side_effect = ValueError("boom")
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup"),
            patch("export_manager.disambiguate_note_model_uuids"),
            patch("export_manager.show_exception") as show_exception,
        ):
            handle_export(7)

        show_exception.assert_called_once()

    def test_a_preparation_connection_error_is_reported(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "D", "dyn": False}
        mw_mock.col.decks.name.side_effect = export_manager.ApiConnectionError("down")
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("tok", False)),
            patch("export_manager.create_backup"),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.show_exception") as show_exception,
        ):
            handle_export(7)

        warning.assert_called_once()
        show_exception.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# _on_export_deck_created
# ──────────────────────────────────────────────────────────────────────


class TestOnExportDeckCreated:
    def _run(
        self,
        mw_mock,
        api_result,
        files_info,
        file_paths,
        ask_user=False,
        monkeypatch=None,
    ):
        config_manager.write_config({})
        writes = spy_config_writes(monkeypatch) if monkeypatch is not None else None
        with (
            patch.object(export_manager, "mw", mw_mock),
            patch("export_manager.aqt.utils.askUser", return_value=ask_user) as ask,
            patch("export_manager.aqt.utils.showInfo") as info,
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.aqt.utils.showCritical") as critical,
            patch("export_manager._start_media_upload") as start_upload,
            patch("export_manager._on_export_media_uploaded") as uploaded,
        ):
            _on_export_deck_created(api_result, 17, files_info, file_paths)
        mocks = {
            "ask": ask,
            "info": info,
            "warning": warning,
            "critical": critical,
            "start_upload": start_upload,
            "uploaded": uploaded,
        }
        if writes is not None:
            mocks["writes"] = writes
        return mocks

    def test_a_new_cloud_deck_is_stored_in_the_addon_config(self, mw_mock):
        self._run(mw_mock, {"status": 1, "message": "new-hash"}, [], {})

        entry = config_manager.get_stored_config()["new-hash"]
        assert entry["deckId"] == 17
        assert entry["optional_tags"] == {}
        assert entry["personal_tags"] == export_manager.DEFAULT_PROTECTED_TAGS
        # Other code parses this timestamp with "%Y-%m-%d %H:%M:%S".
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", entry["timestamp"])
        mw_mock.progress.finish.assert_called_once()

    def test_the_stored_timestamp_is_utc(self, mw_mock):
        """The server compares these timestamps, so the zone is not cosmetic."""
        with patch.object(export_manager, "datetime") as fake_datetime:
            fake_datetime.now.return_value.strftime.return_value = "2026-01-02 03:04:05"
            self._run(mw_mock, {"status": 1, "message": "h"}, [], {})

        assert fake_datetime.now.call_args.args == (export_manager.timezone.utc,)

    def test_an_empty_config_is_initialised_before_writing(self, mw_mock):
        config_manager.write_config({})
        with (
            patch.object(export_manager, "mw", mw_mock),
            patch("export_manager.aqt.utils.askUser", return_value=False),
            patch("export_manager.aqt.utils.showInfo"),
            patch("export_manager._on_export_media_uploaded"),
        ):
            _on_export_deck_created({"status": 1, "message": "h"}, 3, [], {})

        assert set(config_manager.get_stored_config()) == {"h"}

    def test_a_missing_deck_hash_is_not_stored(self, mw_mock, monkeypatch):
        mocks = self._run(
            mw_mock, {"status": 1, "message": ""}, [], {}, monkeypatch=monkeypatch
        )

        assert mocks["writes"] == []

    def test_accepting_the_media_prompt_starts_the_upload_silently(self, mw_mock):
        mocks = self._run(
            mw_mock,
            {"status": 1, "message": "h", "bulk_operation_id": "op1"},
            [{"filename": "a.mp3"}],
            {"a.mp3": "/a.mp3"},
            ask_user=True,
        )

        mocks["start_upload"].assert_called_once_with(
            ("h", "op1", [{"filename": "a.mp3"}], {"a.mp3": "/a.mp3"}, True),
            success_callback=mocks["uploaded"],
        )
        mocks["info"].assert_not_called()

    def test_declining_the_media_prompt_finishes_the_pipeline(self, mw_mock):
        mocks = self._run(
            mw_mock,
            {"status": 1, "message": "h"},
            [{"filename": "a.mp3"}],
            {},
            ask_user=False,
        )

        mocks["start_upload"].assert_not_called()
        mocks["info"].assert_called_once()
        # The completion callback runs with an all-zero summary.
        mocks["uploaded"].assert_called_once_with(
            {
                "uploaded": 0,
                "existing": 0,
                "failed": 0,
                "errors": [],
                "cancelled": False,
                "silent": True,
            }
        )

    def test_a_deck_without_media_never_prompts_and_still_finishes(self, mw_mock):
        mocks = self._run(mw_mock, {"status": 1, "message": "h"}, [], {})

        mocks["ask"].assert_not_called()
        mocks["start_upload"].assert_not_called()
        mocks["info"].assert_called_once()
        mocks["uploaded"].assert_called_once()

    def test_a_rejected_publish_is_reported_and_stops_the_pipeline(self, mw_mock):
        mocks = self._run(
            mw_mock, {"status": 0, "message": "maintainer rejected"}, [], {}
        )

        mocks["warning"].assert_called_once()
        mocks["ask"].assert_not_called()
        mocks["uploaded"].assert_not_called()

    @pytest.mark.parametrize("api_result", [{"status": 2, "message": "weird"}, {}])
    def test_an_unexpected_response_is_reported_as_critical(self, mw_mock, api_result):
        mocks = self._run(mw_mock, api_result, [], {})

        mocks["critical"].assert_called_once()
        mocks["uploaded"].assert_not_called()
