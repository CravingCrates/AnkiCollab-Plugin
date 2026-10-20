"""Anki mock helpers.

Provides ``create_mock_mw()`` which returns a fully wired MagicMock of
``aqt.mw`` — the global Anki main-window object that almost every addon
module touches.  Also supplies lightweight stand-ins for ``anki.notes.Note``,
``anki.models.NotetypeDict``, ``anki.decks.DeckId``, etc.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, PropertyMock, patch

#: The fake profile every mock Anki instance is "logged into".
TEST_PROFILE_NAME = "TestProfile"

# ---------------------------------------------------------------------------
# Lightweight Anki type stand-ins
# ---------------------------------------------------------------------------


class FakeNote:
    """Minimal stand-in for ``anki.notes.Note``."""

    def __init__(
        self,
        mid: int = 1,
        guid: str = "abc123",
        id: int = 0,
        fields: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ):
        self.mid = mid
        self.guid = guid
        self.id = id
        self.fields = fields or ["front", "back"]
        self.tags = tags or []
        self.note_type = MagicMock(return_value={"id": mid, "name": "Basic"})

    def __getitem__(self, key: str) -> str:
        return ""

    def __setitem__(self, key: str, value: str) -> None:
        pass

    def string_tags(self) -> str:
        return " ".join(self.tags)


class FakeCard:
    """Minimal stand-in for ``anki.cards.Card``."""

    def __init__(self, nid: int = 0, did: int = 1, id: int = 0):
        self.nid = nid
        self.did = did
        self.id = id


# ---------------------------------------------------------------------------
# Strict specs for the core Anki interfaces AnkiCollab reads and writes.
#
# These exist so that a typo'd or renamed Anki API call (e.g.
# ``col.decks.get_deck(...)`` instead of ``col.decks.get(...)``) raises
# ``AttributeError`` instead of silently passing on a permissive MagicMock.
# The method names below are the *real* names AnkiCollab's production code
# calls (verified by grepping the codebase); anything not listed here is
# intentionally unavailable on the fake.
# ---------------------------------------------------------------------------


class _DecksAPI:
    """Method names AnkiCollab actually calls on ``col.decks``."""

    def get(self, did, default=True): ...
    def id(self, name): ...
    def name(self, did): ...
    def name_if_exists(self, did): ...
    def all(self): ...
    def all_names_and_ids(self): ...
    def children(self, did): ...
    def parents(self, did): ...
    def save(self, deck): ...
    def remove(self, dids): ...
    def is_filtered(self, did): ...
    def all_config(self): ...
    def get_config(self, conf_id): ...
    def add_config(self, name): ...
    def update_config(self, cfg): ...
    def card_count(self, did, include_subdecks=False): ...


class _ModelsAPI:
    """Method names AnkiCollab actually calls on ``col.models``."""

    def get(self, mid): ...
    def all(self): ...
    def add(self, model): ...
    def save(self, model): ...
    def update_dict(self, model): ...
    def by_name(self, name): ...
    def byName(self, name): ...
    def new(self, name): ...
    def change_notetype_of_notes(self, request): ...


class _DBAPI:
    """Method names AnkiCollab actually calls on ``col.db``."""

    def all(self, *args, **kwargs): ...
    def scalar(self, *args, **kwargs): ...
    def execute(self, *args, **kwargs): ...
    def list(self, *args, **kwargs): ...
    def first(self, *args, **kwargs): ...


class _TagsAPI:
    """Method names AnkiCollab actually calls on ``col.tags``."""

    def clear_unused_tags(self): ...


class _MediaAPI:
    """Method names AnkiCollab actually calls on ``col.media``."""

    def dir(self): ...
    def files_in_str(self, mid, fields): ...
    def check(self): ...


class FakeCol:
    """Minimal stand-in for ``anki.collection.Collection``.

    The core Anki interfaces (``decks``, ``models``, ``db``, ``tags``,
    ``media``) are created with ``spec_set=`` so a typo'd or renamed Anki API
    call raises ``AttributeError`` instead of silently passing.  Tests can
    still set ``return_value`` / ``side_effect`` on the real method names.
    """

    def __init__(self) -> None:
        self.models = MagicMock(spec_set=_ModelsAPI)
        self.decks = MagicMock(spec_set=_DecksAPI)
        self.tags = MagicMock(spec_set=_TagsAPI)
        self.db = MagicMock(spec_set=_DBAPI)
        self.media = MagicMock(spec_set=_MediaAPI)
        self.media.dir.return_value = ""
        self.media.files_in_str.return_value = []
        self.conf = {}

        # get_note returns a FakeNote by default
        self.get_note = MagicMock(return_value=FakeNote())
        self.find_notes = MagicMock(return_value=[])
        self.find_cards = MagicMock(return_value=[])
        self.remove_notes = MagicMock()
        self.update_note = MagicMock()
        self.update_notes = MagicMock()
        self.add_note = MagicMock(return_value=1)
        self.add_notes = MagicMock()
        self.set_deck = MagicMock()
        self.sched = MagicMock()

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Main mock builder
# ---------------------------------------------------------------------------


def create_mock_mw(
    *,
    config: Optional[Dict[str, Any]] = None,
    col: Optional[Any] = None,
    media_dir: Optional[str | Path] = None,
) -> MagicMock:
    """Build a fully wired ``MagicMock`` mimicking ``aqt.mw``.

    Parameters
    ----------
    config:
        The dict that ``mw.addonManager.getConfig(...)`` should return.
        Defaults to ``{}``.
    col:
        A replacement collection object.  If *None*, a ``FakeCol`` is used.
    media_dir:
        Path returned by ``mw.col.media.dir()``.  Defaults to ``""``.

    Returns
    -------
    MagicMock
        Drop-in replacement for ``aqt.mw``.
    """
    if config is None:
        config = {}
    if col is None:
        col = FakeCol()
    if media_dir is not None:
        col.media.dir.return_value = str(media_dir)

    mw = MagicMock()
    mw.col = col

    # addon manager --------------------------------------------------------
    addon_mgr = MagicMock()
    _config_store: Dict[str, Any] = dict(config)  # mutable copy

    def _get_config(addon_id: Any = None) -> Dict[str, Any]:
        return _config_store

    def _write_config(addon_id: Any, cfg: Dict[str, Any]) -> None:
        _config_store.clear()
        _config_store.update(cfg)

    addon_mgr.getConfig = MagicMock(side_effect=_get_config)
    addon_mgr.writeConfig = MagicMock(side_effect=_write_config)
    # Return the actual addon package name instead of hardcoded ID
    from pathlib import Path

    _addon_root = Path(__file__).resolve().parent.parent
    addon_mgr.addon_from_module = MagicMock(return_value=_addon_root.name)
    addon_mgr.addonFromModule = MagicMock(return_value=_addon_root.name)
    addon_mgr.addonsFolder = MagicMock(return_value="")
    mw.addonManager = addon_mgr

    # Settings, auth and the device id live in config_manager now, not in
    # mw.addonManager.getConfig. Seed the real store so `config=` keeps its
    # meaning: "this profile's config".
    import config_manager

    if config_manager._user_files_dir_override is None:
        # Called outside the isolation fixture (integration tests, or a
        # fixture built at collection time). Make our own sandbox rather than
        # writing into the real add-on folder.
        config_manager.reset_for_tests(
            Path(tempfile.mkdtemp(prefix="ankicollab-test-")) / "user_files"
        )
    config_manager.activate_for_tests(TEST_PROFILE_NAME)
    config_manager.write_config(copy.deepcopy(_config_store))

    # progress / taskman ---------------------------------------------------
    mw.progress = MagicMock()
    mw.progress.want_cancel.return_value = False
    mw.taskman = MagicMock()
    mw.taskman.run_on_main = MagicMock(side_effect=lambda fn: fn())
    mw.taskman.with_progress = MagicMock()

    # reviewer / state ----------------------------------------------------
    mw.reviewer = MagicMock()
    mw.state = "overview"

    return mw


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def patch_aqt_mw(mw_mock: MagicMock):
    """Return a context-manager / decorator that patches ``aqt.mw`` globally.

    Usage::

        with patch_aqt_mw(my_mock):
            from utils import DeckManager
            ...
    """
    return patch("aqt.mw", mw_mock)


def install_fake_aqt_module():
    """Create a minimal ``aqt`` package in ``sys.modules`` so that addon
    code can be imported outside a real Anki environment.

    This is called once from the unit conftest so you rarely need it
    directly.
    """
    for mod_name in (
        "aqt",
        "aqt.qt",
        "aqt.utils",
        "aqt.gui_hooks",
        "aqt.operations",
        "aqt.operations.note",
        "aqt.browser",
        "aqt.editor",
        "aqt.reviewer",
        "aqt.addcards",
        "aqt.sound",
        "aqt.theme",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    for mod_name in (
        "anki",
        "anki.collection",
        "anki.consts",
        "anki.decks",
        "anki.errors",
        "anki.hooks",
        "anki.models",
        "anki.notes",
        "anki.cards",
        "anki.utils",
        "anki.sound",
    ):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    # Make sure aqt.mw is a MagicMock that can be re-patched per-test
    aqt_mod = sys.modules["aqt"]
    if not hasattr(aqt_mod, "mw") or aqt_mod.mw is None:
        aqt_mod.mw = MagicMock()


def spy_config_writes(monkeypatch):
    """Record every config write while still performing it.

    For the handful of tests that need to assert *that* (or how often) a write
    happened, rather than just the resulting state.
    """
    import copy

    import config_manager

    writes = []
    real_write = config_manager.write_config

    def spy(config):
        writes.append(copy.deepcopy(config))
        return real_write(config)

    monkeypatch.setattr(config_manager, "write_config", spy)
    return writes
