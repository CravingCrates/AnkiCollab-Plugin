"""Tests for the bulk deck import in crowd_anki/representation/deck.py.

``Deck.save_decks_and_notes_bulk`` is the single entry point that writes an
imported deck into the user's collection.  It creates a temporary deck, moves
notes into the real deck tree, downloads missing media and then has to clean up
after itself.  The things worth testing are the ones that can leave a
collection in a bad state:

* validation refusing a half-configured import,
* a temporary deck surviving a failure (notes stranded in a hidden deck),
* media files being classified as present when they are not,
* path-traversal entries from the server reaching the filesystem,
* an abort or an unexpected error being swallowed instead of reported.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import create_mock_collection

from crowd_anki.representation import deck as deck_module
from crowd_anki.representation.deck import Deck, DeckImportCancelled
from crowd_anki.representation.note import Note
from utils import OperationAbortedError


class _Note:
    def __init__(self, uuid="n1"):
        self._uuid = uuid
        self.note_model_uuid = "m1"
        self.anki_object_dict = {"guid": uuid, "fields": [], "tags": []}

    def get_uuid(self):
        return self._uuid

    def handle_import_config_changes(self, *args, **kwargs):
        pass


def _make_deck(notes=None):
    deck = Deck(lambda *a, **kw: None, {"name": "Server Deck", "id": 1})
    deck.notes = [_Note()] if notes is None else notes
    deck.metadata = MagicMock()
    deck.metadata.models = {"m1": MagicMock()}
    return deck


def _cfg(**overrides):
    cfg = MagicMock()
    cfg.home_deck = overrides.pop("home_deck", "Home")
    cfg.new_notes_home_deck = overrides.pop("new_notes_home_deck", None)
    cfg.deck_hash = overrides.pop("deck_hash", "hash1")
    cfg.keep_empty_subdecks = overrides.pop("keep_empty_subdecks", True)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _parts(
    count=2, temp_id=99, temp_name="_ankicollab_import_x", root="Home", media=None
):
    return (
        patch.object(
            Deck, "_bulk_process_all_notes", return_value=(count, temp_id, temp_name)
        ),
        patch.object(Deck, "_create_deck_structure", return_value=root),
        patch.object(Note, "_move_notes_to_decks"),
        patch.object(Deck, "_cleanup_temp_deck"),
        patch.object(Deck, "get_media_file_list", return_value=media or set()),
    )


def _collect_impl(
    all_notes, note_to_deck_map, parent_name, home_deck, server_root_name=None
):
    """Deterministic stand-in for the real recursive collector."""
    all_notes.append(_Note("n1"))
    note_to_deck_map["n1"] = home_deck or "Home"


def _collect_patcher():
    return patch.object(Deck, "_collect_all_notes", side_effect=_collect_impl)


def _run(deck, col, cfg, progress, parts=None, extra=()):
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in (parts or _parts())]
        extras = [stack.enter_context(p) for p in extra]
        result = deck.save_decks_and_notes_bulk(col, progress, cfg)
    return [result, *mocks, *extras]


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────


class TestSaveBulkValidation:
    @pytest.mark.parametrize(
        "collection,config,metadata",
        [
            (None, "cfg", "meta"),
            ("col", None, "meta"),
            ("col", "cfg", None),
        ],
    )
    def test_an_incomplete_import_is_refused(self, collection, config, metadata):
        deck = _make_deck()
        deck.metadata = metadata
        col = create_mock_collection() if collection else None
        cfg = _cfg() if config else None

        with pytest.raises(ValueError):
            deck.save_decks_and_notes_bulk(col, MagicMock(), cfg)

    def test_keep_empty_subdecks_follows_the_import_config(self):
        col = create_mock_collection()

        keep = _make_deck()
        _run(keep, col, _cfg(keep_empty_subdecks=True), MagicMock())
        assert keep.keep_empty_subdecks is True

        drop = _make_deck()
        _run(drop, col, _cfg(keep_empty_subdecks=False), MagicMock())
        assert drop.keep_empty_subdecks is False

    def test_an_import_config_without_the_flag_defaults_to_dropping_them(self):
        cfg = MagicMock(spec=["home_deck", "deck_hash", "new_notes_home_deck"])
        cfg.home_deck = "Home"
        cfg.new_notes_home_deck = None
        cfg.deck_hash = "h"
        deck = _make_deck()

        _run(deck, create_mock_collection(), cfg, MagicMock())

        assert deck.keep_empty_subdecks is False

    def test_a_deck_without_notes_does_not_start_the_import(self):
        deck = _make_deck(notes=[])
        progress = MagicMock()
        with (
            patch.object(Deck, "_collect_all_notes"),
            patch.object(Deck, "_bulk_process_all_notes") as bulk,
        ):
            result = deck.save_decks_and_notes_bulk(
                create_mock_collection(), progress, _cfg()
            )

        assert result == (0, {"success": True, "downloaded": 0, "skipped": 0})
        bulk.assert_not_called()
        progress.set_phase_label.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────


class TestSaveBulkOrchestration:
    def _env(self):
        return _make_deck(), create_mock_collection(), _cfg(), MagicMock()

    def test_the_collection_is_re_checked_before_each_write_phase(self):
        """A collection closed mid-import must abort before the next write."""
        deck, col, cfg, progress = self._env()
        with patch.object(
            deck_module, "check_collection_or_abort", return_value=col
        ) as check:
            _run(deck, col, cfg, progress)

        assert [c.args[0] for c in check.call_args_list] == [
            "initialization",
            "note_processing",
            "deck_structure_creation",
            "note_organization",
        ]

    def test_notes_are_collected_for_the_home_deck(self):
        deck, col, cfg, progress = self._env()
        with patch.object(Deck, "_collect_all_notes") as collect:
            _run(deck, col, cfg, progress)

        assert collect.call_args.args[2] == ""
        assert collect.call_args.args[3] == "Home"

    def test_the_deck_tree_is_built_under_the_server_root_name(self):
        deck, col, cfg, progress = self._env()
        result = _run(deck, col, cfg, progress)
        assert result[2].call_args.args == (col, "", "Home", "Server Deck")

    def test_a_deck_without_a_name_still_gets_a_root(self):
        deck, col, cfg, progress = self._env()
        deck.anki_dict = {"id": 1}
        result = _run(deck, col, cfg, progress, extra=[_collect_patcher()])
        assert result[2].call_args.args == (col, "", "Home", "Unknown Deck")

    def test_notes_are_moved_using_the_mapping_built_during_collection(self):
        deck, col, cfg, progress = self._env()
        result = _run(deck, col, cfg, progress, extra=[_collect_patcher()])
        move = result[3]

        assert move.call_args.args[0] is col
        assert move.call_args.args[1] == {"n1": "Home"}
        assert move.call_args.args[2] == 99  # the temp deck created for this import

    def test_the_resulting_root_deck_is_recorded_on_the_deck(self):
        deck, col, cfg, progress = self._env()
        _run(deck, col, cfg, progress)
        assert deck.root_deck_id == col.decks.id("Home")

    def test_the_temporary_deck_is_removed_after_a_successful_import(self):
        deck, col, cfg, progress = self._env()
        result = _run(deck, col, cfg, progress)

        cleanup = result[4]
        cleanup.assert_called_once()
        assert cleanup.call_args.args[:3] == (col, 99, "_ankicollab_import_x")

    def test_no_temporary_deck_means_no_cleanup(self):
        deck, col, cfg, progress = self._env()
        result = _run(deck, col, cfg, progress, parts=list(_parts(temp_id=None)))
        result[4].assert_not_called()

    def test_the_note_count_and_media_result_are_returned(self):
        deck, col, cfg, progress = self._env()
        count, media = _run(deck, col, cfg, progress)[0]
        assert count == 2
        assert media == {"success": True, "downloaded": 0, "skipped": 0}


# ──────────────────────────────────────────────────────────────────────
# Media
# ──────────────────────────────────────────────────────────────────────


class TestSaveBulkMedia:
    def _env(self, media_dir):
        return (
            _make_deck(),
            create_mock_collection(media_dir=str(media_dir)),
            _cfg(),
            MagicMock(),
        )

    def test_media_is_collected_from_models_and_child_decks(self, tmp_path):
        deck, col, cfg, progress = self._env(tmp_path)
        result = _run(deck, col, cfg, progress)
        result[5].assert_called_once_with(data_from_models=True, include_children=True)

    def test_files_that_are_not_on_disk_are_queued_for_download(self, tmp_path):
        (tmp_path / "present.mp3").write_bytes(b"x")
        deck, col, cfg, progress = self._env(tmp_path)
        result = _run(
            deck,
            col,
            cfg,
            progress,
            parts=list(_parts(media={"present.mp3", "missing.mp3"})),
        )
        _count, media = result[0]

        assert media["missing_files"] == ["missing.mp3"]
        assert media["skipped"] == 1
        assert media["deck_hash"] == "hash1"
        assert media["downloaded"] == 0

    def test_when_everything_is_present_nothing_is_queued(self, tmp_path):
        (tmp_path / "present.mp3").write_bytes(b"x")
        deck, col, cfg, progress = self._env(tmp_path)
        _count, media = _run(
            deck, col, cfg, progress, parts=list(_parts(media={"present.mp3"}))
        )[0]

        assert media == {"success": True, "downloaded": 0, "skipped": 1}
        assert "missing_files" not in media

    def test_entries_that_escape_the_media_folder_are_dropped(self, tmp_path):
        deck, col, cfg, progress = self._env(tmp_path)
        _count, media = _run(
            deck,
            col,
            cfg,
            progress,
            parts=list(
                _parts(
                    media={
                        "safe.png",
                        "nested/unsafe.png",
                        "../escape.png",
                        "..",
                        "a..b.png",
                    }
                )
            ),
        )[0]

        assert media["missing_files"] == ["safe.png"]

    def test_a_deck_without_media_reports_an_empty_result(self, tmp_path):
        deck, col, cfg, progress = self._env(tmp_path)
        _count, media = _run(deck, col, cfg, progress)[0]
        assert media == {"success": True, "downloaded": 0, "skipped": 0}


# ──────────────────────────────────────────────────────────────────────
# Failure handling
# ──────────────────────────────────────────────────────────────────────


class TestSaveBulkFailureHandling:
    def _env(self):
        return _make_deck(), create_mock_collection(), _cfg(), MagicMock()

    def _abort_run(self, abort, extra=()):
        deck, col, cfg, progress = self._env()
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(Deck, "_bulk_process_all_notes", side_effect=abort)
            )
            stack.enter_context(patch.object(Deck, "_cleanup_temp_deck"))
            stack.enter_context(_collect_patcher())
            for patcher in extra:
                stack.enter_context(patcher)
            with pytest.raises(OperationAbortedError) as excinfo:
                deck.save_decks_and_notes_bulk(col, progress, cfg)
        return excinfo.value

    def test_an_abort_is_propagated_unchanged(self, mw_mock):
        abort = OperationAbortedError("closed", phase="note_processing")
        assert self._abort_run(abort) is abort

    def test_a_user_cancel_is_propagated_unchanged(self, mw_mock):
        """A cancel is not an error; the caller must be able to tell it apart."""
        deck, col, cfg, progress = self._env()
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    Deck,
                    "_bulk_process_all_notes",
                    side_effect=DeckImportCancelled(),
                )
            )
            stack.enter_context(_collect_patcher())
            with pytest.raises(DeckImportCancelled):
                deck.save_decks_and_notes_bulk(col, progress, cfg)

    def test_a_failure_is_wrapped_as_an_import_error(self, mw_mock):
        """Callers distinguish an import failure from an abort by the type."""
        deck, col, cfg, progress = self._env()
        parts = list(_parts())
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in parts]
            mocks[2].side_effect = RuntimeError("boom")  # _move_notes_to_decks
            with pytest.raises(ImportError):
                deck.save_decks_and_notes_bulk(col, progress, cfg)

    def test_a_failure_is_reported_before_it_is_reraised(self, mw_mock):
        deck, col, cfg, progress = self._env()
        parts = list(_parts())
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in parts]
            mocks[2].side_effect = RuntimeError("boom")
            sentry = stack.enter_context(patch.object(deck_module, "sentry_sdk"))
            with pytest.raises(ImportError):
                deck.save_decks_and_notes_bulk(col, progress, cfg)

        sentry.set_context.assert_called_once()
        sentry.capture_exception.assert_called_once()

    def test_the_import_context_carries_the_new_notes_home_deck(self, mw_mock):
        deck = _make_deck()
        col = create_mock_collection()
        cfg = _cfg(new_notes_home_deck="NewDeck")
        parts = list(_parts())
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in parts]
            mocks[2].side_effect = RuntimeError("boom")
            sentry = stack.enter_context(patch.object(deck_module, "sentry_sdk"))
            with pytest.raises(ImportError):
                deck.save_decks_and_notes_bulk(col, MagicMock(), cfg)

        assert sentry.set_context.call_args.args[1]["new_notes_home_deck"] == "NewDeck"

    def test_the_import_context_carries_the_new_notes_home_deck(self, mw_mock):
        deck = _make_deck()
        col = create_mock_collection()
        cfg = _cfg(new_notes_home_deck="NewDeck")
        parts = list(_parts())
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in parts]
            mocks[2].side_effect = RuntimeError("boom")
            sentry = stack.enter_context(patch.object(deck_module, "sentry_sdk"))
            with pytest.raises(ImportError):
                deck.save_decks_and_notes_bulk(col, MagicMock(), cfg)

        assert sentry.set_context.call_args.args[1]["new_notes_home_deck"] == "NewDeck"

    def test_the_temporary_deck_is_removed_even_when_the_import_fails(self, mw_mock):
        """Notes must never be left stranded in the hidden import deck."""
        deck, col, cfg, progress = self._env()
        parts = list(_parts())
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in parts]
            mocks[2].side_effect = RuntimeError("boom")
            with pytest.raises(ImportError):
                deck.save_decks_and_notes_bulk(col, progress, cfg)

        cleanup = mocks[3]
        cleanup.assert_called_once()
        assert cleanup.call_args.args[:3] == (col, 99, "_ankicollab_import_x")

    def test_a_cleanup_failure_does_not_mask_the_original_error(self, mw_mock):
        deck, col, cfg, progress = self._env()
        parts = list(_parts())
        parts[3] = patch.object(
            Deck, "_cleanup_temp_deck", side_effect=RuntimeError("locked")
        )
        with ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in parts]
            mocks[2].side_effect = RuntimeError("boom")
            sentry = stack.enter_context(patch.object(deck_module, "sentry_sdk"))
            with pytest.raises(ImportError):
                deck.save_decks_and_notes_bulk(col, progress, cfg)

        # Both the import failure and the cleanup failure are reported, and the
        # ImportError still surfaces.
        assert sentry.capture_exception.call_count == 2
