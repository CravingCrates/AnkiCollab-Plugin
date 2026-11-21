"""Unit-level conftest — provides an autouse fixture that patches
``aqt.mw`` for every test.

The root conftest already installed fake aqt/anki modules and
imported all addon submodules before this file is reached.
"""

import sys
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

import startup_sync
from tests.mocks import create_mock_mw

# Dynamically determine addon package name from directory
UNIT_DIR = Path(__file__).resolve().parent
TESTS_DIR = UNIT_DIR.parent
ADDON_ROOT = TESTS_DIR.parent
ADDON_PACKAGE = ADDON_ROOT.name

# Modules that bind ``mw`` at import time via ``from aqt import mw``.
# We must patch the module-level ``mw`` attribute in each so that
# test-provided mocks take effect.
_MW_MODULES = [
    "utils",
    "auth_manager",
    "stats",
    "import_manager",
    "startup_sync",
    "export_manager",
    "media_manager",
    "notifications_center",
    "menu",
    "hooks",
    "sentry_integration",
]


@pytest.fixture(autouse=True)
def mw_mock(sample_config):
    """Autouse fixture: every unit test gets a fresh ``aqt.mw`` MagicMock.

    The mock is built from :func:`create_mock_mw` with the default
    ``sample_config`` (from root conftest).  Individual tests can
    override by re-patching or by creating their own ``mw_mock``.

    In addition to patching ``aqt.mw``, this fixture patches the
    module-level ``mw`` binding in every addon module that does
    ``from aqt import mw``.
    """
    mock = create_mock_mw(config=sample_config)

    patchers = [patch("aqt.mw", mock)]

    for mod_short in _MW_MODULES:
        full_name = f"{ADDON_PACKAGE}.{mod_short}"
        mod = sys.modules.get(full_name)
        if mod and not isinstance(mod, MagicMock) and hasattr(mod, "mw"):
            patchers.append(patch.object(mod, "mw", mock))

    for p in patchers:
        p.start()
    sys.modules["aqt"].mw = mock

    startup_sync.reset_state()
    try:
        yield mock
    finally:
        startup_sync.reset_state()
        for p in patchers:
            p.stop()


@pytest.fixture
def mw_no_col(mw_mock):
    """A variant where ``mw.col`` is ``None`` — collection unavailable."""
    mw_mock.col = None
    return mw_mock
