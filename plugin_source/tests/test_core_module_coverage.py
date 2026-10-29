"""Machine-checkable core-module inventory (Phase 0.3).

Every module in :data:`CORE_MODULES` must be imported by at least one test
file.  This is a cheap tripwire against a brand-new core file shipping with no
tests at all — e.g. adding ``sync_manager.py`` without a matching test file
fails this check loudly, instead of silently shipping uncovered logic.

The check is deliberately simple (AST scan of test-file imports), not a
coverage measurement: it is a *structural* guarantee that every core module is
referenced somewhere in the suite, which Phase 1 then strengthens into "has a
test file exercising its real logic directly".
"""

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ADDON_ROOT = TESTS_DIR.parent

# Explicit core modules with real (non-Qt-rendering) logic that must never be
# left with zero test references.  Keep this list in sync with the audit.
CORE_MODULES = [
    "api_client",
    "auth_manager",
    "consent",
    "identifier",
    "export_manager",
    "import_manager",
    "media_manager",
    "media_export",
    "media_import",
    "media_exporter",
    "media_optimizer",
    "stats",
    "utils",
    "menu",
    "hooks",
    "sentry_integration",
]

# Everything under crowd_anki/representation and crowd_anki/anki.
for _subdir in ("representation", "anki"):
    _dir = ADDON_ROOT / "crowd_anki" / _subdir
    if _dir.is_dir():
        CORE_MODULES.append(f"crowd_anki.{_subdir}")
        for _py in sorted(_dir.rglob("*.py")):
            if _py.name == "__init__.py" or _py.name.startswith("_"):
                continue
            rel = _py.relative_to(ADDON_ROOT).with_suffix("")
            # Normalize BOTH path separators to dots: on Windows the relative
            # path uses "\\", on POSIX "/".  Without the "/" replacement this
            # inventory silently fails on Linux CI (module names stay
            # "crowd_anki/representation/deck" and never match dotted imports).
            CORE_MODULES.append(str(rel).replace("\\", ".").replace("/", "."))

CORE_MODULES = sorted(set(CORE_MODULES))


def _test_imported_modules():
    """Return the set of dotted import paths referenced by test files."""
    imported = set()
    for py in sorted(TESTS_DIR.rglob("test_*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                # ``from crowd_anki.representation import deck_initializer``
                # imports the submodule ``crowd_anki.representation.deck_initializer``.
                for alias in node.names:
                    if alias.name != "*":
                        imported.add(f"{node.module}.{alias.name}")
    return imported


def test_core_modules_list_is_wellformed():
    assert len(CORE_MODULES) >= 15
    assert "menu" in CORE_MODULES
    assert "hooks" in CORE_MODULES
    assert "sentry_integration" in CORE_MODULES
    assert any(m.startswith("crowd_anki.representation.deck") for m in CORE_MODULES)


def test_every_core_module_has_a_test_reference():
    imported = _test_imported_modules()

    uncovered = []
    for module in CORE_MODULES:
        referenced = any(
            imp == module or imp.startswith(module + ".") for imp in imported
        )
        if not referenced:
            uncovered.append(module)

    assert not uncovered, (
        "These core modules have zero references from any test file:\n"
        + "\n".join(f"  - {m}" for m in uncovered)
        + "\n\nA brand-new core module must ship with at least one test that "
        "imports it (Phase 1 requires exercising its real logic directly)."
    )
