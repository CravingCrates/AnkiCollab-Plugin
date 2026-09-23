"""Tests for export_manager.py — regex patterns, media validation, helpers."""

import os
import export_manager
from contextlib import ExitStack
from typing import cast
from unittest.mock import AsyncMock
import pytest
from unittest.mock import MagicMock, patch

from export_manager import (
    COMPILED_SOUND_REGEXES,
    COMPILED_HTML_MEDIA_REGEXES,
    ALL_COMPILED_MEDIA_REGEXES,
    _is_valid_media_file,
    _filter_valid_filename_mapping,
    _apply_media_reference_updates,
    handle_media_upload,
    handle_export,
    get_maintainer_data,
    suggest_notes,
    _on_export_deck_created,
    _handle_media_upload_result,
    _start_media_upload,
    _sync_handle_media_upload,
    _sync_optimize_media_and_update_refs,
    schedule_media_reference_updates,
    _handle_operation_aborted,
    ASYNC_MEDIA_REF_THRESHOLD,
    BATCH_UPDATE_NOTES_SIZE,
)
from utils import BackupFailedError, OperationAbortedError
from crowd_anki.representation.deck import Deck

# ──────────────────────────────────────────────────────────────────────
# Constants → behavior at the boundaries (not tautological `> 0` checks)
# ──────────────────────────────────────────────────────────────────────


class TestBatchUpdateNotesSize:
    """BATCH_UPDATE_NOTES_SIZE controls the batch size for mw.col.update_notes.

    Construct exactly N-1 / N / N+1 notes-to-save and assert the batching loop
    produces the correct number of batches and batch sizes at each boundary.
    """

    @pytest.fixture
    def media_dir(self, tmp_path):
        d = tmp_path / "media"
        d.mkdir()
        (d / "new.mp3").write_bytes(b"x" * 100)  # valid (>= 100 bytes)
        return d

    def _make_updates(self, n):
        return [
            {
                "note_id": i,
                "note_guid": f"guid_{i}",
                "fields": [f"front {i}", "back"],
                "old_fields": ["front", "back"],
                "mod": 1234,
                "old_filenames": ["old.mp3"],
            }
            for i in range(n)
        ]

    def _run(self, mw_mock, n, media_dir):
        results = {
            "updates": self._make_updates(n),
            "filename_mapping": {"old.mp3": "new.mp3"},
        }
        mw_mock.col.media.dir.return_value = str(media_dir)

        class _Note:
            def __init__(self, nid):
                self.id = nid
                self.mod = 1234
                self.fields = ["front", "back"]

        mw_mock.col.get_note.side_effect = lambda nid: _Note(nid)
        mw_mock.col.update_notes.reset_mock()

        count, _opchanges = _apply_media_reference_updates(results)
        batch_sizes = [
            len(call.kwargs["notes"])
            for call in mw_mock.col.update_notes.call_args_list
        ]
        return count, batch_sizes

    def test_one_less_than_batch_size(self, mw_mock, media_dir):
        count, batch_sizes = self._run(mw_mock, BATCH_UPDATE_NOTES_SIZE - 1, media_dir)
        assert count == BATCH_UPDATE_NOTES_SIZE - 1
        assert batch_sizes == [BATCH_UPDATE_NOTES_SIZE - 1]

    def test_exactly_batch_size(self, mw_mock, media_dir):
        count, batch_sizes = self._run(mw_mock, BATCH_UPDATE_NOTES_SIZE, media_dir)
        assert count == BATCH_UPDATE_NOTES_SIZE
        assert batch_sizes == [BATCH_UPDATE_NOTES_SIZE]

    def test_one_more_than_batch_size(self, mw_mock, media_dir):
        count, batch_sizes = self._run(mw_mock, BATCH_UPDATE_NOTES_SIZE + 1, media_dir)
        assert count == BATCH_UPDATE_NOTES_SIZE + 1
        assert batch_sizes == [BATCH_UPDATE_NOTES_SIZE, 1]

    def test_two_batches_exactly(self, mw_mock, media_dir):
        count, batch_sizes = self._run(mw_mock, 2 * BATCH_UPDATE_NOTES_SIZE, media_dir)
        assert count == 2 * BATCH_UPDATE_NOTES_SIZE
        assert batch_sizes == [BATCH_UPDATE_NOTES_SIZE, BATCH_UPDATE_NOTES_SIZE]


class TestAsyncMediaRefThreshold:
    """ASYNC_MEDIA_REF_THRESHOLD decides sync vs async reference updates.

    Just below the threshold the synchronous path must be taken; at and above
    it the async (QueryOp) path must be taken.
    """

    def _run(self, mw_mock, n, force_async=False):
        filename_mapping = {f"old_{i}": f"new_{i}" for i in range(n)}
        media_files = [(f"old_{i}", f"note_{i}") for i in range(n)]
        continuation = MagicMock()
        with (
            patch(
                "export_manager.update_media_references", return_value=(n, None)
            ) as mock_sync,
            patch("export_manager.QueryOp") as mock_qop,
        ):
            schedule_media_reference_updates(
                cast(Deck, None),
                filename_mapping,
                media_files,
                None,
                continuation,
                force_async=force_async,
            )
        return mock_sync, mock_qop, continuation

    def test_below_threshold_uses_sync_path(self, mw_mock):
        mock_sync, mock_qop, cont = self._run(mw_mock, ASYNC_MEDIA_REF_THRESHOLD - 1)
        mock_sync.assert_called_once()
        mock_qop.assert_not_called()
        cont.assert_called_once()

    def test_at_threshold_uses_async_path(self, mw_mock):
        mock_sync, mock_qop, cont = self._run(mw_mock, ASYNC_MEDIA_REF_THRESHOLD)
        mock_sync.assert_not_called()
        mock_qop.assert_called_once()
        cont.assert_not_called()  # runs via QueryOp, which is mocked out

    def test_above_threshold_uses_async_path(self, mw_mock):
        mock_sync, mock_qop, cont = self._run(mw_mock, ASYNC_MEDIA_REF_THRESHOLD + 1)
        mock_sync.assert_not_called()
        mock_qop.assert_called_once()
        cont.assert_not_called()

    def test_force_async_overrides_small_set(self, mw_mock):
        mock_sync, mock_qop, cont = self._run(mw_mock, 1, force_async=True)
        mock_sync.assert_not_called()
        mock_qop.assert_called_once()
        cont.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Media regex patterns
# ──────────────────────────────────────────────────────────────────────


class TestMediaRegexes:
    # ---- sound patterns ----
    def test_sound_regex_simple(self):
        text = "[sound:audio.mp3]"
        for regex in COMPILED_SOUND_REGEXES:
            m = regex.search(text)
            if m:
                assert m.group("fname") == "audio.mp3"
                return
        pytest.fail("No sound regex matched")

    def test_sound_regex_with_path(self):
        text = "[sound:subdir/audio file.mp3]"
        matched = False
        for regex in COMPILED_SOUND_REGEXES:
            m = regex.search(text)
            if m:
                assert "audio file.mp3" in m.group("fname")
                matched = True
        assert matched

    def test_sound_regex_case_insensitive(self):
        text = "[Sound:Test.MP3]"
        matched = any(r.search(text) for r in COMPILED_SOUND_REGEXES)
        assert matched

    # ---- HTML media patterns ----
    def test_img_src_double_quoted(self):
        text = '<img src="photo.jpg">'
        found = False
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m:
                assert m.group("fname") == "photo.jpg"
                found = True
        assert found

    def test_img_src_single_quoted(self):
        text = "<img src='photo.png'>"
        found = False
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            m = regex.search(text)
            if m:
                assert m.group("fname") == "photo.png"
                found = True
        assert found

    def test_img_src_unquoted(self):
        text = "<img src=photo.gif>"
        found = any(r.search(text) for r in COMPILED_HTML_MEDIA_REGEXES)
        assert found

    def test_audio_src(self):
        text = '<audio src="clip.mp3">'
        found = any(r.search(text) for r in COMPILED_HTML_MEDIA_REGEXES)
        assert found

    def test_object_data(self):
        text = '<object data="file.svg"></object>'
        found = any(r.search(text) for r in COMPILED_HTML_MEDIA_REGEXES)
        assert found

    def test_no_match_on_plain_text(self):
        text = "Just some plain text with no media references"
        found = any(r.search(text) for r in ALL_COMPILED_MEDIA_REGEXES)
        assert not found

    def test_multiple_images_in_field(self):
        text = '<img src="a.jpg"> some text <img src="b.png">'
        fnames = []
        for regex in COMPILED_HTML_MEDIA_REGEXES:
            for m in regex.finditer(text):
                fnames.append(m.group("fname"))
        assert "a.jpg" in fnames
        assert "b.png" in fnames


# ──────────────────────────────────────────────────────────────────────
# _is_valid_media_file
# ──────────────────────────────────────────────────────────────────────


class TestIsValidMediaFile:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "valid.png"
        f.write_bytes(b"\x00" * 200)
        assert _is_valid_media_file(str(f)) is True

    def test_too_small_file(self, tmp_path):
        f = tmp_path / "tiny.png"
        f.write_bytes(b"\x00" * 50)  # < 100 bytes
        assert _is_valid_media_file(str(f)) is False

    def test_missing_file(self):
        assert _is_valid_media_file("/does/not/exist.png") is False

    def test_none_path(self):
        assert _is_valid_media_file(cast(str, None)) is False

    def test_exactly_100_bytes(self, tmp_path):
        f = tmp_path / "exact.png"
        f.write_bytes(b"\x00" * 100)
        assert _is_valid_media_file(str(f)) is True


# ──────────────────────────────────────────────────────────────────────
# _filter_valid_filename_mapping
# ──────────────────────────────────────────────────────────────────────


class TestFilterValidFilenameMapping:
    def test_empty_mapping(self):
        assert _filter_valid_filename_mapping({}) == {}

    def test_filters_invalid_files(self, tmp_path, mw_mock):
        # Create one valid and one invalid file
        valid = tmp_path / "good.png"
        valid.write_bytes(b"\x00" * 200)
        # "bad.png" does not exist

        mapping = {"old_good.png": "good.png", "old_bad.png": "bad.png"}
        result = _filter_valid_filename_mapping(mapping, media_dir=str(tmp_path))
        assert "old_good.png" in result
        assert "old_bad.png" not in result

    def test_all_valid(self, tmp_path, mw_mock):
        for name in ("a.png", "b.jpg"):
            (tmp_path / name).write_bytes(b"\x00" * 200)
        mapping = {"x.png": "a.png", "y.jpg": "b.jpg"}
        result = _filter_valid_filename_mapping(mapping, media_dir=str(tmp_path))
        assert len(result) == 2


# ──────────────────────────────────────────────────────────────────────
# _handle_operation_aborted
# ──────────────────────────────────────────────────────────────────────


class TestHandleOperationAborted:
    @patch("export_manager.aqt.utils.showInfo")
    def test_handles_operation_aborted(self, mock_show, mw_mock):
        err = OperationAbortedError("closed", phase="export")
        result = _handle_operation_aborted(err, "Export")
        assert result is True

    def test_returns_false_for_other_exceptions(self, mw_mock):
        err = ValueError("something else")
        assert _handle_operation_aborted(err, "Export") is False


class TestMediaReferenceConflictSafety:
    def test_stale_background_update_recomputes_against_latest_note(
        self, mw_mock, tmp_path
    ):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "new.mp3").write_bytes(b"x" * 100)
        mw_mock.col.media.dir.return_value = str(media_dir)

        note = MagicMock(id=42, mod=9, fields=["user edit [sound:old.mp3]"])
        mw_mock.col.get_note.return_value = note
        results = {
            "filename_mapping": {"old.mp3": "new.mp3"},
            "updates": [
                {
                    "note_id": 42,
                    "note_guid": "guid-42",
                    "fields": ["background snapshot [sound:new.mp3]"],
                    "mod": 8,
                    "old_filenames": ["old.mp3"],
                }
            ],
        }

        count, _ = _apply_media_reference_updates(results)

        assert count == 1
        assert note.fields == ["user edit [sound:new.mp3]"]
        mw_mock.col.update_notes.assert_called_once_with(notes=[note])


class TestMediaUploadFailureSemantics:
    @pytest.mark.asyncio
    async def test_batch_failures_are_deduplicated_and_reported(self, mw_mock):
        files = [{"filename": f"file-{i}.mp3"} for i in range(101)]
        upload = AsyncMock(
            side_effect=[
                {
                    "success": True,
                    "uploaded": 3,
                    "existing": 2,
                    "failed_filenames": ["file-0.mp3", "file-0.mp3"],
                    "errors": ["first batch warning"],
                },
                {"success": False, "message": "second batch failed"},
            ]
        )
        callback = MagicMock()
        mw_mock.progress.want_cancel.return_value = False

        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload(
                "deck-hash",
                "operation-id",
                files,
                {file["filename"]: f"/media/{file['filename']}" for file in files},
                progress_callback_wrapper=callback,
            )

        assert result["uploaded"] == 3
        assert result["existing"] == 2
        assert result["failed"] == 2
        assert result["failed_filenames"] == ["file-0.mp3", "file-100.mp3"]
        assert "second batch failed" in result["errors"][-1]
        callback.assert_called_with(1.0)
        assert upload.await_count == 2

    @pytest.mark.asyncio
    async def test_cancellation_stops_before_next_upload_batch(self, mw_mock):
        files = [{"filename": f"file-{i}.mp3"} for i in range(101)]
        upload = AsyncMock(
            return_value={
                "success": True,
                "uploaded": 1,
                "existing": 0,
                "failed_filenames": [],
                "errors": [],
            }
        )
        mw_mock.progress.want_cancel.return_value = True

        with patch.object(
            export_manager.main.media_manager, "upload_media_bulk", upload
        ):
            result = await handle_media_upload(
                "deck-hash",
                "operation-id",
                files,
                {},
            )

        assert result["cancelled"] is True
        assert result["uploaded"] == 0
        assert upload.await_count == 1


class TestExportSafetyGuards:
    def test_backup_failure_aborts_before_deck_preparation(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "Deck", "dyn": False}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=("token", False)),
            patch(
                "export_manager.create_backup",
                side_effect=BackupFailedError("disk failure"),
            ),
            patch.object(
                export_manager.deck_initializer, "from_collection"
            ) as from_collection,
            patch("export_manager.aqt.utils.showWarning") as warning,
        ):
            handle_export(7)

        from_collection.assert_not_called()
        assert "No changes have been made" in warning.call_args.args[0]
        assert warning.call_args.kwargs["title"] == "Backup Failed"

    def test_filtered_deck_is_rejected_before_export(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "Filtered", "dyn": True}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.aqt.utils.showInfo") as info,
        ):
            handle_export(7)

        assert "Filtered decks cannot be published" in info.call_args.args[0]

    def test_missing_login_is_rejected_before_backup(self, mw_mock):
        mw_mock.col.decks.get.return_value = {"id": 7, "name": "Deck", "dyn": False}
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.get_maintainer_data", return_value=(None, False)),
            patch("export_manager.create_backup") as backup,
            patch("export_manager.aqt.utils.showWarning") as warning,
        ):
            handle_export(7)

        backup.assert_not_called()
        assert "logged in" in warning.call_args.args[0]

    def test_suggest_notes_rejects_empty_selection(self, mw_mock):
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.aqt.utils.showWarning") as warning,
        ):
            suggest_notes([], 5)

        assert "No notes selected" in warning.call_args.args[0]

    def test_suggest_notes_rejects_missing_note(self, mw_mock):
        mw_mock.col.get_note.return_value = None
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.aqt.utils.show_exception") as show_exception,
        ):
            suggest_notes([42], 5)

        assert show_exception.called or mw_mock.col.get_note.called

    def test_suggest_notes_rejects_large_selection(self, mw_mock):
        mw_mock.col.get_note.side_effect = lambda note_id: MagicMock()
        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch("export_manager.aqt.utils.showInfo") as info,
        ):
            suggest_notes(list(range(2001)), 5)

        assert "2,000 notes or fewer" in info.call_args.args[0]

    def test_valid_suggestion_starts_background_preparation(self, mw_mock):
        note = MagicMock()
        note.cards.return_value = [MagicMock()]
        mw_mock.col.get_note.return_value = note

        with (
            patch("export_manager.is_collection_available", return_value=True),
            patch(
                "export_manager.get_deck_hash_from_card", return_value=("hash", None)
            ),
            patch("export_manager.get_did_from_hash", return_value=7),
            patch("export_manager.QueryOp") as query_op,
        ):
            suggest_notes([11, 12], 5)

        query_op.assert_called_once()
        prepared_op = query_op.return_value.failure.return_value
        prepared_op.with_progress.assert_called_once_with("Preparing deck data...")
        prepared_op.run_in_background.assert_called_once()


class TestMaintainerAuthentication:
    def test_validates_and_refreshes_expired_token(self):
        response_expired = MagicMock(status_code=200, text="false")
        response_valid = MagicMock(status_code=200, text="true")
        with (
            patch.object(
                export_manager.auth_manager, "get_token", side_effect=["old", "new"]
            ),
            patch.object(
                export_manager.auth_manager, "get_auto_approve", return_value=True
            ),
            patch(
                "api_client.api_client.post_empty",
                side_effect=[response_expired, response_valid],
            ),
            patch.object(
                export_manager.auth_manager, "refresh_token", return_value=True
            ),
        ):
            token, auto_approve = get_maintainer_data("deck-hash")

        assert token == "new"
        assert auto_approve is True

    def test_no_token_skips_remote_validation(self):
        with (
            patch.object(export_manager.auth_manager, "get_token", return_value=None),
            patch.object(
                export_manager.auth_manager, "get_auto_approve", return_value=False
            ),
            patch("api_client.api_client.post_empty") as post_empty,
        ):
            result = get_maintainer_data("deck-hash")

        assert result == (None, False)
        post_empty.assert_not_called()


class TestMediaUploadResultHandling:
    def test_cancelled_upload_reports_counts_and_does_not_rate_silently(self, mw_mock):
        with (
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.capture_media_message") as capture,
            patch("export_manager.ask_for_rating") as rate,
        ):
            _handle_media_upload_result(
                {
                    "uploaded": 2,
                    "existing": 3,
                    "failed": 1,
                    "failed_filenames": ["bad.mp3"],
                    "errors": ["timeout"],
                    "cancelled": True,
                    "silent": False,
                }
            )

        assert "2 files uploaded" in warning.call_args.args[0]
        assert warning.call_args.kwargs["title"] == "Upload Cancelled"
        capture.assert_called_once()
        rate.assert_called_once()
        mw_mock.reset.assert_called_once()

    def test_failed_upload_without_filename_list_uses_warning_summary(self, mw_mock):
        with (
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager.capture_media_message"),
            patch("export_manager.ask_for_rating"),
        ):
            _handle_media_upload_result(
                {
                    "uploaded": 1,
                    "existing": 0,
                    "failed": 2,
                    "failed_filenames": [],
                    "errors": ["one", "two", "three", "four"],
                    "silent": False,
                }
            )

        message = warning.call_args.args[0]
        assert "2 files failed" in message
        assert "...and 1 more errors" in message
        assert warning.call_args.kwargs["title"] == "Media Upload Summary"

    def test_successful_silent_upload_does_not_show_user_notification(self, mw_mock):
        with (
            patch("export_manager.aqt.utils.tooltip") as tooltip,
            patch("export_manager.capture_media_message") as capture,
            patch("export_manager.ask_for_rating") as rate,
        ):
            _handle_media_upload_result(
                {
                    "uploaded": 4,
                    "existing": 1,
                    "failed": 0,
                    "errors": [],
                    "silent": True,
                }
            )

        tooltip.assert_not_called()
        capture.assert_not_called()
        rate.assert_not_called()
        mw_mock.reset.assert_not_called()

    def test_failed_upload_with_filenames_opens_failure_details(self, mw_mock):
        dialog = MagicMock()
        qt_widgets = {
            name: MagicMock()
            for name in (
                "QApplication",
                "QDialog",
                "QVBoxLayout",
                "QGroupBox",
                "QLabel",
                "QHBoxLayout",
                "QWidget",
                "QToolButton",
                "QLineEdit",
                "QPushButton",
                "QListWidget",
            )
        }
        qt_widgets["QApplication"].focusWidget.return_value = None
        qt_widgets["QDialog"].return_value = dialog
        dialog.height.return_value = 100
        dialog.width.return_value = 500
        dialog.sizeHint.return_value.height.return_value = 100
        qt_widgets["QListWidget"].return_value.count.return_value = 1
        qt_widgets["QListWidget"].return_value.sizeHintForRow.return_value = 18

        patches = [
            patch.object(export_manager, name, value)
            for name, value in qt_widgets.items()
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(patch.object(export_manager, "Qt", MagicMock()))
            stack.enter_context(patch("export_manager.capture_media_message"))
            stack.enter_context(patch("export_manager.ask_for_rating"))
            _handle_media_upload_result(
                {
                    "uploaded": 1,
                    "existing": 0,
                    "failed": 1,
                    "failed_filenames": ["broken.mp3"],
                    "errors": ["upload failed"],
                    "silent": False,
                }
            )

        dialog.show.assert_called_once()
        dialog.exec.assert_called_once()
        qt_widgets["QListWidget"].return_value.addItems.assert_called()


class TestMediaOperationWrappers:
    def test_start_media_upload_without_media_finishes_and_calls_callback(
        self, mw_mock
    ):
        callback = MagicMock()
        _start_media_upload(None, callback)

        mw_mock.progress.finish.assert_called_once()
        callback.assert_called_once()
        assert callback.call_args.args[0]["silent"] is True

    def test_media_optimization_empty_input_does_not_start_async_work(self):
        with patch("export_manager._sync_run_async") as run_async:
            assert _sync_optimize_media_and_update_refs([]) == ({}, [], {})
        run_async.assert_not_called()

    def test_media_optimization_preserves_abort_error(self):
        abort = OperationAbortedError("closed", phase="media_optimization_start")
        with patch("export_manager.check_collection_or_abort", side_effect=abort):
            with pytest.raises(OperationAbortedError):
                _sync_optimize_media_and_update_refs([("old.mp3", "guid")])

    def test_sync_upload_wrapper_returns_upload_result_and_completes_progress(
        self, mw_mock
    ):
        mw_mock.taskman.run_on_main.side_effect = lambda callback: callback()
        upload_result = {
            "uploaded": 2,
            "existing": 1,
            "failed": 0,
            "errors": [],
        }
        with (
            patch("export_manager._sync_run_async", return_value=upload_result),
            patch("export_manager.complete_media_progress") as complete,
        ):
            result = _sync_handle_media_upload(
                "hash", "operation", [{"filename": "a.mp3"}], {}, False
            )

        assert result == upload_result
        complete.assert_called_once_with(True, "Uploaded 2 files (1 already existed)")

    def test_sync_upload_wrapper_returns_error_result_when_upload_fails(self, mw_mock):
        mw_mock.taskman.run_on_main.side_effect = lambda callback: callback()
        with (
            patch(
                "export_manager._sync_run_async", side_effect=RuntimeError("offline")
            ),
            patch("export_manager.complete_media_progress") as complete,
        ):
            result = _sync_handle_media_upload(
                "hash", "operation", [{"filename": "a.mp3"}], {}, True
            )

        assert result["failed"] == 1
        assert "offline" in result["errors"][0]
        complete.assert_called_once_with(False, "Upload failed: offline")


class TestDeckCreationCallbacks:
    def test_successful_creation_without_media_persists_subscription_and_finishes(
        self, mw_mock
    ):
        mw_mock.addonManager.getConfig = MagicMock(return_value={})
        mw_mock.addonManager.writeConfig = MagicMock()
        with (
            patch("export_manager.mw", mw_mock),
            patch("export_manager.aqt.utils.askUser", return_value=False),
            patch("export_manager.aqt.utils.showInfo") as info,
            patch("export_manager._on_export_media_uploaded") as uploaded,
        ):
            _on_export_deck_created(
                {"status": 1, "message": "new-hash", "bulk_operation_id": "op"},
                17,
                [],
                {},
            )

        stored = mw_mock.addonManager.writeConfig.call_args.args[1]
        assert stored["new-hash"]["deckId"] == 17
        assert "new-hash" in info.call_args.args[0]
        uploaded.assert_called_once()

    def test_failed_creation_shows_failure_without_starting_media_upload(self, mw_mock):
        with (
            patch("export_manager.mw", mw_mock),
            patch("export_manager.aqt.utils.showWarning") as warning,
            patch("export_manager._start_media_upload") as start_upload,
        ):
            _on_export_deck_created(
                {"status": 0, "message": "maintainer rejected"},
                17,
                [{"filename": "a.mp3"}],
                {"a.mp3": "/a.mp3"},
            )

        assert "maintainer rejected" in warning.call_args.args[0]
        start_upload.assert_not_called()
