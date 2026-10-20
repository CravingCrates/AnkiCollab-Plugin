"""tests/conftest.py — shared test fixtures.

All heavy lifting (fake modules, package registration) is done in the
root-level conftest.py.  This file only provides reusable fixtures.
"""

import sys
import copy
import uuid as uuid_mod
from unittest.mock import MagicMock, patch
from collections import namedtuple, defaultdict
from typing import Dict, List, Optional
from pathlib import Path

import pytest

# Dynamically determine addon package name from directory
TESTS_DIR = Path(__file__).resolve().parent
ADDON_ROOT = TESTS_DIR.parent
ADDON_PACKAGE = ADDON_ROOT.name

# ---------------------------------------------------------------------------
# Helpers for building Anki data structures
# ---------------------------------------------------------------------------


def make_notetype(
    name="Basic",
    fields=None,
    templates=None,
    model_id=1000,
    model_uuid=None,
    css=".card { }",
):
    """Create a realistic notetype dict for testing."""
    if fields is None:
        fields = ["Front", "Back"]
    if templates is None:
        templates = [
            {
                "name": "Card 1",
                "qfmt": "{{Front}}",
                "afmt": "{{FrontSide}}<hr id=answer>{{Back}}",
                "ord": 0,
            }
        ]
    if model_uuid is None:
        model_uuid = str(uuid_mod.uuid4())

    return {
        "id": model_id,
        "name": name,
        "flds": [
            {
                "name": f,
                "ord": i,
                "sticky": False,
                "rtl": False,
                "font": "Arial",
                "size": 20,
            }
            for i, f in enumerate(fields)
        ],
        "tmpls": templates,
        "css": css,
        "crowdanki_uuid": model_uuid,
        "mod": 1700000000,
        "type": 0,
        "sortf": 0,
        "vers": [],
        "tags": [],
        "req": [[0, "all", [0]]],
    }


def make_note_dict(
    guid="test_guid_001",
    fields=None,
    tags=None,
    note_model_uuid=None,
    note_id=None,
):
    """Create a realistic note JSON dict for testing."""
    if fields is None:
        fields = ["Front content", "Back content"]
    if tags is None:
        tags = ["test_tag"]
    result = {
        "guid": guid,
        "fields": list(fields),
        "tags": list(tags),
        "note_model_uuid": note_model_uuid or "model-uuid-001",
        "flags": 0,
    }
    if note_id is not None:
        result["id"] = note_id
    return result


def make_deck_json(
    name="TestDeck",
    notes=None,
    children=None,
    note_models=None,
    deck_configurations=None,
    deck_uuid=None,
    deck_id=1,
):
    """Create a realistic deck JSON for testing."""
    if deck_uuid is None:
        deck_uuid = str(uuid_mod.uuid4())
    return {
        "name": name,
        "crowdanki_uuid": deck_uuid,
        "id": deck_id,
        "notes": notes or [],
        "children": children or [],
        "note_models": note_models or [],
        "deck_configurations": deck_configurations or [],
    }


# ---------------------------------------------------------------------------
# Mock Anki Note that works with the addon's Note class
# ---------------------------------------------------------------------------


class MockAnkiNote:
    """A mock AnkiNote suitable for addon Note wrapper testing."""

    def __init__(self, collection=None, id=None, model=None, **kwargs):
        self.id = id or 0
        self.mid = model.get("id", 0) if isinstance(model, dict) else (model or 0)
        self.mod = 1700000000
        self.guid = ""
        self.tags = []
        self.fields = []
        self.flags = 0
        self._fmap = {}
        self._model = model or {}
        self._col = collection
        self._card_ids = []

    def card_ids(self):
        return list(self._card_ids)

    def note_type(self):
        return self._model

    def joined_fields(self):
        return "\x1f".join(self.fields)


# ---------------------------------------------------------------------------
# Mock Collection with model/deck stores
# ---------------------------------------------------------------------------


def create_mock_collection(media_dir=""):
    """Build a mock collection with working model/deck stores."""
    col = MagicMock()
    col.db = MagicMock()
    col.media = MagicMock()
    col.media.dir.return_value = media_dir
    col.media.files_in_str = MagicMock(return_value=[])

    # --- Models store ---
    all_models = {}

    def models_get(model_id):
        return copy.deepcopy(
            all_models.get(int(model_id) if not isinstance(model_id, int) else model_id)
        )

    def models_all():
        return [copy.deepcopy(m) for m in all_models.values()]

    def models_add(model_dict):
        mid = model_dict.get("id", 0)
        if mid == 0:
            mid = max(all_models.keys(), default=0) + 1
        model_dict["id"] = mid
        all_models[mid] = copy.deepcopy(model_dict)

    def models_update_dict(model_dict):
        mid = model_dict.get("id")
        if mid:
            all_models[mid] = copy.deepcopy(model_dict)

    def models_by_name(name):
        for m in all_models.values():
            if m.get("name") == name:
                return copy.deepcopy(m)
        return None

    def models_new(name):
        return {
            "id": 0,
            "name": name,
            "flds": [],
            "tmpls": [],
            "css": "",
            "crowdanki_uuid": "",
        }

    col.models.get = MagicMock(side_effect=models_get)
    col.models.all = MagicMock(side_effect=models_all)
    col.models.add = MagicMock(side_effect=models_add)
    col.models.update_dict = MagicMock(side_effect=models_update_dict)
    col.models.by_name = MagicMock(side_effect=models_by_name)
    col.models.new = MagicMock(side_effect=models_new)
    col.models.change_notetype_of_notes = MagicMock()
    col.models._store = all_models  # expose for test manipulation

    # --- Decks store ---
    all_decks = {}
    deck_counter = [1]

    def decks_id(name, create=True):
        for did, d in all_decks.items():
            if d.get("name") == name:
                return did
        if create:
            did = deck_counter[0]
            deck_counter[0] += 1
            all_decks[did] = {"id": did, "name": name, "crowdanki_uuid": ""}
            return did
        return None

    def decks_get(did, default=True):
        r = all_decks.get(did)
        if r is None and default:
            return {"id": did, "name": "Default"}
        return r

    def decks_save(deck_dict):
        did = deck_dict.get("id")
        if did:
            all_decks[did] = deck_dict

    def decks_all():
        return list(all_decks.values())

    def decks_all_names_and_ids():
        NI = namedtuple("NI", ["name", "id"])
        return [NI(d["name"], d["id"]) for d in all_decks.values()]

    def decks_children(did):
        parent = all_decks.get(did, {}).get("name", "")
        return [
            (d["name"], d["id"])
            for d in all_decks.values()
            if d["name"].startswith(parent + "::") and d["id"] != did
        ]

    def decks_card_count(did, include_subdecks=False):
        return 0

    def decks_remove(dids):
        r = MagicMock()
        r.count = len(dids)
        for did in dids:
            all_decks.pop(did, None)
        return r

    col.decks.id = MagicMock(side_effect=decks_id)
    col.decks.get = MagicMock(side_effect=decks_get)
    col.decks.save = MagicMock(side_effect=decks_save)
    col.decks.all = MagicMock(side_effect=decks_all)
    col.decks.all_names_and_ids = MagicMock(side_effect=decks_all_names_and_ids)
    col.decks.children = MagicMock(side_effect=decks_children)
    col.decks.card_count = MagicMock(side_effect=decks_card_count)
    col.decks.remove = MagicMock(side_effect=decks_remove)
    col.decks.is_filtered = MagicMock(return_value=False)
    col.decks.all_config = MagicMock(return_value=[])
    col.decks.get_config = MagicMock(
        return_value={"id": 1, "name": "Default", "crowdanki_uuid": "cfg-uuid-001"}
    )
    col.decks.add_config = MagicMock(
        return_value={"id": 2, "name": "New Config", "crowdanki_uuid": ""}
    )
    col.decks.update_config = MagicMock()
    col.decks._store = all_decks

    # --- DB ---
    col.db.all = MagicMock(return_value=[])
    col.db.scalar = MagicMock(return_value=0)
    col.db.execute = MagicMock()
    col.db.list = MagicMock(return_value=[])

    # --- Note operations ---
    col.add_note = MagicMock()
    col.add_notes = MagicMock()
    col.update_notes = MagicMock()
    col.get_note = MagicMock()
    col.set_deck = MagicMock()

    # --- Sched ---
    col.sched = MagicMock()
    col.sched.suspend_cards = MagicMock()

    return col


# ---------------------------------------------------------------------------
# Shared Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_media_dir(tmp_path):
    """Provide a temporary media directory with a few dummy files."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "test_image.png").write_bytes(b"\x89PNG" + b"\x00" * 200)
    (media / "test_audio.mp3").write_bytes(b"ID3" + b"\x00" * 200)
    (media / "tiny.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 50)
    return media


@pytest.fixture
def sample_config():
    """Return a minimal addon config dict suitable for DeckManager."""
    return {
        "settings": {"push_counter": 0, "rated_addon": False},
        "abc123": {
            "deckId": 1,
            "timestamp": "2025-01-01 00:00:00",
            "optional_tags": {},
            "stats_enabled": False,
            "share_stats": False,
            "last_stats_timestamp": 0,
        },
    }


@pytest.fixture
def api_base_url():
    return "http://plugin.localhost"


@pytest.fixture
def mock_collection(tmp_path):
    """A fully mocked Anki collection with working stores."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    return create_mock_collection(media_dir=str(media_dir))


@pytest.fixture
def basic_notetype():
    """Simple Basic notetype dict."""
    return make_notetype(
        name="Basic",
        fields=["Front", "Back"],
        model_id=1000,
        model_uuid="basic-model-uuid-001",
    )


@pytest.fixture
def cloze_notetype():
    """Cloze notetype dict."""
    return make_notetype(
        name="Cloze",
        fields=["Text", "Extra"],
        templates=[
            {
                "name": "Cloze",
                "qfmt": "{{cloze:Text}}",
                "afmt": "{{cloze:Text}}<br>{{Extra}}",
                "ord": 0,
            }
        ],
        model_id=1001,
        model_uuid="cloze-model-uuid-001",
    )


@pytest.fixture
def complex_notetype():
    """Complex notetype with many fields for edge case testing."""
    return make_notetype(
        name="Complex Medical",
        fields=[
            "Term",
            "Definition",
            "Extra Info",
            "Image",
            "Audio",
            "Tags Field",
            "Source",
        ],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Term}}",
                "afmt": "{{Term}}<hr>{{Definition}}<br>{{Extra Info}}",
                "ord": 0,
            },
            {
                "name": "Card 2",
                "qfmt": "{{Definition}}",
                "afmt": "{{Definition}}<hr>{{Term}}",
                "ord": 1,
            },
        ],
        model_id=1002,
        model_uuid="complex-model-uuid-001",
    )


@pytest.fixture
def projektanki_notetype():
    """ProjektAnki notetype whose templates should be preserved."""
    return make_notetype(
        name="ProjektAnki Basic",
        fields=["Front", "Back", "Hint"],
        model_id=1003,
        model_uuid="projektanki-model-uuid-001",
    )


@pytest.fixture
def mock_import_config():
    """Create a mock ImportConfig for testing."""
    cfg = MagicMock()
    cfg.add_tag_to_cards = []
    cfg.optional_tags = []
    cfg.has_optional_tags = False
    cfg.use_notes = True
    cfg.use_media = True
    cfg.ignore_deck_movement = False
    cfg.suspend_new_cards = False
    cfg.keep_empty_subdecks = False
    cfg.home_deck = None
    cfg.deck_hash = "test_hash_123"
    cfg.new_notes_home_deck = None
    cfg.is_personal_field = MagicMock(return_value=False)
    return cfg


# ---------------------------------------------------------------------------
# Config-manager isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config_manager(tmp_path_factory):
    """Never let a test read or write the real add-on's user_files folder.

    The sandbox deliberately lives outside ``tmp_path``. ``create_mock_mw``
    seeds a profile into it for every unit test, and config_manager's own
    tests assert on the contents of ``tmp_path`` -- for example that a
    directory holds exactly one file -- so sharing one directory made those
    assertions fail for reasons unrelated to what they test.
    """
    import config_manager

    sandbox_root = tmp_path_factory.mktemp("ankicollab")
    config_manager.reset_for_tests(sandbox_root / "user_files")

    yield sandbox_root / "user_files"
    config_manager.reset_for_tests(None)
