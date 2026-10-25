"""Mutation-hardening tests for deck lifecycle and collection safety paths."""

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import create_mock_collection

from crowd_anki.representation import deck as deck_module
from crowd_anki.representation.deck import Deck, DeckMetadata
from crowd_anki.representation.note_model import NotetypeSaveError


def _deck():
    deck = Deck(MagicMock(), {"name": "Root", "crowdanki_uuid": "d1", "id": 1})
    deck.metadata = DeckMetadata(deck_configs={}, models={})
    return deck


class TestDeckLifecycle:
    def test_success_wrapper_starts_download_only_for_missing_media(self):
        deck = _deck()
        result = (3, {"missing_files": ["a.mp3"], "deck_hash": "hash"})

        with (
            patch.object(deck, "_start_media_download_from_main_thread") as start,
            patch.object(deck, "on_success") as success,
        ):
            deck.on_success_wrapper(result)

        start.assert_called_once_with("hash", ["a.mp3"], result[1])
        success.assert_not_called()

    def test_success_wrapper_finishes_without_media_download(self):
        deck = _deck()
        result = (3, {"success": True})

        with (
            patch.object(deck, "_start_media_download_from_main_thread") as start,
            patch.object(deck, "on_success") as success,
        ):
            deck.on_success_wrapper(result)

        start.assert_not_called()
        success.assert_called_once_with(3, result[1])

    def test_format_note_change_summary_covers_plurality_and_empty(self):
        assert deck_module.Deck._format_note_change_summary(0, 0) == "No notes changed."
        assert deck_module.Deck._format_note_change_summary(1, 1) == (
            "1 note added, 1 note updated."
        )
        assert deck_module.Deck._format_note_change_summary(2, 3) == (
            "2 notes added, 3 notes updated."
        )

    def test_auth_expired_media_result_is_not_reported_twice(self):
        deck = _deck()
        with (
            patch.object(deck_module.aqt.utils, "tooltip") as tooltip,
            patch.object(deck_module.aqt.utils, "showWarning") as warning,
        ):
            deck.on_media_download_done({"success": False, "auth_expired": True})

        tooltip.assert_not_called()
        warning.assert_not_called()

    def test_media_error_reports_warning(self):
        deck = _deck()
        with patch.object(deck_module.aqt.utils, "showWarning") as warning:
            deck.on_media_download_done({"success": False, "message": "missing"})

        warning.assert_called_once()
        assert "missing" in warning.call_args.args[0]

    def test_a_result_without_success_is_treated_as_an_error(self):
        deck = _deck()
        with patch.object(deck_module.aqt.utils, "showWarning") as warning:
            deck.on_media_download_done({"message": "partial"})

        warning.assert_called_once()
        assert "partial" in warning.call_args.args[0]


class TestEmptyCardCleanup:
    def test_keeps_first_card_for_note_that_will_be_deleted(self):
        col = MagicMock()
        report = MagicMock()
        report.notes = [
            MagicMock(will_delete_note=True, card_ids=[1, 2, 3]),
            MagicMock(will_delete_note=False, card_ids=[4, 5]),
        ]
        col.get_empty_cards.return_value = report

        Deck._delete_empty_cards(col)

        col.remove_cards_and_orphaned_notes.assert_called_once_with([2, 3, 4, 5])

    def test_empty_card_report_without_notes_does_nothing(self):
        col = MagicMock()
        col.get_empty_cards.return_value.notes = []

        Deck._delete_empty_cards(col)

        col.remove_cards_and_orphaned_notes.assert_not_called()


class TestDeckCleanupAndProgress:
    def test_delete_empty_deck_reparents_filtered_children(self):
        deck = _deck()
        anki_decks = MagicMock()
        anki_decks.children.side_effect = [
            [("Root::Empty", 20)],
            [("Root::Empty::Filtered", 21)],
        ]
        anki_decks.is_filtered.side_effect = lambda did: did == 21
        anki_decks.card_count.return_value = 0
        anki_decks.immediate_parent.return_value = "Root"
        anki_decks.id_for_name.return_value = 1
        anki_decks.remove.return_value.count = 1

        with patch.object(deck_module.aqt.mw.col, "decks", anki_decks):
            deck.root_deck_id = 1
            deck.delete_empty_subdecks()

        anki_decks.reparent.assert_called_once_with([21], 1)
        anki_decks.remove.assert_called_once_with(dids=[20])

    def test_unified_progress_tracker_scales_note_progress(self):
        deck = _deck()
        progress = deck_module.aqt.mw.progress
        progress.busy.return_value = True
        deck_module.aqt.mw.taskman.run_on_main.side_effect = lambda callback: callback()

        tracker = deck.create_unified_progress_tracker(10)
        tracker.update_notes_progress(5)

        progress.update.assert_called_with(
            label="Processing notes...", value=450, max=1000
        )

    def test_unified_progress_tracker_marks_media_phase(self):
        deck = _deck()
        progress = deck_module.aqt.mw.progress
        progress.busy.return_value = True
        deck_module.aqt.mw.taskman.run_on_main.side_effect = lambda callback: callback()

        tracker = deck.create_unified_progress_tracker(10)
        tracker.update_media_progress(2, downloading_count=4)

        progress.update.assert_called_with(
            label="Downloading media: 2 / 4", value=950, max=1000
        )


class TestMediaDownloadDelegation:
    def test_empty_missing_file_list_completes_without_query_operation(self):
        deck = _deck()

        with patch.object(deck, "on_success") as success:
            deck._start_media_download_from_main_thread("hash", [], {"success": True})

        success.assert_called_once_with(0, {"success": True})

    def test_expired_session_stops_media_operation_before_download(self):
        deck = _deck()
        media_result = {"success": True}
        query_op = MagicMock()
        response = MagicMock(status_code=403, text="false")

        with (
            patch.object(
                deck_module, "QueryOp", return_value=query_op
            ) as query_op_type,
            patch.object(deck_module.auth_manager, "is_logged_in", return_value=True),
            patch("api_client.api_client") as client,
        ):
            client.post_empty.return_value = response
            deck._start_media_download_from_main_thread(
                "hash", ["missing.mp3"], media_result
            )
            operation = query_op_type.call_args.kwargs["op"]
            result = operation(None)

        assert result["success"] is False
        assert result["auth_expired"] is True
        assert result["message"] == "Session expired"
        query_op.run_in_background.assert_called_once()


class TestModelMediaExtraction:
    def test_models_without_ids_are_skipped(self):
        deck = _deck()
        deck.metadata = DeckMetadata(
            deck_configs={},
            models={"missing": MagicMock(anki_dict={"name": "Missing"})},
        )

        with patch.object(deck_module.logger, "warning") as warning:
            assert deck._get_media_from_models() == set()

        warning.assert_called()

    def test_model_media_provider_result_is_returned(self):
        deck = _deck()
        model = MagicMock(anki_dict={"id": 7, "name": "Basic"})
        provider = MagicMock()
        provider.get_files.return_value = {"template.png"}
        deck.metadata = DeckMetadata(deck_configs={}, models={"m": model})

        with patch.object(
            deck, "file_provider_supplier", return_value=provider
        ) as supplier:
            assert deck._get_media_from_models() == {"template.png"}

        supplier.assert_called_once_with(deck.collection, [7])


class TestNotetypeLifecycle:
    def test_handle_notetype_changes_runs_all_phases_and_combines_failures(self):
        deck = _deck()
        deck.metadata = DeckMetadata(deck_configs={}, models={"m": MagicMock()})
        collection = create_mock_collection()
        failures = []

        with (
            patch.object(
                deck, "_handle_notetype_duplicates", return_value=True
            ) as duplicates,
            patch.object(
                deck, "_create_and_update_notetypes", return_value=False
            ) as create,
            patch.object(
                deck, "_change_existing_note_types", return_value=True
            ) as change,
            patch.object(
                deck, "_validate_notetype_operations", return_value=True
            ) as validate,
        ):
            assert deck.handle_notetype_changes(collection) is False

        duplicates.assert_called_once()
        create.assert_called_once()
        change.assert_called_once()
        validate.assert_called_once()

    def test_handle_notetype_changes_without_models_is_successful_noop(self):
        deck = _deck()
        collection = create_mock_collection()

        with patch.object(deck_module.logger, "warning") as warning:
            assert deck.handle_notetype_changes(collection) is True

        warning.assert_called_once()

    def test_handle_notetype_changes_does_not_swallow_a_save_error(self):
        """Without a field mapping the import must abort, not continue."""
        deck = _deck()
        deck.metadata = DeckMetadata(deck_configs={}, models={"m": MagicMock()})
        collection = create_mock_collection()

        with patch.object(
            deck,
            "_create_and_update_notetypes",
            side_effect=NotetypeSaveError("mapping lost"),
        ):
            with pytest.raises(NotetypeSaveError):
                deck.handle_notetype_changes(collection)

    def test_protected_fields_builds_name_and_index_caches(self):
        deck = _deck()
        model = MagicMock(
            anki_dict={
                "name": "Basic",
                "flds": [{"name": "Front"}, {"name": "Back"}],
            }
        )
        deck.metadata = DeckMetadata(deck_configs={}, models={"m": model})
        response = MagicMock(status_code=200)
        response.json.return_value = [{"name": "Basic", "fields": [{"name": "Back"}]}]

        with patch("api_client.api_client") as client:
            client.get.return_value = response
            result = deck.get_protected_fields("hash")

        assert result["model_name_to_fields"] == {"Basic": ["Back"]}
        assert result["model_name_to_indices"] == {"Basic": [1]}
