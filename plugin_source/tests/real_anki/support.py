"""Helpers for the real-Anki suite.

Kept out of conftest.py so tests can import them directly regardless of pytest's
import mode.
"""

from __future__ import annotations

import copy
import json
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ADDON_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent
DIST_DIR = ADDON_ROOT / "dist"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# main.py puts the vendored dependencies on sys.path at runtime. This suite
# imports the add-on modules directly, so it must do the same (pyfunctional is
# only vendored, not a test dependency).
if str(DIST_DIR) not in sys.path:
    sys.path.append(str(DIST_DIR))

# Import the addon's representation layer under a private top-level package so
# the addon's own __init__.py (which does `from .main import *`) never runs.
PACKAGE = "ankicollab_under_test"
if PACKAGE not in sys.modules:
    _pkg = types.ModuleType(PACKAGE)
    _pkg.__path__ = [str(ADDON_ROOT)]
    _pkg.__package__ = PACKAGE
    sys.modules[PACKAGE] = _pkg


def import_deck_layer():
    """Import ``Deck``/``deck_initializer`` without executing the add-on's main.

    ``deck.py`` imports the add-on's ``main`` module, which starts the UI. Tests
    that exercise the real export/import orchestration stub only that module.
    """
    main_name = f"{PACKAGE}.main"
    if main_name not in sys.modules:
        main_mock = MagicMock()
        sys.modules[main_name] = main_mock
        setattr(sys.modules[PACKAGE], "main", main_mock)

    from ankicollab_under_test.crowd_anki.representation import (  # noqa: E402
        deck_initializer,
    )
    from ankicollab_under_test.crowd_anki.representation.deck import (  # noqa: E402
        Deck,
    )

    return deck_initializer, Deck


class ImportProgress:
    """Minimal progress tracker accepted by ``save_decks_and_notes_bulk``."""

    def __init__(self):
        self.phase_labels = []
        self.notes_progress = []
        self.media_progress = []

    def set_phase_label(self, label):
        self.phase_labels.append(label)

    def update_notes_progress(self, processed):
        self.notes_progress.append(processed)

    def update_media_progress(self, current, total):
        self.media_progress.append((current, total))


def patch_export_import_runtime(monkeypatch, col, cancel=False):
    """Wire the real ``Deck`` code to a real collection and a fake UI shell."""
    import aqt

    deck_initializer, Deck = import_deck_layer()
    from ankicollab_under_test import utils as addon_utils
    from ankicollab_under_test.crowd_anki.config.config_settings import (
        ConfigSettings,
    )
    from ankicollab_under_test.crowd_anki.representation import deck as deck_mod

    runtime = MagicMock()
    runtime.col = col
    runtime.progress.want_cancel.return_value = cancel
    runtime.taskman.run_on_main.side_effect = lambda callback: callback()

    monkeypatch.setattr(aqt, "mw", runtime)
    # These modules bound ``mw`` at import time; patching aqt.mw is not enough.
    monkeypatch.setattr(addon_utils, "mw", runtime)
    monkeypatch.setattr(deck_mod, "mw", runtime)
    monkeypatch.setattr(
        ConfigSettings, "get_instance", lambda *args, **kwargs: MagicMock()
    )
    return runtime


def export_deck(col, deck_name, monkeypatch):
    """Run the real export pipeline and return ``(deck, json_payload)``."""
    deck_initializer, Deck = import_deck_layer()
    patch_export_import_runtime(monkeypatch, col)
    deck = deck_initializer.from_collection(col, deck_name)
    deck_initializer.trim_empty_children(deck)
    payload = json.loads(json.dumps(deck, default=Deck.default_json))
    return deck, payload


def import_deck(col, deck_json, monkeypatch, config=None, cancel=False):
    """Run the real import orchestration against ``deck_json``."""
    deck_initializer, _Deck = import_deck_layer()
    patch_export_import_runtime(monkeypatch, col, cancel=cancel)
    config = config or Config()
    deck = deck_initializer.from_json(copy.deepcopy(deck_json))
    progress = ImportProgress()
    deck.save_metadata(col, config.home_deck)
    result = deck.save_decks_and_notes_bulk(col, progress, config)
    return deck, result, progress


def assert_real_backend() -> None:
    """Fail loudly if the faked `anki` package is in play."""
    import anki.collection
    import anki.models

    for module in (anki.collection, anki.models):
        if isinstance(module, MagicMock):
            raise RuntimeError(
                "The real-Anki suite is running against the faked `anki` package. "
                "Run it with: python -m pytest tests/real_anki "
                "-c tests/real_anki/pytest.ini"
            )
    if not isinstance(anki.collection.Collection, type):
        raise RuntimeError("anki.collection.Collection is not a real class")


from anki.collection import Collection  # noqa: E402
from anki.notes import Note as AnkiNote  # noqa: E402

from ankicollab_under_test.crowd_anki.representation.note import (  # noqa: E402
    Note,
)
from ankicollab_under_test.crowd_anki.representation.note_model import (  # noqa: E402
    NoteModel,
)


class Config:
    """The ImportConfig surface consumed by the import orchestration."""

    def __init__(
        self,
        personal_fields=(),
        personal_tags=(),
        optional_tags=None,
        home_deck="",
        new_notes_home_deck="",
        ignore_deck_movement=False,
        suspend_new_cards=False,
        keep_empty_subdecks=False,
        deck_hash="test-deck",
        use_notes=True,
        use_media=False,
    ):
        self._personal_fields = {(model, field) for model, field in personal_fields}
        self.personal_tags = list(personal_tags)
        self.has_optional_tags = optional_tags is not None
        self.optional_tags = list(optional_tags or [])
        self.home_deck = home_deck
        self.new_notes_home_deck = new_notes_home_deck
        self.ignore_deck_movement = ignore_deck_movement
        self.suspend_new_cards = suspend_new_cards
        self.keep_empty_subdecks = keep_empty_subdecks
        self.deck_hash = deck_hash
        self.use_notes = use_notes
        self.use_media = use_media

    def is_personal_field(self, model_name, field_name):
        return (model_name, field_name) in self._personal_fields


def make_notetype(col, name, fields, uuid="notetype-uuid"):
    """Create a notetype with one template and return the stored dict."""
    notetype = col.models.new(name)
    for field_name in fields:
        col.models.add_field(notetype, col.models.new_field(field_name))
    template = col.models.new_template("Card 1")
    template["qfmt"] = "{{" + fields[0] + "}}"
    template["afmt"] = "{{FrontSide}}"
    col.models.add_template(notetype, template)
    notetype["crowdanki_uuid"] = uuid
    col.models.add(notetype)
    return col.models.get(notetype["id"])


def remote_field_dicts(names):
    """Field dicts as they arrive from the server (ord sequential)."""
    return [
        {
            "name": name,
            "ord": index,
            "sticky": False,
            "rtl": False,
            "font": "Arial",
            "size": 20,
        }
        for index, name in enumerate(names)
    ]


def merge_remote(col, notetype, remote_names):
    """Run the real merge of a remote notetype into an existing local one.

    Returns the NoteModel instance exactly as the import orchestration leaves it
    (merged fields applied to `anki_dict`, local-only names recorded).
    """
    existing = col.models.get(notetype["id"])
    remote = dict(existing)
    remote["flds"] = remote_field_dicts(remote_names)
    model = NoteModel(remote)
    updated, _mapping = model.save_to_collection(col)
    if updated is None:
        # No schema change; the model object already points at the local
        # notetype (id/flds), so use the stored definition.
        updated = col.models.get(notetype["id"])
    model.anki_dict["flds"] = updated["flds"]
    return model


def values_by_name(col, note_id):
    """field name -> value for a persisted note."""
    note = col.get_note(note_id)
    notetype = col.models.get(note.mid)
    return dict(zip([field["name"] for field in notetype["flds"]], note.fields))


def import_note(col, note_model, note_id, remote_values, config=None, tags=()):
    """Run one existing note through the real import path and persist it.

    Mirrors Deck._batch_process_notes: a fresh AnkiNote loaded from the
    collection, the remote JSON payload applied, then update_notes().
    """
    config = config or Config()
    note = Note(config=config)
    note.anki_object_dict = {
        "guid": col.get_note(note_id).guid,
        "fields": list(remote_values),
        "tags": list(tags),
        "note_model_uuid": note_model.get_uuid(),
    }
    note.note_model_uuid = note_model.get_uuid()
    note.anki_object = AnkiNote(col, id=note_id)
    note.handle_import_config_changes(config, note_model)
    note.anki_object.__dict__.update(note.anki_object_dict)
    col.update_notes([note.anki_object])
    return col.get_note(note_id)


def import_new_note(col, note_model, remote_values, config=None, tags=(), deck_id=1):
    """Add a note through the same merge/protection path used for new notes."""
    config = config or Config()
    note = Note(config=config)
    note.anki_object_dict = {
        "guid": f"new-{uuid.uuid4().hex}",
        "fields": list(remote_values),
        "tags": list(tags),
        "note_model_uuid": note_model.get_uuid(),
    }
    note.note_model_uuid = note_model.get_uuid()
    note.anki_object = AnkiNote(col, note_model.anki_dict)
    note.handle_import_config_changes(config, note_model, is_new=True)
    note.anki_object.__dict__.update(note.anki_object_dict)
    col.add_note(note.anki_object, deck_id)
    return col.get_note(note.anki_object.id)


def add_note(col, notetype, deck_id, values, tags=(), guid=None):
    note = col.new_note(notetype)
    note.fields = list(values)
    note.tags = list(tags)
    if guid:
        note.guid = guid
    col.add_note(note, deck_id)
    return note


@pytest.fixture
def col(tmp_path):
    """A real, empty Anki collection backed by a temporary file.

    Imported into each test module (`from support import col`). The backend
    guard lives here so an accidental run under the faked-`anki` conftest fails
    loudly instead of silently testing mocks.
    """
    assert_real_backend()
    collection = Collection(str(tmp_path / "collection.anki2"))
    try:
        yield collection
    finally:
        collection.close()
