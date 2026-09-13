# AnkiCollab Test Suite

## Quick Start

```bash
# From the addon root (1957538407/)
python -m pytest tests/            # run all tests
python -m pytest tests/ -x         # stop on first failure
python -m pytest tests/ -k utils   # run only tests matching "utils"
python -m pytest tests/integration -m integration   # run only workflow-level tests
```

**Current test count: 831** (CI verifies this against `pytest --collect-only`; if you
add or remove tests, update this number in the same change).

The core data-integrity files (`crowd_anki/representation/deck.py`,
`crowd_anki/representation/note.py`, `export_manager.py`, `import_manager.py`)
are also protected by per-file coverage floors enforced by
`tests/check_core_coverage_floors.py` (run after the coverage step, parsing
`coverage.xml`). Raise a floor as coverage improves; never lower one without
an explicit justification in the commit message.

Requires: `pytest`, `pytest-asyncio`, `requests-mock`, `factory-boy`, `pytest-cov`,
`keyring`, `pygtrie` (all installable via pip).

---

## Architecture Overview

### The Problem

The addon runs inside Anki, which provides `aqt.mw` (the main window singleton)
and `anki.*` packages. Every addon module does `from aqt import mw` at import
time, creating a module-level binding. Outside Anki, none of these packages
exist.

Additionally, the addon's `__init__.py` does `from .main import *`, which
triggers heavy side effects (UI hooks, menu registrations, etc.) that crash
without a running Anki instance. All modules use **relative imports**
(`from .var_defs import ...`), so they must be loaded as submodules of the
numeric package `1957538407`.

### The Solution: Two-Layer Bootstrap

#### Layer 1: Root `conftest.py` (addon root)

Runs before pytest collects any test files. It:

1. **Sets `SKIP_INIT=1`** in the environment
2. **Installs fake modules** (`aqt`, `aqt.qt`, `anki.*`, `sentry_sdk`, etc.)
   as `MagicMock` objects in `sys.modules`.
3. **Registers the addon as a package** — creates a dummy `types.ModuleType`
   for `1957538407` in `sys.modules`, preventing pytest from importing the
   real `__init__.py` (which would run `from .main import *`).
4. **Pre-registers `main` as a MagicMock** — `main.py` has circular deps
   and side effects; other modules that `from . import main` get the mock.
5. **Imports all addon submodules in dependency order** via
   `importlib.import_module(".module_name", package="1957538407")`,
   registering each under both `1957538407.module_name` and `module_name`
   in `sys.modules`. If any module fails to import, a MagicMock fallback
   is registered so downstream modules don't cascade-fail.

Key detail: `sys.modules["__init__"] = _pkg` prevents pytest's
`--import-mode=importlib` from re-importing the real `__init__.py` during
package collection.

#### Layer 2: Unit `tests/unit/conftest.py`

Provides the autouse `mw_mock` fixture that runs before every test:

1. Builds a fresh `MagicMock` via `create_mock_mw(config=sample_config)`.
2. **Patches `aqt.mw`** so any code doing `import aqt; aqt.mw` sees the mock.
3. **Patches the module-level `mw`** in every addon module that imported it
   at load time (`utils.mw`, `auth_manager.mw`, `stats.mw`, etc.).
   This is critical: `from aqt import mw` binds `mw` at import time,
   so patching only `aqt.mw` does NOT update the already-bound reference.

---

## File Layout

```
1957538407/
├── conftest.py                 # Root bootstrap (fake modules, package registration)
├── pytest.ini                  # Test configuration
├── tests/
│   ├── __init__.py             # Empty (marks tests as package)
│   ├── conftest.py             # Shared fixtures (tmp_media_dir, sample_config, create_mock_collection)
│   ├── mocks.py                # create_mock_mw(), FakeNote, FakeCard, FakeCol (strict core interfaces)
│   ├── factories.py            # factory_boy factories for test data
│   ├── TESTING.md              # This file
│   ├── test_bootstrap.py       # Bootstrap integrity: broken imports fail collection loudly
│   ├── test_core_module_coverage.py  # Core-module inventory: no core file with zero test refs
│   ├── integration/            # Workflow-level tests (marked `integration`)
│   │   ├── test_export_workflow.py
│   │   ├── test_import_workflow.py
│   │   ├── test_export_import_roundtrip.py
│   │   ├── test_media_reference_update_workflow.py
│   │   └── test_cancellation_and_failure.py
│   └── unit/
│       ├── __init__.py
│       ├── conftest.py         # Autouse mw_mock fixture (patches all modules)
│       ├── test_api_client.py
│       ├── test_auth_manager.py
│       ├── test_bootstrap_strictness.py  # Meta-test: fake Anki objects reject typos
│       ├── test_crowd_anki_adapters.py
│       ├── test_crowd_anki_anki_misc.py
│       ├── test_crowd_anki_representation_misc.py
│       ├── test_deck_chunking.py         # CHUNK_SIZE batching boundaries
│       ├── test_deck_import.py
│       ├── test_export.py
│       ├── test_export_manager.py
│       ├── test_hooks.py
│       ├── test_http_contracts.py        # Calls real payload builders
│       ├── test_identifier.py
│       ├── test_import_manager.py
│       ├── test_import_safety.py
│       ├── test_media_glue.py
│       ├── test_media_manager.py
│       ├── test_media_optimizer.py
│       ├── test_menu_logic.py
│       ├── test_note_import.py
│       ├── test_note_model.py
│       ├── test_notifications_center.py
│       ├── test_sentry_integration.py
│       ├── test_stats.py
│       ├── test_utils.py
│       └── test_var_defs.py
```

---

## What Each Test File Covers

### `test_var_defs.py` (9 tests)
Constants and configuration: `API_BASE_URL`, `VERSION`, `DEFAULT_PROTECTED_TAGS`,
tag/field prefixes, Sentry config. These break immediately if you rename or
remove a constant.

### `test_utils.py` (~35 tests)
Core utility layer — the most critical file:
- **Collection guards**: `ensure_collection()`, `is_collection_available()`,
  `check_collection_or_abort()` — tests both success and `mw.col=None` paths.
- **DeckManager**: Filtering reserved keys (`settings`, `auth`), hash lookup,
  iteration, context-manager save, None config resilience.
- **Lookup chain**: `get_timestamp()` → `get_hash_from_local_id()` →
  `get_did_from_hash()` → `get_local_deck_from_hash()` → `get_deck_hash_from_did()`
  with parent fallback → `get_deck_hash_from_card()` with odid + dynamic deck.
- **`get_deck_and_subdecks()`**: Recursive child traversal, invalid ID guards.
- **Backup**: Input validation (`bg+critical`), no-collection paths.

### `test_auth_manager.py` (26 tests)
Full authentication lifecycle:
- Init with empty/existing config, token storage with numeric/ISO/bad expiry.
- Token refresh flow — success, HTTP failure, network exception, missing refresh token.
- **Auto-refresh on `get_token()`** — verifies transparent refresh when near expiry.
- `is_logged_in()`, auto-approve get/set, `logout()` (server + local cleanup).

### `test_export_manager.py` (22 tests)
Media reference extraction — tests real compiled regex objects:
- Sound patterns `[sound:file.mp3]`, HTML `<img src>`, `<audio src>`,
  `<object data>`, case insensitivity, multiple matches per field.
- `_is_valid_media_file()` with real filesystem (100-byte threshold, missing, None).
- `_filter_valid_filename_mapping()` with real files on disk.
- `_handle_operation_aborted()` — distinguishes OperationAbortedError from others.

### `test_identifier.py` (9 tests)
User hash + subscription API calls (requests are mocked, logic is real):
- `get_user_hash()`: token available → success, no token → None, exception → None.
- `subscribe/unsubscribe_to_deck()`: success, no user hash, server error.

### `test_import_manager.py` (23 tests)
Deck import pipeline:
- `_fetch_manifest()`: real error handling (network, invalid JSON).
- `_safe_destination()`: path traversal attack prevention, normal resolution.
- `_coerce_subscription_payload()`: dict/list/string/no-deck-key validation.
- `_extract_media_entries()`: real zip extraction to temp dir, skip-existing,
  path prefix filtering, no-collection guard.
- **Optional tags**: lookup, change detection.
- **Note ID lookups**: batch `get_noteids_from_uuids()` / `get_guids_from_noteids()`.
- `wants_to_share_stats()`, `do_nothing()`.

### `test_stats.py` (11 tests)
Review history and analytics:
- `ReviewHistory` construction (chains `get_did_from_hash` + `get_deck_and_subdecks`).
- `calc_retention()`: boundary cases (0/0, None/None, all passed, all failed).
- `get_card_data()`: groups by deck + guid, aggregates retention/lapses/reps.
- `upload_review_history()`: compression pipeline (gzip→b64→POST), skip on no hash.
- `update_stats_timestamp()`: DeckManager context-manager mutation.

### `test_media_manager.py` (25 tests)
Media upload/download infrastructure:
- Exception class hierarchy verification.
- Constants validation (MAX_FILE_SIZE, extensions, content type map coverage).
- `RateLimiter`: token bucket — init validation, consumption, multi-request drain.
- `retry` decorator: first-try success, retry-then-recover, exhaust retries,
  no-retry on 404/401/executor_unavailable (status code inspection).
- `MediaManager` init: validation, trailing slash stripping, invalid folder.
- Helpers: `_file_exists_with_size`, `_is_anki_available` (with/without collection),
  `_ensure_executor_available` (shutdown → RuntimeError vs recreation).

> Note: `test_thread.py` was previously listed here but does not exist.  The
> thread helpers (`run_function_in_thread`, `sync_run_async`, …) currently have
> no dedicated test file — a known gap tracked for a future pass.

---

## The `mw_mock` Fixture

The `mw_mock` fixture (autouse in unit tests) provides:

| Attribute | Behavior |
|---|---|
| `mw.col` | `FakeCol` with mocked `.decks`, `.db`, `.models`, `.media`, etc. |
| `mw.addonManager.getConfig(id)` | Returns a mutable dict (shared `_config_store`) |
| `mw.addonManager.writeConfig(id, cfg)` | Updates the same `_config_store` |
| `mw.taskman.run_on_main(fn)` | Calls `fn()` synchronously |
| `mw.progress.want_cancel()` | Returns `False` |

Tests that need custom config use `mw_mock.addonManager.getConfig.side_effect`
to override the default. Tests that need `mw.col = None` use the `mw_no_col` fixture.

---

## How Tests Will Catch Real Bugs

Every test calls the **actual business function** with controlled inputs and
checks output/behavior. The mock layer only replaces Anki's runtime
(`aqt.mw`, `mw.col.db`, network calls). If you change any of these:

- **DeckManager iteration logic** → `test_filters_settings_and_auth` fails
- **Timestamp parsing format** → `test_get_timestamp_returns_float` fails
- **Deck hash lookup chain** → `test_direct_match`, `test_falls_back_to_parent` fail
- **Filtered deck detection** → `test_dynamic_deck_returns_filtered_error` fails
- **Token refresh threshold** → `test_near_expiry_returns_true` fails
- **Media regex patterns** → `test_sound_regex_simple`, `test_img_src_*` fail
- **Path traversal guard** → `test_traversal_raises` fails
- **Media extraction skip logic** → `test_skips_existing_files` fails
- **Retention calculation** → `test_all_passed`, `test_all_failed` fail
- **Retry no-retry conditions** → `test_no_retry_on_404`, `test_no_retry_on_401` fail

---

## Adding New Tests

1. Import from the **short module name** (e.g., `from utils import DeckManager`).
2. Use `mw_mock` fixture for anything touching `mw`. Override config via
   `mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(my_config)`.
3. Mock network calls with `@patch("module_name.requests.post")`.
4. For new modules that use `from aqt import mw`, add the module name to
   `_MW_MODULES` in `tests/unit/conftest.py`.
5. For new addon submodules, add them to `_ADDON_MODULES_ORDERED` in the
   root `conftest.py` (respect dependency order).

---

## Known Limitations

- **No UI tests**: Dialogs, menus, and Qt widgets are fully mocked. Testing
  those requires `pytest-anki` with a real Anki instance.
- **`main.py` is a MagicMock**: The main module triggers too many side effects.
  Functions it exports can't be unit tested this way.
- **`.oga` content type gap**: `.oga` is in `ALLOWED_EXTENSIONS` but missing
  from `CONTENT_TYPE_MAP` — documented in test, not a test bug.
- **Fake Anki collection**: The real Anki API is not importable outside a
  running Anki instance, so the suite fakes it (see `mocks.py` and
  `tests/integration/conftest.py`).  The core collection interfaces
  (`decks`, `models`, `db`, `tags`, `media`) are strict — a typo'd/renamed
  Anki API call raises `AttributeError` instead of silently passing.

## Harness Integrity (added in the hardening pass)

- **Broken production imports fail collection loudly.**  The root `conftest.py`
  never installs a `MagicMock` fallback for a production module that fails to
  import; `tests/test_bootstrap.py` proves it.  Previously three modules were
  silently mocked (`media_progress_indicator`, `deck_manager`, `media_import`).
- **`calc_retention` mid-range is locked.**  `int(passed * 100 / total)` is
  covered at 33/50/66/100/0 and no-data boundaries, so a regression to
  `int(passed / total) * 100` (which silently zeroes retention below 100%)
  fails immediately.
- **Constant assertions test behavior, not `> 0`.**  `BATCH_UPDATE_NOTES_SIZE`,
  `ASYNC_MEDIA_REF_THRESHOLD`, and `CHUNK_SIZE` are tested at their exact
  boundary values.
- **HTTP contract tests call the real payload builders** (e.g.
  `subscribe_to_deck()`, `_submit_deck_op()`), not hand-built payloads.
- **Workflow tests** under `tests/integration/` run export, import, round-trip,
  media-reference, and cancellation/failure through the real orchestration code
  against a realistic in-memory collection.

## Coverage

Coverage is enforced in CI with a real (measured) baseline: `pytest --cov` with
`--cov-fail-under=30` (see `.github/workflows/ci.yml`).  The `.coveragerc` scopes
measurement to the addon's own source (excluding vendored `dist/`, Qt-only `ui/`,
and `dialogs.py`).  The floor should be raised as coverage of the core modules
improves.
