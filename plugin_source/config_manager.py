"""Per-profile settings, auth and device-id storage for AnkiCollab.

This module is the only way the rest of the add-on reads or writes settings,
auth tokens and the device id. Everything lives inside
``<addon_root>/user_files/``, which Anki preserves across add-on updates and
deletes when the add-on is uninstalled::

    user_files/
      state.json
      profiles/
        <slug>-<hash12>/
          profile.json
          config.json
          auth.json
          device.json

Known limitation: profiles are keyed by profile *name*. Renaming a profile in
Anki does not move its AnkiCollab data -- a new directory is created for the
new name and the old one is left orphaned. No orphan cleanup is performed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ankicollab")

SCHEMA = 1
CONFIG_FILE = "config.json"
AUTH_FILE = "auth.json"
DEVICE_FILE = "device.json"
PROFILE_FILE = "profile.json"
STATE_FILE = "state.json"
PROFILES_DIR = "profiles"

_lock = threading.RLock()

# Module state. Every post-activation access happens under ``_lock``.
_user_files_dir_override: Optional[Path] = None
_current_profile_name: Optional[str] = None
_profile_dir: Optional[Path] = None
_config_cache: Dict[str, Any] = {}
_auth_cache: Dict[str, Any] = {}
_device_id_cache: Optional[str] = None
_defaults_cache: Optional[Dict[str, Any]] = None
_listeners: List[Callable[[str], None]] = []
_initialised = False
_no_profile_logged = False

_SLUG_RE = re.compile(r"[^a-z0-9_-]")


# ---------------------------------------------------------------------------
# Pure helpers (no aqt)
# ---------------------------------------------------------------------------


def normalize_profile_name(name: str) -> str:
    """NFC-normalize a profile name.

    macOS can hand back decomposed Unicode, which would otherwise create a
    second directory for a profile that is already stored.
    """
    return unicodedata.normalize("NFC", name)


def profile_slug(name: str) -> str:
    """Lowercase ASCII ``[a-z0-9_-]`` characters, truncated, never empty."""
    slug = _SLUG_RE.sub("", normalize_profile_name(name).lower())
    slug = slug[:20]
    return slug or "profile"


def profile_dir_name(name: str) -> str:
    """Directory name for a profile: ``<slug>-<hash12>``.

    The hash disambiguates names that collide after slugging, differ only by
    case on a case-insensitive filesystem, or normalize to the same NFC form.
    """
    digest = hashlib.sha256(normalize_profile_name(name).encode("utf-8")).hexdigest()
    return f"{profile_slug(name)}-{digest[:12]}"


def deep_merge(defaults: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    """Merge *user* over *defaults* without mutating either.

    Dicts merge recursively, user values win, keys only present in *defaults*
    are kept so new keys added in later add-on versions appear for existing
    users, and lists/scalars are replaced wholesale.
    """
    result = deepcopy(defaults) if isinstance(defaults, dict) else {}
    if not isinstance(user, dict):
        return result
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------


def _quarantine(path: Path, reason: BaseException) -> None:
    """Move an unreadable file aside instead of deleting it."""
    target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        os.replace(path, target)
        logger.warning(
            "AnkiCollab: %s was unreadable (%s); moved it to %s", path, reason, target
        )
    except OSError as exc:
        logger.warning(
            "AnkiCollab: %s was unreadable (%s) and could not be moved: %s",
            path,
            reason,
            exc,
        )


def _load_json_object(path: Path) -> Optional[Dict[str, Any]]:
    """Read *path* as a JSON object, or ``None`` when it cannot be read so.

    ``None`` means "no usable object here": missing, unreadable, or not a JSON
    object. A corrupt file is quarantined, never deleted. This is deliberately
    weaker than :func:`_read_json`, because one caller -- the legacy auth scan
    -- must tell "unreadable" apart from "a valid, empty object".
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _quarantine(path, exc)
        return None
    except OSError as exc:
        logger.warning("AnkiCollab: could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        _quarantine(path, ValueError("JSON value is not an object"))
        return None
    return data


def _warn_if_newer_schema(data: Dict[str, Any], path: Path) -> None:
    """Warn when a file was written by a newer version of the add-on.

    The data is still returned untouched: discarding unknown keys would lose
    the user's data on a downgrade, which is far worse than reading a shape we
    do not fully understand.
    """
    schema = data.get("schema")
    if isinstance(schema, int) and schema > SCHEMA:
        logger.warning(
            "AnkiCollab: %s uses schema %s but this version understands %s; "
            "reading it as-is and keeping unknown keys",
            path,
            schema,
            SCHEMA,
        )


def _read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON object. Missing, unreadable or corrupt -> ``{}``.

    Never raises: this is called on hot paths where a crash would take Anki
    down. A corrupt file is renamed, never deleted.
    """
    data = _load_json_object(path)
    if data is None:
        return {}
    _warn_if_newer_schema(data, path)
    return data


def _replace_with_retry(
    source: Path, target: Path, attempts: int = 5, delay: float = 0.05
) -> None:
    """``os.replace`` with a short backoff.

    On Windows antivirus and indexers briefly lock freshly written files,
    which surfaces as ``PermissionError``.
    """
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def _write_json(path: Path, data: Dict[str, Any], *, private: bool = False) -> None:
    """Atomically write *data* to *path*.

    The temp file is created in the destination directory so ``os.replace``
    stays atomic on the same filesystem, and is always cleaned up on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        handle_fd, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_path, path)
        temp_path = None
        if private and os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                logger.warning("AnkiCollab: could not restrict %s: %s", path, exc)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Test hooks (never called by production code)
# ---------------------------------------------------------------------------


def reset_for_tests(directory: Optional[Path] = None) -> None:
    """Test-only: clear all module state and optionally redirect storage.

    Pass a directory to use as ``user_files``; pass ``None`` to restore normal
    resolution.
    """
    global _user_files_dir_override, _current_profile_name, _profile_dir
    global _config_cache, _auth_cache, _device_id_cache, _defaults_cache
    global _initialised, _no_profile_logged
    with _lock:
        _user_files_dir_override = Path(directory) if directory is not None else None
        _current_profile_name = None
        _profile_dir = None
        _config_cache = {}
        _auth_cache = {}
        _device_id_cache = None
        _defaults_cache = None
        _initialised = False
        _no_profile_logged = False
        _listeners.clear()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def _addon_id(manager: Any) -> str:
    """This add-on's directory name, via Anki's snake_case API when present.

    ``AddonManager.addon_from_module`` was only added after Anki 2.1.50 -- the
    oldest version this add-on still declares support for -- so fall back to
    the softly-deprecated ``addonFromModule``.
    """
    resolver = getattr(manager, "addon_from_module", None)
    if resolver is None:
        resolver = manager.addonFromModule
    return resolver(__name__)


def _addon_root() -> Path:
    """The add-on's folder.

    * ``addon_from_module`` handles ``__name__`` being a submodule
      (``"123456.sub.module"`` -> ``"123456"``).
    * The ``__file__`` fallback covers unit tests and any Anki version where
      the add-on manager is unavailable. It is only correct while this module
      sits in the add-on root.
    """
    try:
        from aqt import mw

        manager = getattr(mw, "addonManager", None)
        if manager is not None:
            addon = _addon_id(manager)
            folder = manager.addonsFolder(addon)
            if isinstance(folder, str) and folder:
                return Path(folder)
    except Exception as exc:
        logger.warning("AnkiCollab: falling back to __file__ for the root: %s", exc)
    return Path(__file__).resolve().parent


def user_files_dir() -> Path:
    """Root of all AnkiCollab state.

    The override is checked *before* any ``aqt`` access so tests can never
    fall through to the real add-on folder.
    """
    if _user_files_dir_override is not None:
        return Path(_user_files_dir_override)
    return _addon_root() / "user_files"


def _load_shipped_defaults() -> Dict[str, Any]:
    """Read the shipped defaults, uncached.

    Prefers Anki's own accessor and falls back to reading the add-on's
    ``config.json`` directly so the helpers stay testable without Anki.
    """
    data: Any = None
    try:
        from aqt import mw

        manager = getattr(mw, "addonManager", None)
        if manager is not None:
            data = manager.addonConfigDefaults(_addon_id(manager))
    except Exception as exc:
        logger.warning("AnkiCollab: could not read shipped defaults: %s", exc)
        data = None
    if not isinstance(data, dict):
        data = _read_json(_addon_root() / CONFIG_FILE)
    return data or {}


def _shipped_defaults() -> Dict[str, Any]:
    """Cached copy of the shipped defaults. Callers may mutate the result."""
    global _defaults_cache
    with _lock:
        if _defaults_cache is None:
            _defaults_cache = _load_shipped_defaults()
        return deepcopy(_defaults_cache)


def profile_dir(profile_name: str) -> Path:
    """Directory holding one profile's files."""
    return user_files_dir() / PROFILES_DIR / profile_dir_name(profile_name)


# ---------------------------------------------------------------------------
# Global state file
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return user_files_dir() / STATE_FILE


def _read_state() -> Dict[str, Any]:
    return _read_json(_state_path())


def _write_state(state: Dict[str, Any]) -> None:
    payload: Dict[str, Any] = {"schema": SCHEMA}
    payload.update(state)
    _write_json(_state_path(), payload)


# ---------------------------------------------------------------------------
# One-time migration of legacy (pre-profile) installs
# ---------------------------------------------------------------------------

#: Where older versions kept auth.json, newest first. The first existing file
#: is treated as the legacy source; every path is deleted after a successful
#: migration.
LEGACY_AUTH_PATHS = ("user_files/auth.json", "auth.json")


def _legacy_auth_paths() -> List[Path]:
    """Legacy auth locations, relative to the add-on root.

    Derived from :func:`user_files_dir` rather than :func:`_addon_root` so the
    test override redirects these lookups too. Without that, ``_addon_root()``
    falls back to the real add-on folder during a test run, ``_find_legacy_auth``
    finds the developer's live ``user_files/auth.json``, and step 5 of the
    migration deletes it as "consumed legacy data". Never resolve a
    *deletable* path through anything but the override-aware helper.
    """
    return [user_files_dir().parent / name for name in LEGACY_AUTH_PATHS]


def _find_legacy_auth() -> Optional[Dict[str, Any]]:
    """The first legacy auth file that parses as a JSON object, or ``None``.

    An empty object counts as a source: the stale file must still be consumed
    and removed. A candidate that exists but cannot be read as an object is
    skipped rather than ending the search -- otherwise a corrupt leftover would
    shadow a valid one, and migration would delete the valid tokens without
    ever consuming them (spec section 8: "the first one found that parses as a
    dict").
    """
    for path in _legacy_auth_paths():
        if not path.is_file():
            continue
        data = _load_json_object(path)
        if data is not None:
            return data
        logger.warning(
            "AnkiCollab: %s is not a readable legacy auth file; "
            "trying the next legacy location",
            path,
        )
    return None


def _read_legacy_config() -> Optional[Dict[str, Any]]:
    """The global config from ``meta.json``, or ``None`` when there is none."""
    try:
        from aqt import mw

        manager = getattr(mw, "addonManager", None)
        if manager is None:
            return None
        meta = manager.addonMeta(_addon_id(manager))
        if not isinstance(meta, dict):
            return None
        legacy = meta.get("config")
        if isinstance(legacy, dict) and legacy:
            return legacy
    except Exception as exc:
        logger.warning("AnkiCollab: could not read the legacy config: %s", exc)
    return None


def _delete_file(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("AnkiCollab: could not delete %s: %s", path, exc)


def _delete_legacy_config() -> None:
    """Drop the ``config`` key from ``meta.json``."""
    try:
        from aqt import mw

        manager = getattr(mw, "addonManager", None)
        if manager is None:
            return
        addon = _addon_id(manager)
        meta = manager.addonMeta(addon)
        if not isinstance(meta, dict) or "config" not in meta:
            return
        del meta["config"]
        writer = getattr(manager, "writeAddonMeta", None)
        if callable(writer):
            writer(addon, meta)
        else:
            logger.warning(
                "AnkiCollab: writeAddonMeta is unavailable; restoring shipped defaults"
            )
            manager.writeConfig(addon, _load_shipped_defaults())
    except Exception as exc:
        logger.warning("AnkiCollab: could not delete the legacy config: %s", exc)


def _has_content(path: Path) -> bool:
    """True when *path* holds a non-empty JSON object."""
    return bool(_read_json(path))


def _strip_auth_section(config: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy config without its inlined ``auth`` block."""
    if not isinstance(config, dict):
        return {}
    return {key: value for key, value in config.items() if key != "auth"}


def _build_migrated_auth(
    legacy_config: Optional[Dict[str, Any]], legacy_auth: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Merge every legacy auth source into one dict.

    Before ``auth.json`` existed the secrets lived inside ``config["auth"]``
    (see ``AuthManager._load_auth_data``), so both the whole section and the
    separate file contribute. The file wins where they overlap.
    """
    result: Dict[str, Any] = {}
    if isinstance(legacy_config, dict):
        section = legacy_config.get("auth")
        if isinstance(section, dict):
            result.update(section)
    if isinstance(legacy_auth, dict):
        result.update(legacy_auth)
    return result


def _write_migrated_files(
    directory: Path,
    legacy_config: Optional[Dict[str, Any]],
    legacy_auth: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Write what is missing, never overwriting existing profile data.

    Returns the payloads that were written so the caller can verify them.
    """
    written: Dict[str, Dict[str, Any]] = {}

    config_path = directory / CONFIG_FILE
    if legacy_config is not None and not _has_content(config_path):
        payload = deep_merge(_shipped_defaults(), _strip_auth_section(legacy_config))
        _write_json(config_path, payload)
        written[CONFIG_FILE] = payload

    auth_path = directory / AUTH_FILE
    if not _has_content(auth_path):
        payload = _build_migrated_auth(legacy_config, legacy_auth)
        if payload:
            _write_json(auth_path, payload, private=True)
            written[AUTH_FILE] = payload

    return written


def _verify_migrated_files(directory: Path, written: Dict[str, Dict[str, Any]]) -> bool:
    """Re-read what was written. Only verified data may be deleted."""
    for name, payload in written.items():
        if _read_json(directory / name) != payload:
            return False
    return True


def _run_legacy_migration(profile_name: str, directory: Path) -> None:
    """Move legacy global data into *profile_name*, exactly once.

    Callable only from an activation path (main thread, under ``_lock``).
    Never raises: a broken migration, a corrupt file, or a read-only
    ``user_files`` must not stop Anki from opening a profile.
    """
    with _lock:
        try:
            state = _read_state()
            if state.get("legacy_migration") == "done":
                if _read_legacy_config() is not None or _find_legacy_auth() is not None:
                    logger.warning(
                        "AnkiCollab: legacy AnkiCollab data reappeared after "
                        "migration had already completed; leaving it untouched"
                    )
                return

            legacy_config = _read_legacy_config()
            legacy_auth = _find_legacy_auth()

            if legacy_config is None and legacy_auth is None:
                _write_state({"legacy_migration": "done"})
                return

            written = _write_migrated_files(directory, legacy_config, legacy_auth)
            if not _verify_migrated_files(directory, written):
                logger.warning(
                    "AnkiCollab: legacy migration could not be verified; "
                    "leaving the legacy data in place and retrying on the next open"
                )
                return

            _delete_legacy_config()
            for path in _legacy_auth_paths():
                _delete_file(path)

            _write_state(
                {
                    "legacy_migration": "done",
                    "migrated_to": directory.name,
                    "migrated_at": int(time.time()),
                }
            )
            logger.info(
                "AnkiCollab: migrated legacy settings into profile %r (%s)",
                profile_name,
                directory.name,
            )
        except Exception as exc:
            logger.warning("AnkiCollab: legacy migration failed: %s", exc)


def _migrate_auto_approve(directory: Path) -> None:
    """Move a legacy ``auto_approve`` preference out of ``auth.json``.

    Older versions stored this maintainer setting next to the credentials, so
    a failed token refresh -- which clears the credentials -- silently reset
    it. It now lives in ``config.json`` under ``settings``. Idempotent: once
    the key is gone from ``auth.json`` this is a no-op.

    Best-effort, like every other write during activation: leaving the value
    in the wrong file for one more run beats blocking the profile from opening.
    """
    auth_path = directory / AUTH_FILE
    auth = _read_json(auth_path)
    if "auto_approve" not in auth:
        return

    config_path = directory / CONFIG_FILE
    config = _read_json(config_path)
    settings = config.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    # A value already in config wins: it is the new home, the auth copy stale.
    settings.setdefault("auto_approve", bool(auth["auto_approve"]))
    config["settings"] = settings
    del auth["auto_approve"]

    try:
        # Config first: if the second write fails the value survives in both
        # files and the next activation finishes the move. The other order
        # could lose it.
        _write_json(config_path, config)
        _write_json(auth_path, auth, private=True)
    except OSError as exc:
        logger.warning("AnkiCollab: could not move auto_approve out of auth: %s", exc)


# ---------------------------------------------------------------------------
# Active profile
# ---------------------------------------------------------------------------


class NoActiveProfileError(RuntimeError):
    """Raised when a write is attempted with no Anki profile open.

    Silently dropping the write would lose the user's data, so writes fail
    loudly while reads keep working from defaults.
    """


def _is_main_thread() -> bool:
    """Anki's main thread is the process main thread.

    ``mw.pm`` and Qt objects may only be touched from it, which is why lazy
    activation and the hooks are both gated on this.
    """
    return threading.current_thread() is threading.main_thread()


def _profile_name_from_mw() -> Optional[str]:
    """``mw.pm.name``, or ``None``.

    ``isinstance(name, str)`` matters: in the unit-test harness ``mw`` is a
    ``MagicMock``, so ``mw.pm.name`` is a truthy MagicMock rather than a name.
    """
    try:
        from aqt import mw

        manager = getattr(getattr(mw, "pm", None), "name", None)
        if isinstance(manager, str) and manager:
            return manager
    except Exception as exc:
        logger.warning("AnkiCollab: could not read the profile name: %s", exc)
    return None


def _ensure_profile_file(directory: Path, profile_name: str) -> None:
    """Record the real profile name next to its mangled directory."""
    path = directory / PROFILE_FILE
    if path.is_file():
        return
    _write_json(path, {"schema": SCHEMA, "profile_name": profile_name})


def _activate(profile_name: str) -> None:
    """Load *profile_name* into the cache. Caller must hold ``_lock``.

    Never raises on a read-only or full ``user_files``: activation succeeds
    with whatever can be read and the file mutations are best-effort. A later
    write still fails loudly, because ``_write_json`` raises the real error.
    """
    global _current_profile_name, _profile_dir, _config_cache
    global _auth_cache, _device_id_cache

    directory = profile_dir(profile_name)
    _run_legacy_migration(profile_name, directory)
    _migrate_auto_approve(directory)

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "AnkiCollab: could not create the profile directory %s: %s", directory, exc
        )

    _current_profile_name = profile_name
    _profile_dir = directory
    _config_cache = _read_json(directory / CONFIG_FILE)
    _auth_cache = _read_json(directory / AUTH_FILE)

    device_payload = _read_json(directory / DEVICE_FILE)
    device_id = device_payload.get("device_id")
    _device_id_cache = device_id if isinstance(device_id, str) and device_id else None

    try:
        _ensure_profile_file(directory, profile_name)
    except OSError as exc:
        logger.warning(
            "AnkiCollab: could not record the profile name in %s: %s", directory, exc
        )


def _notify_listeners(profile_name: str) -> None:
    with _lock:
        listeners = list(_listeners)
    for callback in listeners:
        try:
            callback(profile_name)
        except Exception as exc:
            logger.warning("AnkiCollab: a profile listener failed: %s", exc)


def activate_profile(profile_name: str) -> None:
    """Make *profile_name* the active profile and notify listeners."""
    if not isinstance(profile_name, str) or not profile_name:
        return
    with _lock:
        _activate(profile_name)
    _notify_listeners(profile_name)


def activate_for_tests(profile_name: str) -> None:
    """Test-only: activate a profile without Anki. Never called in production."""
    activate_profile(profile_name)


def _activate_if_possible() -> Optional[str]:
    """Lazily activate on the main thread.

    Returns the profile name if this call activated one, else ``None``.
    Caller must hold ``_lock``. Background threads never activate: touching
    ``mw.pm`` there is forbidden, so they fall back to defaults.
    """
    if _current_profile_name is not None:
        return None
    if not _is_main_thread():
        return None
    name = _profile_name_from_mw()
    if name is None:
        return None
    _activate(name)
    return name


def _require_profile_dir() -> Path:
    if _profile_dir is None:
        raise NoActiveProfileError(
            "No Anki profile is open, so AnkiCollab cannot store settings yet."
        )
    return _profile_dir


def _log_no_profile_once() -> None:
    global _no_profile_logged
    if _no_profile_logged:
        return
    _no_profile_logged = True
    logger.warning(
        "AnkiCollab: no profile is open; settings are read-only defaults for now"
    )


def current_profile_name() -> Optional[str]:
    """The active profile's name, or ``None`` when no profile is open."""
    with _lock:
        activated = _activate_if_possible()
        name = _current_profile_name
    if activated is not None:
        _notify_listeners(activated)
    return name


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------


def get_config() -> Dict[str, Any]:
    """The active profile's config, deep-merged over the shipped defaults.

    Returns a deep copy: callers may mutate it freely. With no profile active
    this returns the defaults and logs once.
    """
    with _lock:
        activated = _activate_if_possible()
        if _current_profile_name is None:
            _log_no_profile_once()
            result = _shipped_defaults()
        else:
            result = deep_merge(_shipped_defaults(), _config_cache)
    if activated is not None:
        _notify_listeners(activated)
    return result


def get_stored_config() -> Dict[str, Any]:
    """The active profile's config exactly as stored, without shipped defaults.

    :func:`get_config` is the read view: it merges the shipped defaults in so
    callers always see a complete config. This is the write view's counterpart,
    for callers that write back what they read: re-writing a merged view would
    bake every shipped default into the user's file. Returns a deep copy, and
    ``{}`` when no profile is open.
    """
    with _lock:
        activated = _activate_if_possible()
        result = deepcopy(_config_cache)
    if activated is not None:
        _notify_listeners(activated)
    return result


def write_config(config: Dict[str, Any]) -> None:
    """Replace the active profile's whole config.

    Same semantics as Anki's ``writeConfig``: the stored file becomes exactly
    *config*, with no defaults merged in. Use :func:`set_value` to change a
    single key without materialising the defaults.
    """
    payload = config if isinstance(config, dict) else {}
    with _lock:
        activated = _activate_if_possible()
        directory = _require_profile_dir()
        _write_json(directory / CONFIG_FILE, payload)
        _config_cache.clear()
        _config_cache.update(deepcopy(payload))
    if activated is not None:
        _notify_listeners(activated)


def get_value(key: str, default: Any = None) -> Any:
    """Convenience wrapper around :func:`get_config`."""
    return get_config().get(key, default)


def get_auto_approve() -> bool:
    """Whether outgoing changes are auto-approved (maintainer-only setting).

    Stored in ``config.json`` under ``settings``, next to the other user
    preferences, rather than with the credentials: a failed token refresh
    clears the credentials and must not reset this.
    """
    settings = get_config().get("settings")
    if not isinstance(settings, dict):
        return False
    return bool(settings.get("auto_approve", False))


def set_value(key: str, value: Any) -> None:
    """Set one top-level key, preserving every other stored key.

    The write is applied to the *stored* dict rather than the merged view, so
    reading a config never bakes shipped defaults into the user's file.
    """
    with _lock:
        activated = _activate_if_possible()
        directory = _require_profile_dir()
        stored = deepcopy(_config_cache)
        stored[key] = value
        _write_json(directory / CONFIG_FILE, stored)
        _config_cache.clear()
        _config_cache.update(stored)
    if activated is not None:
        _notify_listeners(activated)


def update_values(updates: Dict[str, Any]) -> None:
    """Set several top-level keys in one atomic write.

    Plural form of :func:`set_value`: applied to the *stored* dict, so unowned
    keys and the absence of shipped defaults are both preserved, and only one
    file write happens no matter how many keys change.
    """
    if not isinstance(updates, dict) or not updates:
        return
    with _lock:
        activated = _activate_if_possible()
        directory = _require_profile_dir()
        stored = deepcopy(_config_cache)
        stored.update(updates)
        _write_json(directory / CONFIG_FILE, stored)
        _config_cache.clear()
        _config_cache.update(stored)
    if activated is not None:
        _notify_listeners(activated)


def add_profile_changed_listener(callback: Callable[[str], None]) -> None:
    """Register *callback*, called on the main thread after a profile is active.

    The callback runs after that profile's migration, so it may read config.
    """
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_profile_changed_listener(callback: Callable[[str], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------


def get_auth() -> Dict[str, Any]:
    """The active profile's auth data as a deep copy; ``{}`` when there is none.

    Treated as opaque: this module does not know which keys are secrets.
    """
    with _lock:
        activated = _activate_if_possible()
        result = deepcopy(_auth_cache) if _current_profile_name is not None else {}
    if activated is not None:
        _notify_listeners(activated)
    return result


def write_auth(auth: Dict[str, Any]) -> None:
    """Atomically replace the active profile's auth data.

    On POSIX the file is chmodded to 0o600 (best effort).
    """
    payload = auth if isinstance(auth, dict) else {}
    with _lock:
        activated = _activate_if_possible()
        directory = _require_profile_dir()
        _write_json(directory / AUTH_FILE, payload, private=True)
        _auth_cache.clear()
        _auth_cache.update(deepcopy(payload))
    if activated is not None:
        _notify_listeners(activated)


def clear_auth() -> None:
    """Logout: delete the active profile's auth data."""
    with _lock:
        activated = _activate_if_possible()
        directory = _require_profile_dir()
        _delete_file(directory / AUTH_FILE)
        _auth_cache.clear()
    if activated is not None:
        _notify_listeners(activated)


# ---------------------------------------------------------------------------
# Device id
# ---------------------------------------------------------------------------


def get_device_id() -> str:
    """A stable identifier for the active profile, sent when logging in.

    The server keeps one token row per account *per device*. Without a device
    id every device shares a single row, so signing in or refreshing on one
    device overwrites another device's token and silently signs it out.

    With no profile open this returns a process-local id instead of raising:
    an id that cannot be persisted costs one extra token row, which is much
    better than failing a login.
    """
    global _device_id_cache
    with _lock:
        activated = _activate_if_possible()
        if _current_profile_name is None:
            if _device_id_cache is None:
                _device_id_cache = str(uuid.uuid4())
                logger.warning(
                    "AnkiCollab: no profile is open; the device id will not persist"
                )
            return _device_id_cache

        if _device_id_cache is None:
            _device_id_cache = str(uuid.uuid4())
            try:
                _write_json(
                    _profile_dir / DEVICE_FILE,
                    {"schema": SCHEMA, "device_id": _device_id_cache},
                )
            except OSError as exc:
                # Never fail a login over this: an id that cannot be persisted
                # still works, it just costs one extra server token row per
                # restart until the directory becomes writable.
                logger.warning(
                    "AnkiCollab: could not save the device id to %s: %s",
                    _profile_dir / DEVICE_FILE,
                    exc,
                )
        result = _device_id_cache
    if activated is not None:
        _notify_listeners(activated)
    return result


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

#: Marks this module's handlers so a re-executed module can replace its own
#: stale copy instead of leaving two live handlers behind. Identity comparison
#: cannot do this: a reload rebinds the handler to a new function object.
_HOOK_MARKER = "_ankicollab_config_manager_hook"


def _hook_callbacks(hook_list: Any) -> List[Any]:
    """The callbacks a ``gui_hooks`` attribute currently holds.

    Anki's hook attributes are generated objects that expose only
    ``append``/``remove``/``count``/``__call__`` -- they are **not iterable** --
    and keep their callbacks in a private ``_hooks`` list. The unit-test
    harness swaps in plain lists instead, so both shapes must work, and an
    unknown shape must degrade to "nothing to remove" rather than raise.
    """
    hooks = getattr(hook_list, "_hooks", None)
    if isinstance(hooks, list):
        return list(hooks)
    if isinstance(hook_list, list):
        return list(hook_list)
    return []


def _on_profile_did_open() -> None:
    """Main-thread hook: activate whatever profile Anki just opened."""
    try:
        name = _profile_name_from_mw()
        if name is not None:
            activate_profile(name)
    except Exception as exc:
        logger.warning("AnkiCollab: could not activate the profile: %s", exc)


def _on_profile_will_close() -> None:
    """Deliberately does *not* clear the active profile.

    Other code (a sync-on-close step) may still need the config during
    shutdown. Writes are write-through, so there is nothing to flush; the
    state is replaced by the next ``profile_did_open``.
    """
    with _lock:
        logger.info("AnkiCollab: profile %r is closing", _current_profile_name)


_on_profile_did_open._ankicollab_config_manager_hook = True
_on_profile_will_close._ankicollab_config_manager_hook = True


def _register_config_action() -> None:
    """Point Tools > Add-ons > Config at the add-on's own settings dialog.

    Anki's default editor would edit the now-ignored global config, which
    would confuse users.
    """
    try:
        from aqt import mw

        manager = getattr(mw, "addonManager", None)
        setter = getattr(manager, "setConfigAction", None)
        if manager is None or not callable(setter):
            return
        addon = _addon_id(manager)

        def _open_settings():
            try:
                from .menu import show_global_settings_dialog

                show_global_settings_dialog(mw)
                return True
            except Exception as exc:
                logger.warning(
                    "AnkiCollab: could not open the settings dialog: %s", exc
                )
                try:
                    from aqt.utils import showInfo

                    showInfo(
                        "AnkiCollab settings and logins are stored per Anki "
                        "profile, inside the add-on's user_files folder."
                    )
                except Exception:
                    pass
                return False

        setter(addon, _open_settings)
    except Exception as exc:
        logger.warning("AnkiCollab: could not override the Config button: %s", exc)


def init() -> None:
    """Wire the module into Anki.

    Call this first, before anything that registers hooks or reads config, so
    the ``profile_did_open`` listener is registered ahead of every other
    listener and the caches are populated before they run.

    Safe to call more than once (add-on reload).
    """
    global _initialised
    with _lock:
        if _initialised:
            return
        _initialised = True

    try:
        from aqt import gui_hooks

        for hook_list, handler in (
            (gui_hooks.profile_did_open, _on_profile_did_open),
            (gui_hooks.profile_will_close, _on_profile_will_close),
        ):
            for stale in [
                callback
                for callback in _hook_callbacks(hook_list)
                if getattr(callback, _HOOK_MARKER, False)
            ]:
                try:
                    hook_list.remove(stale)
                except Exception:
                    pass
            hook_list.append(handler)
    except Exception as exc:
        logger.warning("AnkiCollab: could not register the profile hooks: %s", exc)

    _register_config_action()

    # An add-on reload does not re-fire profile_did_open for a profile that is
    # already open, so activate it now.
    try:
        name = _profile_name_from_mw()
        if name is not None:
            activate_profile(name)
    except Exception as exc:
        logger.warning("AnkiCollab: could not activate the open profile: %s", exc)
