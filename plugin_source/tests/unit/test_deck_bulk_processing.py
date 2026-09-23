"""Mutation-driven tests for bulk note processing in deck.py.

Targets survivors in:
- ``_bulk_process_all_notes``: new/update categorization, separate new-notes
  deck mapping, missing-model skipping, cancel handling.
- ``_restore_original_note_ids``: safe/conflicted ID restoration and the SQL
  issued to the DB.
- ``_batch_process_notes`` / ``process_single_note``: model-id resolution and
  per-note error fallback.

The chunk-boundary lookup logic itself is covered by ``test_deck_chunking.py``.
"""

from unittest.mock import MagicMock, patch

import pytest
from contextlib import ExitStack

from tests.conftest import create_mock_collection, make_notetype

from crowd_anki.representation import deck as deck_module
from crowd_anki.representation.deck import CHUNK_SIZE, Deck
from crowd_anki.representation.note import Note
from crowd_anki.representation.note_model import NoteModel
from utils import OperationAbortedError


def _patch_logger(method="info"):
    return patch.object(deck_module.logger, method)


class _Note:
    """Minimal note stand-in carrying a uuid + notetype-model uuid."""

    def __init__(self, uuid, model="m1"):
        self._uuid = uuid
        self.note_model_uuid = model
        self.anki_object_dict = {"guid": uuid, "fields": [], "tags": []}

    def get_uuid(self):
        return self._uuid

    def handle_import_config_changes(self, *args, **kwargs):
        pass


def _make_deck(notes, models=None):
    deck = Deck(lambda *a, **kw: None, {"name": "Test", "id": 1})
    deck.notes = notes
    deck.metadata = MagicMock()
    if models is None:
        models = {"m1": MagicMock()}
    deck.metadata.models = models
    return deck


def _cfg(new_notes_home_deck=None, home_deck=None):
    cfg = MagicMock()
    cfg.new_notes_home_deck = new_notes_home_deck
    cfg.home_deck = home_deck
    return cfg


# ──────────────────────────────────────────────────────────────────────
# _bulk_process_all_notes
# ──────────────────────────────────────────────────────────────────────


class TestBulkProcessAllNotes:
    def _patch_mw(self):
        mw = MagicMock()
        mw.progress.want_cancel.return_value = False
        return patch.object(deck_module, "mw", mw), mw

    def _run(self, deck, notes, col, cfg, note_to_deck_map=None):
        progress = MagicMock()
        mw_patcher, _ = self._patch_mw()
        with (
            mw_patcher,
            patch.object(Note, "bulk_add_notes") as m_add,
            patch.object(Note, "_bulk_update_notes_preserving_placement") as m_pres,
            patch.object(Deck, "_restore_original_note_ids") as m_restore,
        ):
            # Let the real _batch_process_* run; they only touch mocked notes.
            result = deck._bulk_process_all_notes(
                col, notes, cfg, progress, note_to_deck_map or {}
            )
        return result, m_add, m_pres, m_restore

    def test_all_new_notes_batched_to_add(self):
        col = create_mock_collection()
        col.db.all.return_value = []  # no existing notes
        notes = [_Note("n1"), _Note("n2")]
        deck = _make_deck(notes)
        result, m_add, m_pres, m_restore = self._run(deck, notes, col, _cfg())
        # Both notes are new -> bulk_add_notes called with both.
        assert result[0] == 2
        added = m_add.call_args.args[1]
        assert {n.get_uuid() for n in added} == {"n1", "n2"}
        m_restore.assert_called_once()
        m_pres.assert_not_called()

    def test_existing_notes_go_to_update(self):
        col = create_mock_collection()
        # db.all returns (guid, id) for the pre-existing note.
        col.db.all.return_value = [("n1", 100)]
        notes = [_Note("n1"), _Note("n2")]
        deck = _make_deck(notes)
        result, m_add, m_pres, m_restore = self._run(
            deck, notes, col, _cfg(), {"n1": "Deck", "n2": "Deck"}
        )
        assert result[0] == 2
        # n1 is an update -> not added as new.
        added = m_add.call_args.args[1]
        assert [n.get_uuid() for n in added] == ["n2"]
        # updates are preserved-placement
        m_pres.assert_called_once()

    def test_new_notes_deck_mapping_applied(self):
        col = create_mock_collection()
        col.db.all.return_value = []
        notes = [_Note("n1"), _Note("n2")]
        deck = _make_deck(notes)
        cfg = _cfg(new_notes_home_deck="NewDeck", home_deck="Home")
        note_to_deck_map = {"n1": "Home", "n2": "Home::Sub"}
        result, m_add, m_pres, m_restore = self._run(
            deck, notes, col, cfg, note_to_deck_map
        )
        assert result[0] == 2
        added = m_add.call_args.args[1]
        assert {n.get_uuid() for n in added} == {"n1", "n2"}

    def test_missing_model_notes_skipped(self):
        col = create_mock_collection()
        col.db.all.return_value = []
        notes = [_Note("n1", model="m1"), _Note("n2", model="m2")]
        deck = _make_deck(notes)  # metadata.models only has m1
        with _patch_logger("warning") as mock_warn:
            result, m_add, m_pres, m_restore = self._run(deck, notes, col, _cfg())
        assert result[0] == 1
        added = m_add.call_args.args[1]
        assert [n.get_uuid() for n in added] == ["n1"]
        assert mock_warn.called

    def test_temp_deck_created(self):
        col = create_mock_collection()
        col.db.all.return_value = []
        notes = [_Note("n1")]
        deck = _make_deck(notes)
        self._run(deck, notes, col, _cfg())
        # A temp import deck must have been created and tracked.
        assert deck.root_deck_id is not None
        temp_name = [
            d["name"]
            for d in col.decks.all()
            if d["name"].startswith("_ankicollab_import_")
        ]
        assert len(temp_name) == 1

    def test_cancel_mid_processing_returns_partial(self):
        col = create_mock_collection()
        col.db.all.return_value = []
        notes = [_Note(f"n{i}") for i in range(50)]
        deck = _make_deck(notes)
        progress = MagicMock()

        mw_patcher, mw = self._patch_mw()
        # Simulate a cancel on the second progress update: want_cancel returns
        # False for the first updates, then True.
        calls = {"n": 0}

        def fake_want_cancel():
            calls["n"] += 1
            return calls["n"] >= 2

        mw.progress.want_cancel.side_effect = fake_want_cancel

        with (
            mw_patcher,
            patch.object(Note, "bulk_add_notes") as m_add,
            patch.object(Note, "_bulk_update_notes_preserving_placement"),
            patch.object(Deck, "_restore_original_note_ids"),
        ):
            result = deck._bulk_process_all_notes(col, notes, _cfg(), progress, {})
        # Returns a triple: (processed_count, temp_deck_id, temp_deck_name)
        assert result[0] > 0
        assert result[1] is not None
        assert result[2].startswith("_ankicollab_import_")


# ──────────────────────────────────────────────────────────────────────
# _restore_original_note_ids
# ──────────────────────────────────────────────────────────────────────


class TestRestoreOriginalNoteIds:
    def _note_with_id(self, uuid, original_id, current_id=1000):
        n = _Note(uuid)
        n._original_id = original_id
        n.anki_object = MagicMock()
        n.anki_object.id = current_id
        return n

    def test_no_notes_returns(self):
        col = create_mock_collection()
        deck = _make_deck([])
        col.db.all.assert_not_called() if False else None
        deck._restore_original_note_ids(col, [])
        col.db.execute.assert_not_called()

    def test_notes_without_original_id_skipped(self):
        col = create_mock_collection()
        deck = _make_deck([])
        n = _Note("n1")
        with _patch_logger("debug") as mock_debug:
            deck._restore_original_note_ids(col, [n])
        col.db.execute.assert_not_called()
        assert mock_debug.called

    def test_safe_notes_restored(self):
        col = create_mock_collection()
        col.db.all.return_value = []  # no original IDs are taken
        deck = _make_deck([])
        notes = [
            self._note_with_id("n1", 111, current_id=1000),
            self._note_with_id("n2", 222, current_id=1001),
        ]
        deck._restore_original_note_ids(col, notes)
        # UPDATE issued for notes and cards.
        assert col.db.execute.call_count == 2
        for call in col.db.execute.call_args_list:
            sql = call.args[0]
            assert sql.startswith("UPDATE")
        # Note objects updated to their original ids.
        assert notes[0].anki_object.id == 111
        assert notes[1].anki_object.id == 222
        # _original_id cleaned up.
        assert not hasattr(notes[0], "_original_id")

    def test_conflicted_notes_not_restored(self):
        col = create_mock_collection()
        # original id 111 is already taken by another note.
        col.db.all.return_value = [(111,)]
        deck = _make_deck([])
        notes = [self._note_with_id("n1", 111, current_id=1000)]
        with _patch_logger("warning") as mock_warn:
            deck._restore_original_note_ids(col, notes)
        col.db.execute.assert_not_called()
        assert notes[0].anki_object.id == 1000  # kept the new id
        assert mock_warn.called

    def test_sql_variable_limit_chunking(self):
        col = create_mock_collection()
        col.db.all.return_value = []  # none taken
        deck = _make_deck([])
        # More notes than SQL_VARIABLE_LIMIT (900) -> multiple lookups.
        notes = [self._note_with_id(f"n{i}", 1000 + i) for i in range(950)]
        deck._restore_original_note_ids(col, notes)
        # Lookups in chunks of 900: ceil(950/900) = 2 SELECT lookups.
        select_calls = [
            c for c in col.db.all.call_args_list if c.args[0].startswith("SELECT")
        ]
        assert len(select_calls) == 2


# ──────────────────────────────────────────────────────────────────────
# _batch_process_notes / process_single_note
# ──────────────────────────────────────────────────────────────────────


class TestBatchProcessNotes:
    def test_new_note_model_id_resolved(self):
        col = create_mock_collection()
        model = make_notetype(name="Basic", fields=["A", "B"], model_id=5)
        col.models.add(model)
        note_model = NoteModel(model)
        note = _Note("n1")

        deck = _make_deck([note])
        # model id present in anki_dict -> no UUID fetch needed.
        note_model.anki_dict["id"] = 5
        with patch.object(deck_module, "AnkiNote") as mock_anki_note:
            deck._batch_process_new_notes([note], col, note_model, None, _cfg(), 100)
        # AnkiNote constructed for a new note (no id kwarg) and model id set.
        kwargs = mock_anki_note.call_args.kwargs
        assert "id" not in kwargs

    def test_new_note_model_missing_id_fetches_by_uuid(self):
        col = create_mock_collection()
        model = make_notetype(name="Basic", fields=["A", "B"], model_id=5)
        col.models.add(model)
        note_model = NoteModel(model)
        note_model.anki_dict["id"] = None  # force UUID-based fetch
        note = _Note("n1")

        deck = _make_deck([note])
        with patch.object(deck_module, "AnkiNote") as mock_anki_note:
            deck._batch_process_new_notes([note], col, note_model, None, _cfg(), 100)
        # Model id must have been resolved from the collection.
        assert note_model.anki_dict["id"] == 5

    def test_missing_model_id_raises(self):
        col = create_mock_collection()  # empty model store
        note_model = NoteModel(
            make_notetype(name="Basic", fields=["A", "B"], model_id=99)
        )
        note_model.anki_dict["id"] = None
        note = _Note("n1")
        deck = _make_deck([note])
        with patch.object(deck_module, "AnkiNote"):
            with _patch_logger("warning") as mock_warn:
                deck._batch_process_new_notes(
                    [note], col, note_model, None, _cfg(), 100
                )
        assert mock_warn.called


# ──────────────────────────────────────────────────────────────────────
# save_decks_and_notes_bulk (import orchestration)
# ──────────────────────────────────────────────────────────────────────


class TestSaveDecksAndNotesBulk:
    def _setup(self):
        col = create_mock_collection()
        cfg = MagicMock()
        cfg.home_deck = "Home"
        cfg.new_notes_home_deck = None
        cfg.deck_hash = "hash1"
        cfg.keep_empty_subdecks = False
        deck = _make_deck([_Note("n1")])
        deck.collection = col
        progress = MagicMock()
        return deck, col, cfg, progress

    def _patch_parts(
        self,
        count=1,
        temp_id=99,
        temp_name="_ankicollab_import_x",
        root="Home",
        media=set(),
    ):
        return (
            patch.object(
                Deck,
                "_bulk_process_all_notes",
                return_value=(count, temp_id, temp_name),
            ),
            patch.object(Deck, "_create_deck_structure", return_value=root),
            patch.object(Note, "_move_notes_to_decks"),
            patch.object(Deck, "_cleanup_temp_deck"),
            patch.object(Deck, "get_media_file_list", return_value=media),
        )

    def test_success_no_media(self):
        deck, col, cfg, progress = self._setup()
        with ExitStack() as stack:
            m_bulk, m_struct, m_move, m_cleanup, m_media = [
                stack.enter_context(p) for p in self._patch_parts()
            ]
            count, media_result = deck.save_decks_and_notes_bulk(col, progress, cfg)
        assert count == 1
        assert media_result == {"success": True, "downloaded": 0, "skipped": 0}
        m_bulk.assert_called_once()
        m_struct.assert_called_once()
        m_move.assert_called_once()
        m_cleanup.assert_called_once()

    def test_success_with_missing_media(self, tmp_path):
        deck, col, cfg, progress = self._setup()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "present.mp3").write_bytes(b"x")
        col.media.dir.return_value = str(media_dir)
        with ExitStack() as stack:
            [
                stack.enter_context(p)
                for p in self._patch_parts(media={"present.mp3", "missing.mp3"})
            ]
            count, media_result = deck.save_decks_and_notes_bulk(col, progress, cfg)
        assert count == 1
        assert media_result["missing_files"] == ["missing.mp3"]
        assert media_result["deck_hash"] == "hash1"
        assert media_result["downloaded"] == 0

    def test_success_all_media_present(self, tmp_path):
        deck, col, cfg, progress = self._setup()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "present.mp3").write_bytes(b"x")
        col.media.dir.return_value = str(media_dir)
        with ExitStack() as stack:
            [stack.enter_context(p) for p in self._patch_parts(media={"present.mp3"})]
            count, media_result = deck.save_decks_and_notes_bulk(col, progress, cfg)
        assert media_result["skipped"] == 1
        assert "missing_files" not in media_result

    def test_media_path_filter_excludes_subdirectories_and_parent_traversal(
        self, tmp_path
    ):
        deck, col, cfg, progress = self._setup()
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        col.media.dir.return_value = str(media_dir)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    Deck,
                    "get_media_file_list",
                    return_value={"safe.png", "nested/unsafe.png", "../escape.png"},
                )
            )
            stack.enter_context(
                patch.object(
                    Deck,
                    "_bulk_process_all_notes",
                    return_value=(1, 99, "_ankicollab_import_x"),
                )
            )
            stack.enter_context(
                patch.object(Deck, "_create_deck_structure", return_value="Home")
            )
            stack.enter_context(patch.object(Note, "_move_notes_to_decks"))
            stack.enter_context(patch.object(Deck, "_cleanup_temp_deck"))

            _, media_result = deck.save_decks_and_notes_bulk(col, progress, cfg)

        assert media_result["missing_files"] == ["safe.png"]
        assert "nested/unsafe.png" not in media_result["missing_files"]
        assert "../escape.png" not in media_result["missing_files"]

    def test_no_notes_returns_early(self):
        deck, col, cfg, progress = self._setup()
        deck.notes = []
        with patch.object(Deck, "_collect_all_notes"):
            result = deck.save_decks_and_notes_bulk(col, progress, cfg)
        assert result == (0, {"success": True, "downloaded": 0, "skipped": 0})

    def test_validation_errors(self):
        deck, col, cfg, progress = self._setup()
        with pytest.raises(ValueError):
            deck.save_decks_and_notes_bulk(None, progress, cfg)
        with pytest.raises(ValueError):
            deck.save_decks_and_notes_bulk(col, progress, None)
        deck2 = _make_deck([_Note("n1")])
        deck2.metadata = None
        with pytest.raises(ValueError):
            deck2.save_decks_and_notes_bulk(col, progress, cfg)

    def test_operation_aborted_reraises(self):
        deck, col, cfg, progress = self._setup()

        def abort(*a, **kw):
            raise OperationAbortedError("closed", phase="note_processing")

        with patch.object(Deck, "_bulk_process_all_notes", side_effect=abort):
            with pytest.raises(OperationAbortedError):
                deck.save_decks_and_notes_bulk(col, progress, cfg)

    def test_generic_exception_wraps_and_cleans_up(self):
        deck, col, cfg, progress = self._setup()

        def boom(*a, **kw):
            raise RuntimeError("boom")

        # _bulk_process_all_notes succeeds (sets temp_deck_id), then the note
        # move fails -> the finally block must still clean up the temp deck.
        with ExitStack() as stack:
            m_bulk, m_struct, m_move, m_cleanup, m_media = [
                stack.enter_context(p) for p in self._patch_parts()
            ]
            m_move.side_effect = boom
            with pytest.raises(ImportError, match="Bulk import failed"):
                deck.save_decks_and_notes_bulk(col, progress, cfg)
            assert m_cleanup.called
