"""Root-level conftest — loaded FIRST by pytest.

Prevents the addon's ``__init__.py`` (which does ``from .main import *``)
from being imported by pytest's package-collection mechanism.
We register a dummy package module in sys.modules before pytest can
attempt to import it.
"""

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ── SKIP_INIT pattern ──────────────────────────────────────────────────
os.environ["SKIP_INIT"] = "1"

# ── Determine paths ───────────────────────────────────────────────────
ADDON_ROOT = Path(__file__).resolve().parent
ADDON_PACKAGE = ADDON_ROOT.name  # "1957538407"
ADDONS_DIR = str(ADDON_ROOT.parent)
# The addon vendors third-party deps under dist/ and puts that directory on
# sys.path at runtime (see main.py: ``sys.path.append(..., "dist")``).  Mirror
# that here so the test suite loads the SAME vendored packages the addon ships
# (e.g. yaml for crowd_anki.importer.anki_importer) and does not accidentally
# depend on a pip-installed copy happening to be present in the environment.
DIST_DIR = ADDON_ROOT / "dist"

# ── Fake aqt / anki / sentry_sdk modules ──────────────────────────────
_FAKE_MODULES = [
    "aqt",
    "aqt.qt",
    "aqt.utils",
    "aqt.gui_hooks",
    "aqt.operations",
    "aqt.operations.note",
    "aqt.operations.tag",
    "aqt.browser",
    "aqt.editor",
    "aqt.reviewer",
    "aqt.addcards",
    "aqt.sound",
    "aqt.theme",
    "aqt.errors",
    "aqt.dialogs",
    "aqt.emptycards",
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
    "sentry_sdk",
]

for _mod_name in _FAKE_MODULES:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

# --- Fix anki.utils.point_version to return an integer ---
# note.py calls ``point_version()`` at module level and compares the result
# to integer constants.  A MagicMock would fail the ``>=`` comparison.
_anki_utils = sys.modules["anki.utils"]
_anki_utils.point_version = MagicMock(return_value=250100)  # pretend Anki 25.01
_anki_utils.is_win = True
_anki_utils.join_fields = lambda fields: "\x1f".join(fields)
_anki_utils.split_fields = lambda s: s.split("\x1f")
_anki_utils.ids2str = lambda ids: "(%s)" % ",".join(str(i) for i in ids)

# --- Fake 'functional' (PyFunctional) package used by crowd_anki/utils/uuid.py ---
if "functional" not in sys.modules:
    _functional_mock = types.ModuleType("functional")

    # seq() should iterate over a list and support the chained operations that
    # crowd_anki actually uses (map/filter/flat_map/to_list/to_set/make_string/
    # find/any/for_each).
    class _FakeSeq:
        def __init__(self, iterable=None):
            self._data = list(iterable) if iterable else []

        def map(self, fn):
            return _FakeSeq(fn(x) for x in self._data)

        def filter(self, fn):
            return _FakeSeq(x for x in self._data if fn(x))

        def flat_map(self, fn):
            result = []
            for x in self._data:
                result.extend(fn(x))
            return _FakeSeq(result)

        def to_list(self):
            return list(self._data)

        def to_set(self):
            return set(self._data)

        def make_string(self, sep=""):
            return sep.join(str(x) for x in self._data)

        def find(self, predicate):
            for item in self._data:
                if predicate(item):
                    return item
            return None

        def any(self):
            return any(self._data)

        def for_each(self, fn):
            for item in self._data:
                fn(item)

        def __iter__(self):
            return iter(self._data)

    _functional_mock.seq = _FakeSeq
    sys.modules["functional"] = _functional_mock

# --- Fake anki.models types used by deck.py ---
_anki_models = sys.modules.get("anki.models")
if _anki_models is None:
    _anki_models = MagicMock()
    sys.modules["anki.models"] = _anki_models
for _sym in ("ChangeNotetypeRequest", "NoteType", "NotetypeDict", "NotetypeId"):
    if not hasattr(_anki_models, _sym) or isinstance(
        getattr(_anki_models, _sym), MagicMock
    ):
        setattr(_anki_models, _sym, MagicMock())

# --- Fake anki.collection.AddNoteRequest used conditionally by note.py ---
_anki_collection = sys.modules.get("anki.collection")
if _anki_collection is None:
    _anki_collection = MagicMock()
    sys.modules["anki.collection"] = _anki_collection
if not hasattr(_anki_collection, "AddNoteRequest") or isinstance(
    getattr(_anki_collection, "AddNoteRequest"), MagicMock
):
    setattr(_anki_collection, "AddNoteRequest", MagicMock())

# ── Wire up aqt.qt ─────────────────────────────────────────────────────
# ``aqt.qt`` must be a *real* module, not a MagicMock: several addon modules
# do ``from aqt.qt import *``, and star-import only binds names present in
# ``__all__`` (a bare MagicMock exposes almost nothing).  We scan every
# production source file for Qt identifiers so ``__all__`` is comprehensive,
# and install a PEP 562 ``__getattr__`` so any other Qt symbol still resolves.
_qt_mod = types.ModuleType("aqt.qt")
sys.modules["aqt.qt"] = _qt_mod
# qtmajor must be a real int — import_ui.py does ``if qtmajor > 5:``
_qt_mod.qtmajor = 6

# --- Stub classes that can be used as base classes in addon code ---
# MagicMock *instances* cannot serve as base classes in Python 3, so we
# provide lightweight classes for the Qt types that addon modules inherit from.


class _StubQThread:
    """Minimal stub for QThread so subclasses can define class bodies."""

    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass

    def isRunning(self):
        return False


class _StubQDialog:
    """Minimal stub for QDialog."""

    # Mirrors real Qt: ``QDialog.DialogCode.Accepted`` / ``Rejected`` used by
    # dialog exec() callers across the addon (e.g. _ask_share_stats).
    from types import SimpleNamespace as _SimpleNamespace

    DialogCode = _SimpleNamespace(Accepted=1, Rejected=0)

    def __init__(self, *a, **kw):
        pass

    def setWindowTitle(self, *a):
        pass

    def resize(self, *a):
        pass

    def exec(self):
        pass

    def accept(self):
        pass

    def close(self):
        pass

    def raise_(self):
        pass

    def activateWindow(self):
        pass

    def __getattr__(self, name):
        # Any other QDialog method called during dialog __init__ (setModal,
        # setFixedWidth, setStyleSheet, ...) is a no-op test stub.
        def _noop(*args, **kwargs):
            return None

        return _noop


class _StubQWebEnginePage:
    """Minimal stub for QWebEnginePage."""

    def __init__(self, *a, **kw):
        pass

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        return True

    def runJavaScript(self, *a):
        pass


class _StubQWebEngineView:
    """Minimal stub for QWebEngineView."""

    def __init__(self, *a, **kw):
        pass

    def setPage(self, *a):
        pass

    def page(self):
        return None

    loadFinished = MagicMock()

    def load(self, *a):
        pass


class _StubQWidget:
    """Minimal stub for QWidget.

    ``media_progress_indicator.MediaProgressIndicator`` subclasses ``QWidget``
    at module level; a ``MagicMock`` instance cannot serve as a base class in
    Python 3, so without this stub the module fails to import (previously it
    was silently masked by the MagicMock import fallback).
    """

    def __init__(self, *a, **kw):
        pass

    def hide(self):
        pass

    def show(self):
        pass

    def setFixedHeight(self, *a):
        pass

    def setMinimumWidth(self, *a):
        pass

    def __getattr__(self, name):
        # Any other QWidget method called during widget setup is a no-op.
        def _noop(*args, **kwargs):
            return None

        return _noop


def _stub_pyqtSignal(*args, **kwargs):
    """Return a MagicMock descriptor that acts like pyqtSignal."""
    return MagicMock()


_qt_mod.QThread = _StubQThread
_qt_mod.QDialog = _StubQDialog
_qt_mod.QWebEnginePage = _StubQWebEnginePage
_qt_mod.QWebEngineView = _StubQWebEngineView
_qt_mod.QWidget = _StubQWidget
_qt_mod.pyqtSignal = _stub_pyqtSignal

# Collect every Qt identifier referenced anywhere in the addon's production
# source, so ``from aqt.qt import *`` binds all of them.  (Names captured from
# comments/CSS strings are harmless extra mocks.)  ``QtWidgets`` is included
# because the vendored config/import UIs access widgets via ``QtWidgets.X``.
import re as _re

_QT_IDENTIFIER_RE = _re.compile(r"\b(Q[A-Z][A-Za-z0-9_]*|QtWidgets)\b")
_qt_symbols = set()
for _py in ADDON_ROOT.rglob("*.py"):
    if "tests" in _py.parts or _py.name in (
        "conftest.py",
        "_diag_bootstrap.py",
        "_diag_import.py",
    ):
        continue
    try:
        _qt_symbols.update(_QT_IDENTIFIER_RE.findall(_py.read_text(encoding="utf-8")))
    except Exception:
        pass
# Ensure essential names are always present even if unused by the scan.
_qt_symbols.update(
    {
        "Qt",
        "QtWidgets",
        "QAction",
        "QMenu",
        "QDialog",
        "QWidget",
        "QThread",
        "QTimer",
        "QLabel",
        "QPushButton",
        "QLineEdit",
        "QCheckBox",
        "QVBoxLayout",
        "QHBoxLayout",
        "QGroupBox",
        "QTableWidget",
        "QTableWidgetItem",
        "QApplication",
        "QMessageBox",
        "QFileDialog",
        "QTextBrowser",
        "QListWidget",
        "QComboBox",
        "QRadioButton",
        "QFont",
        "QSize",
        "QIcon",
        "QPixmap",
        "QColor",
        "QUrl",
        "QShortcut",
        "QKeySequence",
        "QTextEdit",
        "QPlainTextEdit",
        "QProgressBar",
        "QFrame",
        "QGraphicsOpacityEffect",
        "QPropertyAnimation",
        "QEasingCurve",
        "QPalette",
        "QWebEnginePage",
        "QWebEngineView",
        "QAbstractItemView",
        "QHeaderView",
        "QSizePolicy",
        "QSpacerItem",
        "QListWidgetItem",
        "QGridLayout",
        "QToolButton",
        "pyqtSignal",
        "qtmajor",
    }
)
for _sym in _qt_symbols:
    if not hasattr(_qt_mod, _sym):
        setattr(_qt_mod, _sym, MagicMock())
_qt_mod.__all__ = sorted(_qt_symbols)


def _qt_getattr(name: str):
    """PEP 562: lazily resolve any other Qt symbol referenced by name."""
    _m = MagicMock()
    setattr(_qt_mod, name, _m)
    return _m


_qt_mod.__getattr__ = _qt_getattr

sys.modules["aqt"].mw = MagicMock()

# ── Register addon package WITHOUT running __init__.py ────────────────
if ADDONS_DIR not in sys.path:
    sys.path.insert(0, ADDONS_DIR)
if str(ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(ADDON_ROOT))
# Mirror main.py's ``sys.path.append(..., "dist")``: appended AFTER the addon
# root so the addon's own modules win, and after site-packages entries already
# on the path so pip-installed (CI-pinned) copies take precedence when present.
# Vendored dist/ is the fallback that keeps a clean install (e.g. headless
# Linux CI without pyyaml in site-packages) importable.
if str(DIST_DIR) not in sys.path:
    sys.path.append(str(DIST_DIR))

# Create a lightweight package module
_pkg = types.ModuleType(ADDON_PACKAGE)
_pkg.__path__ = [str(ADDON_ROOT)]
_pkg.__package__ = ADDON_PACKAGE
_pkg.__file__ = str(ADDON_ROOT / "__init__.py")
sys.modules[ADDON_PACKAGE] = _pkg

# Also register under the names pytest may try during import_path
# (pytest with importlib mode may compute "__init__" as the module name
# since __init__.py is at the rootdir)
sys.modules[f"{ADDON_PACKAGE}.__init__"] = _pkg
sys.modules["__init__"] = _pkg


# ── Pre-register 'main' as MagicMock ──────────────────────────────────
# main.py has heavy side effects and circular deps; register it as a
# MagicMock so that ``from . import main`` in other submodules resolves
# without triggering the real file.
_main_mock = MagicMock()
_main_full = f"{ADDON_PACKAGE}.main"
sys.modules[_main_full] = _main_mock
sys.modules["main"] = _main_mock
setattr(_pkg, "main", _main_mock)

# ── Import addon submodules (dependency order) ────────────────────────
import importlib

# mutmut (3.7) copies the source tree into a ``mutants/`` sandbox and runs
# pytest from there with this conftest.  Its trampoline records mutation hits
# keyed by the module's top-level ``__name__`` (e.g. ``stats.x_update_...``
# for ``mutants/stats.py``).  If the addon modules are imported through the
# synthetic package they get ``__name__ == "mutants.stats"`` and mutmut aborts
# with "tests import the source under a different module path".  Detect the
# sandbox by the working-tree name and register a meta-path finder that loads
# every addon source file ONCE with ``__name__`` = its top-level path (so the
# trampoline keys match) while keeping ``__package__`` correct so the addon's
# relative imports still resolve.  The normal (non-mutmut) run is completely
# unaffected.
_IN_MUTMUT_SANDBOX = ADDON_ROOT.name == "mutants"

if _IN_MUTMUT_SANDBOX:
    import importlib.abc
    import importlib.machinery
    import importlib.util

    class _SandboxModuleLoader(importlib.abc.Loader):
        def __init__(self, fullname: str, topname: str, path: Path):
            self._fullname = fullname
            self._topname = topname
            self._path = path

        def create_module(self, spec):
            return None  # use the default module creation path

        def exec_module(self, module):
            # Name the module by its TOP-LEVEL path so mutmut's trampoline
            # keys (derived from ``func.__module__``) match the file paths.
            module.__name__ = self._topname
            module.__file__ = str(self._path)
            if self._path.name == "__init__.py":
                # A package's `__init__.py`.  ``ui.__init__`` (an
                # importlib-mode artifact listed in _ADDON_MODULES_ORDERED) is
                # really the ``ui`` package, so strip the ``.__init__`` suffix.
                pkg = (
                    self._topname[: -len(".__init__")]
                    if self._topname.endswith(".__init__")
                    else self._topname
                )
            elif "." in self._topname:
                pkg = self._topname.rpartition(".")[0]
            else:
                # Flat top-level module using relative imports
                # (``from .utils import ...``).
                pkg = ""
            # The addon's relative imports assume the synthetic package prefix
            # (e.g. ``from ...utils import get_logger`` inside
            # ``crowd_anki/representation/note_model.py`` resolves up through
            # ``mutants.crowd_anki...``), so __package__ carries that prefix
            # while __name__ stays top-level for mutmut's trampoline keys.
            module.__package__ = ADDON_PACKAGE + (("." + pkg) if pkg else "")
            sys.modules.setdefault(self._topname, module)
            source = self._path.read_text(encoding="utf-8")
            code = compile(source, str(self._path), "exec")
            exec(code, module.__dict__)

    class _SandboxFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == ADDON_PACKAGE:
                return None  # synthetic package already in sys.modules
            topname = fullname
            if fullname.startswith(ADDON_PACKAGE + "."):
                topname = fullname[len(ADDON_PACKAGE) + 1 :]
            if not topname or topname.startswith(("tests", "__pycache__")):
                return None  # never touch test collection or cache dirs
            rel = Path(*topname.split("."))
            candidate = ADDON_ROOT / rel
            if candidate.is_dir():
                file_path = candidate / "__init__.py"
            else:
                file_path = candidate.with_suffix(".py")
            if not file_path.is_file():
                return None  # not an addon source file
            spec = importlib.machinery.ModuleSpec(
                fullname,
                _SandboxModuleLoader(fullname, topname, file_path),
                origin=str(file_path),
            )
            spec.has_location = True
            if candidate.is_dir():
                # Mark package modules so ``__path__`` is set and submodule
                # imports (e.g. ``ui.colors``) resolve.
                spec.submodule_search_locations = [str(candidate)]
            return spec

    sys.meta_path.insert(0, _SandboxFinder())


def _import_addon_module(name: str):
    """Import addon submodule by name and register under both
    ``{ADDON_PACKAGE}.{name}`` and ``{name}`` in sys.modules.
    """
    full = f"{ADDON_PACKAGE}.{name}"
    if full not in sys.modules:
        mod = importlib.import_module(f".{name}", package=ADDON_PACKAGE)
        sys.modules[full] = mod
    sys.modules[name] = sys.modules[full]
    # Attach to package so relative imports in other submodules work
    parts = name.split(".")
    target = _pkg
    for i, part in enumerate(parts[:-1]):
        sub_full = f"{ADDON_PACKAGE}.{'.'.join(parts[:i+1])}"
        target = sys.modules.get(sub_full, target)
    setattr(target, parts[-1], sys.modules[full])
    return sys.modules[full]


_ADDON_MODULES_ORDERED = [
    "var_defs",
    "utils",
    "sentry_integration",
    "auth_manager",
    "api_client",
    "identifier",
    "thread",
    "media_exporter",
    "media_export",
    "media_import",
    "media_manager",
    "media_progress_indicator",
    "media_optimizer",
    "stats",
    "ui",
    "ui.__init__",
    "ui.colors",
    "dialogs",
    "crowd_anki",
    "crowd_anki.__init__",
    "crowd_anki.utils",
    "crowd_anki.utils.__init__",
    "crowd_anki.utils.constants",
    "crowd_anki.utils.uuid",
    "crowd_anki.utils.disambiguate_uuids",
    "crowd_anki.utils.notifier",
    "crowd_anki.utils.utils",
    "crowd_anki.utils.deckconf",
    "crowd_anki.utils.filesystem",
    "crowd_anki.utils.filesystem.__init__",
    "crowd_anki.utils.filesystem.name_sanitizer",
    "crowd_anki.representation",
    "crowd_anki.representation.__init__",
    "crowd_anki.representation.json_serializable",
    "crowd_anki.representation.note_model",
    "crowd_anki.representation.note",
    "crowd_anki.representation.deck_config",
    "crowd_anki.representation.deck",
    "crowd_anki.representation.deck_initializer",
    "crowd_anki.representation.benchmarking",
    "crowd_anki.config",
    "crowd_anki.config.__init__",
    "crowd_anki.config.config_settings",
    "crowd_anki.anki",
    "crowd_anki.anki.__init__",
    "crowd_anki.anki.adapters",
    "crowd_anki.anki.adapters.__init__",
    "crowd_anki.anki.adapters.anki_deck",
    "crowd_anki.anki.adapters.deck_manager",
    "crowd_anki.anki.adapters.file_provider",
    "crowd_anki.anki.adapters.note_model_file_provider",
    "crowd_anki.anki.adapters.hook_manager",
    "crowd_anki.export",
    "crowd_anki.export.__init__",
    "crowd_anki.export.note_sorter",
    "crowd_anki.importer",
    "crowd_anki.importer.__init__",
    "crowd_anki.importer.import_dialog",
    "export_manager",
    "import_manager",
    "menu",
    "hooks",
    "gear_menu_setup",
    "notifications_center",
]


def _import_addon_modules() -> None:
    """Import every addon submodule in dependency order.

    A production module that fails to import is a **hard error**: we must NOT
    fall back to a MagicMock, because then every dependent module's tests
    silently run against a mock instead of real code, and regressions in the
    broken module go unnoticed forever.  Fail test collection loudly instead,
    preserving the module name and the original traceback as the cause.
    """
    for _name in _ADDON_MODULES_ORDERED:
        try:
            _import_addon_module(_name)
        except Exception as _exc:
            raise RuntimeError(
                f"Failed to import production addon module '{_name}'. "
                f"Original error: {type(_exc).__name__}: {_exc}. "
                "Refusing to install a MagicMock fallback: a broken production "
                "module must fail test collection loudly so regressions cannot "
                "hide behind a mock."
            ) from _exc


_import_addon_modules()


# Tell pytest not to try collecting the addon's own files as tests
collect_ignore_glob = [
    "main.py",
    "menu.py",
    "hooks.py",
    "dialogs.py",
    "export_manager.py",
    "import_manager.py",
    "media_manager.py",
    "auth_manager.py",
    "identifier.py",
    "stats.py",
    "thread.py",
    "utils.py",
    "var_defs.py",
    "sentry_integration.py",
    "media_exporter.py",
    "media_export.py",
    "media_import.py",
    "media_optimizer.py",
    "media_progress_indicator.py",
    "gear_menu_setup.py",
    "notifications_center.py",
    "crowd_anki/*",
    "ui/*",
    "__pycache__/*",
    "dist/*",
]
