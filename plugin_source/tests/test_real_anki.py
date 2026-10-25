"""Run the real-Anki scenarios as part of the normal ``pytest tests`` run.

The root ``conftest.py`` installs fake ``aqt``/``anki`` modules in
``sys.modules`` for the unit suite. A real ``anki.Collection`` cannot coexist
with those fakes in one interpreter, so each scenario file is executed in a
clean subprocess with ``tests/real_anki/pytest.ini`` as its rootdir. That ini
prevents the addon's root conftest.py from being loaded.

If a real Anki backend is not installed (for example in CI), the runner skips;
the rest of the suite is unaffected. On a developer machine with Anki installed,
``pytest tests`` executes these scenarios too.

``ANKICOLLAB_REAL_ANKI`` controls the behaviour: ``0`` disables (recommended in
CI), ``1`` requires a backend, and the default ``auto`` probes and skips when
Anki is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ADDON_ROOT = TESTS_DIR.parent
REAL_ANKI_DIR = TESTS_DIR / "real_anki"
SCENARIO_FILES = sorted(path.name for path in REAL_ANKI_DIR.glob("test_*.py"))

_PROBE = (
    "import anki.collection, anki.models, aqt; "
    "assert isinstance(anki.collection.Collection, type)"
)

_DISABLED = {"0", "false", "no", "off", "disabled"}
_REQUIRED = {"1", "true", "yes", "on", "required"}


def pytest_generate_tests(metafunc):
    if "real_anki_file" in metafunc.fixturenames:
        metafunc.parametrize("real_anki_file", SCENARIO_FILES)


def _mode_from_env(value=None):
    """``disabled`` / ``required`` / ``auto`` from ANKICOLLAB_REAL_ANKI."""
    raw = os.environ.get("ANKICOLLAB_REAL_ANKI", "auto") if value is None else value
    raw = str(raw).strip().lower()
    if raw in _DISABLED:
        return "disabled"
    if raw in _REQUIRED:
        return "required"
    return "auto"


def _clean_env():
    env = dict(os.environ)
    # Never let the outer pytest invocation leak its current test/addopts into
    # the nested run.
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _probe_real_anki():
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=str(ADDON_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=_clean_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"exit code {proc.returncode}"
    return True, ""


@pytest.fixture(scope="session")
def real_anki_state():
    """State for the runner: available, unavailable, disabled or required.

    ``ANKICOLLAB_REAL_ANKI=0`` lets CI (which has no Anki install) skip without
    probing. ``=1`` turns a missing backend into a failure instead of a skip.
    The default ``auto`` probes a clean interpreter.
    """
    mode = _mode_from_env()
    if mode == "disabled":
        return "disabled", "disabled by ANKICOLLAB_REAL_ANKI"
    available, reason = _probe_real_anki()
    if available:
        return "available", ""
    return ("required" if mode == "required" else "unavailable"), reason


@pytest.mark.real_anki
def test_real_anki_scenario_files_are_discovered():
    assert SCENARIO_FILES, f"no real-Anki scenarios found in {REAL_ANKI_DIR}"


@pytest.mark.real_anki
def test_real_anki_scenario_file(real_anki_file, real_anki_state):
    state, reason = real_anki_state
    if state == "disabled":
        pytest.skip(f"real-Anki scenarios are {reason}")
    if state == "unavailable":
        pytest.skip(f"real Anki backend is not available: {reason}")
    if state == "required":
        pytest.fail(
            f"ANKICOLLAB_REAL_ANKI requires a real backend, but it is not "
            f"available: {reason}",
            pytrace=False,
        )

    command = [
        sys.executable,
        "-m",
        "pytest",
        real_anki_file,
        "-c",
        "pytest.ini",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(REAL_ANKI_DIR),
            capture_output=True,
            text=True,
            timeout=600,
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"real-Anki scenario {real_anki_file} timed out after 600s",
            pytrace=False,
        )

    if proc.returncode != 0:
        pytest.fail(
            f"real-Anki scenario {real_anki_file} failed "
            f"(exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}",
            pytrace=False,
        )
