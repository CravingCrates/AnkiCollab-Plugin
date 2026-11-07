"""Tests for the crowd_anki/anki adapter layer (Phase 0.3 coverage + logic).

These adapters wrap Anki's collection interfaces.  They were previously
imported only by the bootstrap (if at all) with zero test references — this
file exercises their real logic.
"""

import os
from unittest.mock import MagicMock

import pytest

import crowd_anki.anki  # noqa: F401  (package must stay importable)
from crowd_anki.anki.adapters.anki_deck import AnkiDeck
from crowd_anki.anki.adapters.deck_manager import (
    AnkiStaticDeckManager,
    DeckManager as CrowdDeckManager,
)
from crowd_anki.anki.adapters.file_provider import FileProvider
from crowd_anki.anki.adapters.hook_manager import AnkiHookManager
from crowd_anki.representation import deck_initializer

from crowd_anki.anki.adapters import note_model_file_provider as provider_mod
from crowd_anki.anki.adapters.note_model_file_provider import (
    ANKI_VERSION_23_10_00,
    NoteModelFileProvider,
    _model_references,
)


def test_file_provider_interface_is_abstract():
    """FileProvider is an ABC — instantiation must fail (it has abstract methods)."""
    with pytest.raises(TypeError):
        FileProvider()


class TestAnkiDeck:
    def test_name_and_dynamic_flags(self):
        deck = AnkiDeck({"name": "Parent::Child", "dyn": 0})
        assert deck.name == "Parent::Child"
        assert deck.is_dynamic is False
        assert deck.data["name"] == "Parent::Child"

    def test_dynamic_deck_detected(self):
        deck = AnkiDeck({"name": "Filtered", "dyn": 1})
        assert deck.is_dynamic is True


class TestAnkiStaticDeckManager:
    def test_all_filters_out_dynamic_decks(self):
        internal = MagicMock()
        internal.all.return_value = [
            {"name": "A", "dyn": 0},
            {"name": "B", "dyn": 1},
        ]
        mgr = AnkiStaticDeckManager(internal_deck_manager=internal)
        names = [d.name for d in mgr.all()]
        assert names == ["A"]

    def test_decks_by_name(self):
        internal = MagicMock()
        internal.all.return_value = [{"name": "A", "dyn": 0}]
        mgr = AnkiStaticDeckManager(internal_deck_manager=internal)
        assert "A" in mgr.decks_by_name()


class TestAnkiHookManager:
    def test_hook_registers(self):
        m = AnkiHookManager()
        m.hooks = MagicMock()
        handler = lambda *a: None  # noqa: E731
        m.hook("exportersList", handler)
        m.hooks.addHook.assert_called_once_with("exportersList", handler)

    def test_unhook_removes(self):
        m = AnkiHookManager()
        m.hooks = MagicMock()
        handler = lambda *a: None  # noqa: E731
        m.unhook("exportersList", handler)
        m.hooks.remHook.assert_called_once_with("exportersList", handler)


# ── fixtures / helpers ──────────────────────────────────────────────────────


def make_model(model_id, css="", tmpls=None):
    return {
        "id": model_id,
        "name": f"Model {model_id}",
        "css": css,
        "tmpls": tmpls or [],
    }


BASIC = make_model(
    1,
    css="body{}",
    tmpls=[
        {
            "name": "Card 1",
            "qfmt": "<img src='_logo.png'>{{Front}}",
            "afmt": "{{Back}}",
        },
    ],
)
STYLED = make_model(
    2,
    css=".card{background:url('_bg.jpg')}",
    tmpls=[
        {"name": "Card 1", "qfmt": "{{Front}}", "afmt": "{{Back}}"},
    ],
)
MODELS = [BASIC, STYLED]


class FakeModels:
    """Mirrors ``anki.models.ModelManager.get``: returns None for a missing id."""

    def __init__(self, models):
        self._models = {int(m["id"]): m for m in models}

    def get(self, mid, default=False):
        return self._models.get(mid)


class FakeMediaModern:
    """Anki 23.10+ ``MediaManager``: has ``extract_static_media_files``."""

    def __init__(self, media_dir, static_by_model):
        self._dir = str(media_dir)
        self._static = {int(k): v for k, v in static_by_model.items()}

    def dir(self):
        return self._dir

    def extract_static_media_files(self, mid):
        return self._static[int(mid)]


class FakeMediaLegacy:
    """Anki < 23.10 ``MediaManager``: deliberately lacks ``extract_static_media_files``."""

    def __init__(self, media_dir):
        self._dir = str(media_dir)

    def dir(self):
        return self._dir


class FakeCollection:
    def __init__(self, models, media):
        self.models = FakeModels(models)
        self.media = media


@pytest.fixture
def media_dir(tmp_path):
    """Media folder with referenced + unreferenced underscore files."""
    for fn in ("_logo.png", "_bg.jpg", "_unreferenced.png", "note.jpg"):
        (tmp_path / fn).write_text("x")
    return tmp_path


def set_anki_version(monkeypatch, point_version_value):
    monkeypatch.setattr(provider_mod, "point_version", lambda: point_version_value)


class TestNoteModelFileProvider:
    def test_post_init_loads_models_filtering_none(self):
        col = MagicMock()
        col.models.get.side_effect = lambda mid: {1: {"id": 1}, 2: None}[mid]
        provider = NoteModelFileProvider(col, [1, 2])
        assert len(provider.models) == 1
        assert provider.models[0]["id"] == 1

    def test_version_threshold_constant(self):
        # 23.10 -> point_version 231000 (same constant used across the addon).
        assert ANKI_VERSION_23_10_00 == 231000

    # ── modern path (Anki 23.10+) ───────────────────────────────────────────────

    def test_modern_path_returns_static_media(self, monkeypatch, media_dir):
        set_anki_version(monkeypatch, 250100)  # Anki 25.01
        static = {1: ["_logo.png"], 2: ["_bg.jpg"]}
        col = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        assert NoteModelFileProvider(col, [1, 2]).get_files() == {
            "_logo.png",
            "_bg.jpg",
        }

    def test_modern_path_at_exact_23_10_boundary(self, monkeypatch, media_dir):
        set_anki_version(monkeypatch, ANKI_VERSION_23_10_00)  # exactly 23.10
        static = {1: ["_logo.png"], 2: ["_bg.jpg"]}
        col = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        assert NoteModelFileProvider(col, [1, 2]).get_files() == {
            "_logo.png",
            "_bg.jpg",
        }

    def test_modern_path_excludes_referenced_but_missing_file(
        self, monkeypatch, media_dir
    ):
        # extract_static_media_files reports _missing.png, but it is not on disk:
        # the historical contract only returns files that actually exist.
        set_anki_version(monkeypatch, 250100)
        static = {1: ["_logo.png", "_missing.png"], 2: ["_bg.jpg"]}
        col = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        assert NoteModelFileProvider(col, [1]).get_files() == {"_logo.png"}

    # ── legacy path (Anki 2.1.x) ────────────────────────────────────────────────

    def test_legacy_path_returns_static_media(self, monkeypatch, media_dir):
        set_anki_version(monkeypatch, 50)  # Anki 2.1.50
        col = FakeCollection(MODELS, FakeMediaLegacy(media_dir))
        assert NoteModelFileProvider(col, [1, 2]).get_files() == {
            "_logo.png",
            "_bg.jpg",
        }

    def test_legacy_path_works_without_modern_api(self, monkeypatch, media_dir):
        set_anki_version(monkeypatch, 66)  # Anki 2.1.66
        assert not hasattr(FakeMediaLegacy(media_dir), "extract_static_media_files")
        col = FakeCollection(MODELS, FakeMediaLegacy(media_dir))
        assert NoteModelFileProvider(col, [1, 2]).get_files() == {
            "_logo.png",
            "_bg.jpg",
        }

    def test_legacy_path_ignores_unreferenced_underscore_file(
        self, monkeypatch, media_dir
    ):
        set_anki_version(monkeypatch, 50)
        # _bg.jpg belongs to STYLED (excluded), _unreferenced.png belongs to no model.
        col = FakeCollection([BASIC], FakeMediaLegacy(media_dir))
        assert NoteModelFileProvider(col, [1]).get_files() == {"_logo.png"}

    # ── parity between the two paths ────────────────────────────────────────────

    def test_modern_and_legacy_paths_agree(self, monkeypatch, media_dir):
        static = {1: ["_logo.png"], 2: ["_bg.jpg"]}
        col_modern = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        col_legacy = FakeCollection(MODELS, FakeMediaLegacy(media_dir))

        set_anki_version(monkeypatch, 250100)
        modern = NoteModelFileProvider(col_modern, [1, 2]).get_files()
        set_anki_version(monkeypatch, 50)
        legacy = NoteModelFileProvider(col_legacy, [1, 2]).get_files()

        assert modern == legacy == {"_logo.png", "_bg.jpg"}

    def test_missing_model_id_handled_on_both_paths(self, monkeypatch, media_dir):
        static = {1: ["_logo.png"], 2: ["_bg.jpg"]}
        col_modern = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        col_legacy = FakeCollection(MODELS, FakeMediaLegacy(media_dir))

        set_anki_version(monkeypatch, 250100)
        assert NoteModelFileProvider(col_modern, [1, 999]).get_files() == {"_logo.png"}
        set_anki_version(monkeypatch, 50)
        assert NoteModelFileProvider(col_legacy, [1, 999]).get_files() == {"_logo.png"}

    # ── _model_references parity with the removed AnkiExporter._modelHasMedia ──

    def test_model_references_matches_removed_anki_implementation(self):
        def reference(model, fname):
            # Verbatim logic from the removed anki/exporting.py AnkiExporter._modelHasMedia.
            if fname in model["css"]:
                return True
            for t in model["tmpls"]:
                if fname in t["qfmt"] or fname in t["afmt"]:
                    return True
            return False

        probes = [
            "_logo.png",
            "_bg.jpg",
            "Front",
            "Back",
            "_logo",
            "x",
            "_",
            "note.jpg",
            "{{Front}}",
        ]
        for model in MODELS:
            for fname in probes:
                assert _model_references(model, fname) is reference(model, fname)

    def test_modern_path_existing_only_false_includes_non_existing(
        self, monkeypatch, media_dir
    ):
        set_anki_version(monkeypatch, 250100)
        static = {1: ["_logo.png", "_missing.png"], 2: ["_bg.jpg"]}
        col = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        # With existing_only=True (default), missing file on disk is excluded
        assert NoteModelFileProvider(col, [1]).get_files(existing_only=True) == {
            "_logo.png"
        }
        # With existing_only=False, missing file on disk is included
        assert NoteModelFileProvider(col, [1]).get_files(existing_only=False) == {
            "_logo.png",
            "_missing.png",
        }

    def test_modern_path_filters_disallowed_filenames(self, monkeypatch, media_dir):
        set_anki_version(monkeypatch, 250100)
        static = {1: ["_logo.png", "_blank", "_self"]}
        col = FakeCollection(MODELS, FakeMediaModern(media_dir, static))
        assert NoteModelFileProvider(col, [1]).get_files(existing_only=False) == {
            "_logo.png"
        }

    def test_legacy_path_existing_only_false_uses_regex(self, monkeypatch, media_dir):
        set_anki_version(monkeypatch, 50)
        empty_dir = media_dir / "empty_dir"
        empty_dir.mkdir()
        col = FakeCollection(MODELS, FakeMediaLegacy(empty_dir))
        # With existing_only=True, returns empty set because nothing is on disk
        assert NoteModelFileProvider(col, [1, 2]).get_files(existing_only=True) == set()
        # With existing_only=False, extracts referenced files from templates & CSS via regex
        assert NoteModelFileProvider(col, [1, 2]).get_files(existing_only=False) == {
            "_logo.png",
            "_bg.jpg",
        }

    def test_legacy_path_regex_extracts_imports_urls_sounds_and_filters_blank(
        self, monkeypatch, media_dir
    ):
        set_anki_version(monkeypatch, 50)
        complex_model = make_model(
            3,
            css=(
                "@import \"_styles.css\"; @import '_extra.css'; "
                '.card { background: url("_bg.png"); font-family: url("_font.woff2"); }'
            ),
            tmpls=[
                {
                    "name": "Card 1",
                    "qfmt": (
                        '<script src="_script.js"></script>'
                        '<a target="_blank">link</a>[sound:_beep.mp3]'
                    ),
                    "afmt": '<div data="_data.svg"></div>"_self" "_top"',
                }
            ],
        )
        empty_dir = media_dir / "empty_regex"
        empty_dir.mkdir()
        col = FakeCollection([complex_model], FakeMediaLegacy(empty_dir))
        files = NoteModelFileProvider(col, [3]).get_files(existing_only=False)
        assert files == {
            "_styles.css",
            "_extra.css",
            "_bg.png",
            "_font.woff2",
            "_script.js",
            "_beep.mp3",
            "_data.svg",
        }
        assert "_blank" not in files
        assert "_self" not in files
        assert "_top" not in files


class TestDeckInitializer:
    def test_from_collection_returns_none_when_deck_missing(self):
        col = MagicMock()
        col.decks.by_name.return_value = None
        assert deck_initializer.from_collection(col, "Missing Deck") is None

    def test_from_collection_returns_none_for_dynamic_deck(self):
        col = MagicMock()
        col.decks.by_name.return_value = {"name": "Dyn", "dyn": True, "id": 1}
        assert deck_initializer.from_collection(col, "Dyn") is None

    def test_get_card_ids_builds_query_and_delegates(self):
        fake_self = MagicMock()
        fake_self.children.return_value = [("Child", 2)]
        fake_self.col.db.list.return_value = [1, 2, 3]
        result = deck_initializer.get_card_ids(fake_self, 1, children=True)
        assert result == [1, 2, 3]
        # Query must reference the deck ids list
        query = fake_self.col.db.list.call_args[0][0]
        assert "cards" in query and "did" in query
