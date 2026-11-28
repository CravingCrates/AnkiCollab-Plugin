"""Startup auto-sync orchestration, AnkiWeb safety checks, and pull concurrency guard."""

from __future__ import annotations

import sys
import threading
import types
from typing import Any, Callable, Optional

import requests
import sentry_sdk

import aqt
import aqt.utils
from anki.utils import point_version
from aqt import gui_hooks, mw
from aqt.operations import QueryOp
from aqt.qt import QApplication, QTimer, QWidget

from . import config_manager
from . import import_manager
from .api_client import api_client
from .auth_manager import auth_manager
from .utils import DeckManager, get_logger, is_collection_available

logger = get_logger("ankicollab.startup_sync")

_HOOK_MARKER = "_ankicollab_startup_sync_hook"
_ORIG_ATTR = "_ankicollab_startup_sync_orig"
_SKIPPED = object()

_lock = threading.RLock()
_session_generation: int = 0
_profile_closing: bool = False
_attempted_profiles: set[str] = set()
_pending_sync_hook: Optional[Callable[..., None]] = None
_ankiweb_sync_active: bool = False
_collection_temporarily_closed: bool = False
_pull_slot_owner: Optional[int] = None
_next_pull_slot_token: int = 0
_next_addon_check_id: int = 0
_active_startup_addon_checks: set[int] = set()
_awaiting_prompt_addon_checks: set[int] = set()
_open_addon_update_dialogs: dict[int, tuple[Any, dict[str, Any]]] = {}
_prompt_dialog_capture_stack: list[list[tuple[Any, dict[str, Any]]]] = []
_pending_post_addon_update_pull: Optional[tuple[int, str, bool]] = None
_installed_addon_update_hooks: list[tuple[Any, str, bool, Any, Any]] = []


def reset_state() -> None:
    """Reset all module state and detach any pending sync hook (for tests)."""
    global _session_generation, _profile_closing, _pending_sync_hook
    global _ankiweb_sync_active, _collection_temporarily_closed
    global _pull_slot_owner, _next_pull_slot_token
    global _next_addon_check_id, _pending_post_addon_update_pull
    _detach_pending_sync_hook()
    _uninstall_addon_update_hooks()
    with _lock:
        _session_generation = 0
        _profile_closing = False
        _attempted_profiles.clear()
        _pending_sync_hook = None
        _ankiweb_sync_active = False
        _collection_temporarily_closed = False
        _pull_slot_owner = None
        _next_pull_slot_token = 0
        _next_addon_check_id = 0
        _active_startup_addon_checks.clear()
        _awaiting_prompt_addon_checks.clear()
        _open_addon_update_dialogs.clear()
        _prompt_dialog_capture_stack.clear()
        _pending_post_addon_update_pull = None
        _installed_addon_update_hooks.clear()


def is_pull_in_progress() -> bool:
    """Return True if a pull operation currently holds the pull slot."""
    with _lock:
        return _pull_slot_owner is not None


def acquire_pull_slot() -> Optional[int]:
    """Atomically acquire the shared pull slot, returning an owner token or None."""
    global _pull_slot_owner, _next_pull_slot_token
    with _lock:
        if _pull_slot_owner is not None:
            return None
        _next_pull_slot_token += 1
        _pull_slot_owner = _next_pull_slot_token
        return _pull_slot_owner


def release_pull_slot(token: Optional[int] = None) -> None:
    """Release the shared pull slot when forced (token=None) or owned by token."""
    global _pull_slot_owner
    with _lock:
        if token is None or _pull_slot_owner == token:
            _pull_slot_owner = None


def _hook_callbacks(hook_list: Any) -> list[Any]:
    """Return callbacks stored in an Anki hook object or a plain test list."""
    hooks = getattr(hook_list, "_hooks", None)
    if isinstance(hooks, list):
        return list(hooks)
    if isinstance(hook_list, list):
        return list(hook_list)
    return []


def _on_ankiweb_sync_will_start(*_args: Any, **_kwargs: Any) -> None:
    global _ankiweb_sync_active
    with _lock:
        _ankiweb_sync_active = True


def _on_ankiweb_sync_did_finish(*_args: Any, **_kwargs: Any) -> None:
    global _ankiweb_sync_active
    with _lock:
        _ankiweb_sync_active = False


def _on_collection_will_temporarily_close(*_args: Any, **_kwargs: Any) -> None:
    global _collection_temporarily_closed
    with _lock:
        _collection_temporarily_closed = True


def _on_collection_did_temporarily_close(*_args: Any, **_kwargs: Any) -> None:
    global _collection_temporarily_closed
    with _lock:
        _collection_temporarily_closed = False


setattr(_on_ankiweb_sync_will_start, _HOOK_MARKER, True)
setattr(_on_ankiweb_sync_did_finish, _HOOK_MARKER, True)
setattr(_on_collection_will_temporarily_close, _HOOK_MARKER, True)
setattr(_on_collection_did_temporarily_close, _HOOK_MARKER, True)


def _get_aqt_addons_module() -> Any:
    mod = sys.modules.get("aqt.addons")
    if mod is not None:
        return mod
    try:
        import aqt.addons  # type: ignore

        return aqt.addons
    except Exception:
        return None


def _current_addon_id() -> Optional[str]:
    target_mw = mw if mw is not None else getattr(aqt, "mw", None)
    addon_mgr = getattr(target_mw, "addonManager", None) if target_mw else None
    addon_from_mod = getattr(addon_mgr, "addon_from_module", None)
    if callable(addon_from_mod):
        try:
            addon_id = addon_from_mod(__name__)
            if isinstance(addon_id, str) and addon_id:
                return addon_id
        except Exception:
            pass
    if __package__:
        return __package__.split(".")[0] or None
    return None


def _was_self_addon_updated(log: Any) -> bool:
    if not log:
        return False
    self_id = _current_addon_id()
    if not self_id:
        return False
    addons_mod = _get_aqt_addons_module()
    install_ok_cls = getattr(addons_mod, "InstallOk", None) if addons_mod else None
    try:
        entries = list(log)
    except TypeError:
        return False
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            continue
        addon_id, status = entry
        if str(addon_id) != self_id:
            continue
        if isinstance(install_ok_cls, type):
            if isinstance(status, install_ok_cls):
                return True
        elif type(status).__name__ == "InstallOk":
            return True
    return False


def _is_progress_busy() -> bool:
    target_mw = mw if mw is not None else getattr(aqt, "mw", None)
    progress = getattr(target_mw, "progress", None) if target_mw else None
    busy_fn = getattr(progress, "busy", None)
    if not callable(busy_fn):
        return False
    try:
        val = busy_fn()
    except Exception:
        return False
    return val is True or (
        isinstance(val, int) and not isinstance(val, bool) and val > 0
    )


def _has_visible_addon_update_dialog() -> bool:
    with _lock:
        tracked = list(_open_addon_update_dialogs.values())
    for dialog, _state in tracked:
        try:
            is_visible_fn = getattr(dialog, "isVisible", None)
            if callable(is_visible_fn):
                if is_visible_fn() is True:
                    return True
            else:
                return True
        except RuntimeError:
            # Qt C++ wrapper was deleted after dialog closed
            continue
        except Exception:
            return True

    addons_mod = _get_aqt_addons_module()
    dialog_cls = (
        getattr(addons_mod, "ChooseAddonsToUpdateDialog", None)
        if addons_mod is not None
        else None
    )
    if not isinstance(dialog_cls, type):
        return False
    try:
        widgets = QApplication.topLevelWidgets()
    except Exception:
        return False
    if not isinstance(widgets, (list, tuple)):
        return False
    for widget in widgets:
        try:
            if isinstance(widget, dialog_cls):
                is_visible_fn = getattr(widget, "isVisible", None)
                if not callable(is_visible_fn) or is_visible_fn() is True:
                    return True
        except RuntimeError:
            continue
        except Exception:
            continue
    return False


def _is_addon_update_blocking() -> bool:
    with _lock:
        if _active_startup_addon_checks:
            return True
    return _has_visible_addon_update_dialog()


def _uninstall_addon_update_hooks() -> None:
    with _lock:
        entries = list(_installed_addon_update_hooks)
        _installed_addon_update_hooks.clear()
    for owner, attr_name, had_in_dict, orig_raw, wrapper in reversed(entries):
        try:
            owner_dict = getattr(owner, "__dict__", None)
            if owner_dict is not None:
                current = owner_dict.get(attr_name)
                if current is not wrapper:
                    continue
            if had_in_dict:
                setattr(owner, attr_name, orig_raw)
            else:
                delattr(owner, attr_name)
        except Exception:
            pass


def _unwrap_hook_target(obj: Any) -> Any:
    while getattr(obj, _HOOK_MARKER, False) is True and hasattr(obj, _ORIG_ATTR):
        obj = getattr(obj, _ORIG_ATTR)
    return obj


def _install_addon_update_hooks() -> None:
    target_mw = mw if mw is not None else getattr(aqt, "mw", None)
    addons_mod = _get_aqt_addons_module()
    if target_mw is None or addons_mod is None:
        return

    raw_check = getattr(target_mw, "check_for_addon_updates", None)
    raw_prompt = getattr(addons_mod, "prompt_to_update", None)
    dialog_cls = getattr(addons_mod, "ChooseAddonsToUpdateDialog", None)
    if not isinstance(dialog_cls, type):
        return
    raw_ask = getattr(dialog_cls, "ask", None)

    orig_check = _unwrap_hook_target(raw_check)
    orig_prompt = _unwrap_hook_target(raw_prompt)
    orig_ask = _unwrap_hook_target(raw_ask)

    if not isinstance(orig_check, (types.FunctionType, types.MethodType)):
        return
    if not isinstance(orig_prompt, (types.FunctionType, types.MethodType)):
        return
    if not isinstance(orig_ask, (types.FunctionType, types.MethodType)):
        return

    if (
        getattr(raw_check, _HOOK_MARKER, False) is True
        and getattr(raw_prompt, _HOOK_MARKER, False) is True
        and getattr(raw_ask, _HOOK_MARKER, False) is True
    ):
        return

    def _wrapped_check_for_addon_updates(*args: Any, **kwargs: Any) -> Any:
        global _next_addon_check_id
        by_user = bool(args[0]) if args else bool(kwargs.get("by_user", True))
        if by_user:
            return orig_check(*args, **kwargs)

        with _lock:
            _next_addon_check_id += 1
            check_id = _next_addon_check_id
            _active_startup_addon_checks.add(check_id)
            _awaiting_prompt_addon_checks.add(check_id)

        orig_on_done = args[1] if len(args) >= 2 else kwargs.get("on_done")
        completed = False

        def _wrapped_on_done(log: Any = None) -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            with _lock:
                _active_startup_addon_checks.discard(check_id)
                _awaiting_prompt_addon_checks.discard(check_id)
            self_updated = _was_self_addon_updated(log)
            skip_reason = (
                "AnkiCollab add-on was updated; restart Anki to apply."
                if self_updated
                else None
            )
            try:
                if callable(orig_on_done):
                    orig_on_done(log)
            finally:
                _maybe_dispatch_pending_startup_pull(skip_reason=skip_reason)

        try:
            if len(args) >= 2:
                new_args = list(args)
                new_args[1] = _wrapped_on_done
                return orig_check(*new_args, **kwargs)
            new_kwargs = dict(kwargs)
            new_kwargs["on_done"] = _wrapped_on_done
            return orig_check(*args, **new_kwargs)
        except Exception:
            with _lock:
                _active_startup_addon_checks.discard(check_id)
                _awaiting_prompt_addon_checks.discard(check_id)
            _maybe_dispatch_pending_startup_pull()
            raise

    def _wrapped_dialog_ask(
        dialog: Any, on_done: Any, *args: Any, **kwargs: Any
    ) -> Any:
        state: dict[str, Any] = {
            "closed": False,
            "started_download": False,
            "check_ids": set(),
        }

        def _on_dialog_closed(*_args: Any) -> None:
            should_dispatch = False
            with _lock:
                if state["closed"]:
                    return
                state["closed"] = True
                _open_addon_update_dialogs.pop(id(dialog), None)
                if not state["started_download"] and state["check_ids"]:
                    _active_startup_addon_checks.difference_update(state["check_ids"])
                    state["check_ids"].clear()
                    should_dispatch = True
            if should_dispatch:
                _maybe_dispatch_pending_startup_pull()

        def _tracked_after_choosing(ids: Any) -> Any:
            if ids:
                with _lock:
                    state["started_download"] = True
            try:
                return on_done(ids) if callable(on_done) else None
            except Exception:
                with _lock:
                    state["started_download"] = False
                raise

        with _lock:
            _open_addon_update_dialogs[id(dialog)] = (dialog, state)
            if _prompt_dialog_capture_stack:
                _prompt_dialog_capture_stack[-1].append((dialog, state))

        for sig_name in ("finished", "destroyed"):
            sig = getattr(dialog, sig_name, None)
            connect_fn = getattr(sig, "connect", None)
            if callable(connect_fn):
                try:
                    connect_fn(_on_dialog_closed)
                except Exception:
                    pass

        try:
            wrapped_cb = _tracked_after_choosing if callable(on_done) else on_done
            return orig_ask(dialog, wrapped_cb, *args, **kwargs)
        except Exception:
            _on_dialog_closed()
            raise

    def _wrapped_prompt_to_update(*args: Any, **kwargs: Any) -> Any:
        requested_by_user = (
            bool(args[4])
            if len(args) >= 5
            else bool(kwargs.get("requested_by_user", True))
        )
        opened_dialogs: list[tuple[Any, dict[str, Any]]] = []
        with _lock:
            _prompt_dialog_capture_stack.append(opened_dialogs)
        try:
            return orig_prompt(*args, **kwargs)
        finally:
            should_dispatch = False
            with _lock:
                if (
                    _prompt_dialog_capture_stack
                    and _prompt_dialog_capture_stack[-1] is opened_dialogs
                ):
                    _prompt_dialog_capture_stack.pop()
                elif opened_dialogs in _prompt_dialog_capture_stack:
                    _prompt_dialog_capture_stack.remove(opened_dialogs)
                if not requested_by_user:
                    claimed = set(_awaiting_prompt_addon_checks)
                    _awaiting_prompt_addon_checks.clear()
                    if claimed:
                        open_entries = [
                            (dlg, st)
                            for dlg, st in opened_dialogs
                            if not st["closed"] or st["started_download"]
                        ]
                        if not open_entries:
                            _active_startup_addon_checks.difference_update(claimed)
                            should_dispatch = True
                        else:
                            for _dlg, st in open_entries:
                                if not st["closed"]:
                                    st["check_ids"].update(claimed)
            if should_dispatch:
                _maybe_dispatch_pending_startup_pull()

    setattr(_wrapped_check_for_addon_updates, _HOOK_MARKER, True)
    setattr(_wrapped_check_for_addon_updates, _ORIG_ATTR, orig_check)
    setattr(_wrapped_dialog_ask, _HOOK_MARKER, True)
    setattr(_wrapped_dialog_ask, _ORIG_ATTR, orig_ask)
    setattr(_wrapped_prompt_to_update, _HOOK_MARKER, True)
    setattr(_wrapped_prompt_to_update, _ORIG_ATTR, orig_prompt)

    targets = [
        (
            target_mw,
            "check_for_addon_updates",
            raw_check,
            _wrapped_check_for_addon_updates,
        ),
        (addons_mod, "prompt_to_update", raw_prompt, _wrapped_prompt_to_update),
        (dialog_cls, "ask", raw_ask, _wrapped_dialog_ask),
    ]
    with _lock:
        tracked_owners = {
            (id(owner), attr): idx
            for idx, (owner, attr, *_rest) in enumerate(_installed_addon_update_hooks)
        }
        for owner, attr_name, current_val, wrapper in targets:
            if getattr(current_val, _HOOK_MARKER, False) is True:
                continue
            owner_dict = getattr(owner, "__dict__", None)
            had_in_dict = owner_dict is not None and attr_name in owner_dict
            orig_raw = owner_dict[attr_name] if had_in_dict else current_val
            setattr(owner, attr_name, wrapper)
            record = (owner, attr_name, had_in_dict, orig_raw, wrapper)
            key = (id(owner), attr_name)
            if key in tracked_owners:
                _installed_addon_update_hooks[tracked_owners[key]] = record
            else:
                _installed_addon_update_hooks.append(record)


def register_hooks() -> None:
    """Register reload-safe persistent hooks tracking sync and collection state."""
    try:
        for hook_name, handler in (
            ("sync_will_start", _on_ankiweb_sync_will_start),
            ("sync_did_finish", _on_ankiweb_sync_did_finish),
            (
                "collection_will_temporarily_close",
                _on_collection_will_temporarily_close,
            ),
            (
                "collection_did_temporarily_close",
                _on_collection_did_temporarily_close,
            ),
        ):
            hook_list = getattr(gui_hooks, hook_name, None)
            if hook_list is None:
                continue
            for stale in [
                callback
                for callback in _hook_callbacks(hook_list)
                if getattr(callback, _HOOK_MARKER, False) is True
            ]:
                try:
                    hook_list.remove(stale)
                except Exception:
                    pass
            hook_list.append(handler)
        _install_addon_update_hooks()
    except Exception as exc:
        logger.warning("Failed to register startup sync hooks: %s", exc)


def _detach_pending_sync_hook() -> None:
    global _pending_sync_hook
    with _lock:
        hook = _pending_sync_hook
        _pending_sync_hook = None
    if hook is not None:
        try:
            gui_hooks.sync_did_finish.remove(hook)
        except Exception:
            pass


def on_profile_will_close() -> None:
    """Synchronously abort pending startup sync before Anki closes the profile."""
    global _profile_closing, _session_generation, _ankiweb_sync_active
    global _collection_temporarily_closed, _pull_slot_owner
    global _pending_post_addon_update_pull
    with _lock:
        _profile_closing = True
        _session_generation += 1
        _ankiweb_sync_active = False
        _collection_temporarily_closed = False
        _pull_slot_owner = None
        _pending_post_addon_update_pull = None
    _detach_pending_sync_hook()


def _is_collection_ready() -> bool:
    """Return True when the collection is open, not temporarily closed, and has a DB."""
    with _lock:
        if _collection_temporarily_closed:
            return False
    if not is_collection_available():
        return False
    col = getattr(mw, "col", None)
    return col is not None and getattr(col, "db", None) is not None


def _is_session_current(expected_gen: int, expected_profile: Optional[str]) -> bool:
    with _lock:
        if _profile_closing or _session_generation != expected_gen:
            return False
    if not expected_profile:
        return False
    return (
        config_manager.current_profile_name() == expected_profile
        and _is_collection_ready()
    )


def _is_pull_on_startup_enabled() -> bool:
    config = config_manager.get_config()
    if not isinstance(config, dict):
        return False
    settings = config.get("settings")
    return isinstance(settings, dict) and bool(settings.get("pull_on_startup", False))


def _has_subscribed_decks() -> bool:
    for _ in DeckManager():
        return True
    return False


def _schedule_on_next_tick(callback: Callable[[], None]) -> None:
    QTimer.singleShot(0, callback)


def _anki_sync_allowed_statuses() -> tuple[int, int]:
    """Return ``(NO_CHANGES, NORMAL_SYNC)`` from ``SyncStatus``, safe under MagicMocks."""
    no_changes = 0
    normal_sync = 1
    try:
        from anki.sync import SyncStatus  # type: ignore

        val_no = getattr(SyncStatus, "NO_CHANGES", None)
        if isinstance(val_no, int) and not isinstance(val_no, bool):
            no_changes = val_no
        val_normal = getattr(SyncStatus, "NORMAL_SYNC", None)
        if isinstance(val_normal, int) and not isinstance(val_normal, bool):
            normal_sync = val_normal
    except Exception:
        pass
    return (no_changes, normal_sync)


def _is_ankiweb_media_syncing() -> bool:
    """Return True when AnkiWeb media sync is active (main thread only)."""
    try:
        media_syncer = getattr(mw, "media_syncer", None)
        fn = getattr(media_syncer, "is_syncing", None)
        return (fn() is True) if callable(fn) else False
    except Exception:
        return False


def _updates_require_full_sync(webresult: Any, col: Any) -> bool:
    """Return True if applying ``webresult`` would modify an existing notetype's schema."""
    if not isinstance(webresult, (list, tuple)) or col is None:
        return False
    all_fn = getattr(getattr(col, "models", None), "all", None)
    existing_models = all_fn() if callable(all_fn) else None
    if not isinstance(existing_models, list):
        return False
    existing_by_uuid = {
        m.get("crowdanki_uuid"): m
        for m in existing_models
        if isinstance(m, dict) and m.get("crowdanki_uuid")
    }
    note_guid_to_model_uuid: dict[str, str] = {}
    stack = [sub.get("deck") for sub in webresult if isinstance(sub, dict)]
    while stack:
        deck = stack.pop()
        if not isinstance(deck, dict):
            continue
        children = deck.get("children")
        if isinstance(children, list):
            stack.extend(children)
        notes = deck.get("notes")
        if isinstance(notes, list):
            for note_dict in notes:
                if not isinstance(note_dict, dict):
                    continue
                guid = note_dict.get("guid") or note_dict.get("crowdanki_uuid")
                target_uuid = note_dict.get("note_model_uuid")
                if (
                    isinstance(guid, str)
                    and guid
                    and isinstance(target_uuid, str)
                    and target_uuid
                ):
                    note_guid_to_model_uuid[guid] = target_uuid
        models = deck.get("note_models")
        if not isinstance(models, list):
            continue
        for model_dict in models:
            if not isinstance(model_dict, dict):
                continue
            existing = existing_by_uuid.get(model_dict.get("crowdanki_uuid"))
            if existing is None:
                continue
            if (
                "type" in model_dict
                and "type" in existing
                and model_dict.get("type") != existing.get("type")
            ):
                return True
            old_names = [
                (f.get("name") or "").lower()
                for f in (existing.get("flds") or [])
                if isinstance(f, dict)
            ]
            remote_names = [
                (f.get("name") or "").lower()
                for f in (model_dict.get("flds") or [])
                if isinstance(f, dict)
            ]
            remote_set = set(remote_names)
            merged = remote_names + [n for n in old_names if n not in remote_set]
            if len(merged) != len(old_names) or merged[: len(old_names)] != old_names:
                return True
            if (
                "projektanki" not in (model_dict.get("name") or "").lower()
                and "tmpls" in model_dict
            ):
                old_tmpls = existing.get("tmpls") or []
                new_tmpls = model_dict.get("tmpls") or []
                if len(old_tmpls) != len(new_tmpls):
                    return True
                old_t = [t.get("name") for t in old_tmpls if isinstance(t, dict)]
                new_t = [t.get("name") for t in new_tmpls if isinstance(t, dict)]
                if old_t != new_t and set(old_t) == set(new_t):
                    return True
                for idx, (old_tmpl, new_tmpl) in enumerate(zip(old_tmpls, new_tmpls)):
                    if not isinstance(old_tmpl, dict) or not isinstance(new_tmpl, dict):
                        continue
                    if old_tmpl.get("ord", idx) != new_tmpl.get("ord", idx):
                        return True

    if note_guid_to_model_uuid:
        local_mid_to_uuid = {
            m.get("id"): m.get("crowdanki_uuid")
            for m in existing_models
            if isinstance(m, dict) and m.get("id") is not None
        }
        db = getattr(col, "db", None)
        all_db_fn = getattr(db, "all", None)
        if callable(all_db_fn):
            guids = list(note_guid_to_model_uuid.keys())
            for i in range(0, len(guids), 1000):
                batch = guids[i : i + 1000]
                placeholders = ", ".join("?" for _ in batch)
                try:
                    rows = all_db_fn(
                        f"SELECT guid, mid FROM notes WHERE guid IN ({placeholders})",
                        *batch,
                    )
                except Exception:
                    rows = None
                if not isinstance(rows, list):
                    break
                for row in rows:
                    if not isinstance(row, (tuple, list)) or len(row) != 2:
                        continue
                    note_guid, current_mid = row[0], row[1]
                    target_uuid = note_guid_to_model_uuid.get(note_guid)
                    if (
                        target_uuid
                        and local_mid_to_uuid.get(current_mid) != target_uuid
                    ):
                        return True
    return False


def _has_active_modal_dialog() -> bool:
    try:
        modal = QApplication.activeModalWidget()
    except Exception:
        return False
    return isinstance(modal, QWidget)


def can_apply_startup_updates_now() -> bool:
    """Return True if it is safe to show dialogs and import updates right now."""
    with _lock:
        if _ankiweb_sync_active:
            return False
    if not _is_collection_ready():
        return False
    if getattr(mw, "state", None) not in ("deckBrowser", "overview"):
        return False
    if _has_active_modal_dialog():
        return False
    if _is_addon_update_blocking():
        return False
    if _is_progress_busy():
        return False
    return True


def _show_guarded_tooltip(
    message: str,
    *,
    expected_gen: Optional[int] = None,
    expected_profile: Optional[str] = None,
    period: int = 3000,
) -> None:
    def _show() -> None:
        if expected_gen is not None and expected_profile is not None:
            if not _is_session_current(expected_gen, expected_profile):
                return
        if not _is_collection_ready():
            return
        try:
            aqt.utils.tooltip(message, parent=mw, period=period)
        except Exception:
            pass

    try:
        target_mw = mw if mw is not None else aqt.mw
        taskman = getattr(target_mw, "taskman", None) if target_mw else None
        if taskman is not None and callable(getattr(taskman, "run_on_main", None)):
            taskman.run_on_main(_show)
    except Exception:
        pass


def _validate_token_in_background(
    silent: bool = True,
    *,
    expected_gen: Optional[int] = None,
    expected_profile: Optional[str] = None,
) -> bool:
    def _can_notify() -> bool:
        if expected_gen is not None and expected_profile is not None:
            return _is_session_current(expected_gen, expected_profile)
        return _is_collection_ready()

    def _notify(message: str) -> None:
        _show_guarded_tooltip(
            message,
            expected_gen=expected_gen,
            expected_profile=expected_profile,
        )

    return import_manager.validate_user_token(
        silent=silent,
        can_notify=_can_notify,
        notify=_notify,
        api=api_client,
        auth=auth_manager,
    )


def _start_startup_pull(
    expected_gen: int,
    expected_profile: str,
    require_ankiweb_clean: bool,
) -> None:
    """Start the silent background startup update check on the main thread."""
    if (
        not _is_session_current(expected_gen, expected_profile)
        or not _is_collection_ready()
    ):
        return
    with _lock:
        if _ankiweb_sync_active:
            return
    if not _is_pull_on_startup_enabled():
        return

    if require_ankiweb_clean:
        pm = getattr(mw, "pm", None)
        sync_auth = pm.sync_auth() if pm is not None else None
        if not sync_auth:
            return
    else:
        sync_auth = None

    ankiweb_media_syncing = _is_ankiweb_media_syncing()

    slot_token = acquire_pull_slot()
    if slot_token is None:
        return

    def _session_notify(message: str) -> None:
        _show_guarded_tooltip(
            message,
            expected_gen=expected_gen,
            expected_profile=expected_profile,
        )

    def _bg_op(col: Any) -> Any:
        if not _is_session_current(expected_gen, expected_profile):
            return _SKIPPED

        if require_ankiweb_clean:
            sync_status_fn = getattr(col, "sync_status", None)
            if col is None or not callable(sync_status_fn):
                logger.warning(
                    "Skipping startup auto-sync: collection sync_status is unavailable"
                )
                return _SKIPPED
            try:
                status = sync_status_fn(sync_auth)
            except Exception as exc:
                logger.info(
                    "Skipping startup auto-sync: AnkiWeb sync_status check failed (%s)",
                    exc,
                )
                return _SKIPPED

            new_endpoint = getattr(status, "new_endpoint", None)
            if isinstance(new_endpoint, str) and new_endpoint:

                def _set_endpoint(url: str = new_endpoint) -> None:
                    if not _is_session_current(expected_gen, expected_profile):
                        return
                    try:
                        pm = getattr(mw, "pm", None)
                        setter = getattr(pm, "set_current_sync_url", None)
                        if callable(setter):
                            setter(url)
                    except Exception as exc:
                        logger.warning(
                            "Failed to update AnkiWeb sync endpoint: %s", exc
                        )

                try:
                    target_mw = mw if mw is not None else aqt.mw
                    taskman = getattr(target_mw, "taskman", None) if target_mw else None
                    if taskman is not None and callable(
                        getattr(taskman, "run_on_main", None)
                    ):
                        taskman.run_on_main(_set_endpoint)
                except Exception as exc:
                    logger.warning(
                        "Failed to schedule AnkiWeb sync endpoint update: %s", exc
                    )

            required = getattr(status, "required", None)
            if (
                isinstance(required, bool)
                or required not in _anki_sync_allowed_statuses()
            ):
                logger.info(
                    "Skipping startup auto-sync: AnkiWeb sync requires full sync or unknown state (%r)",
                    required,
                )
                return _SKIPPED

        if not _is_session_current(expected_gen, expected_profile):
            return _SKIPPED

        if not _validate_token_in_background(
            silent=True,
            expected_gen=expected_gen,
            expected_profile=expected_profile,
        ):
            return _SKIPPED

        if not _is_session_current(expected_gen, expected_profile):
            return _SKIPPED

        return import_manager.async_start_pull(
            None,
            silent=True,
            notify=_session_notify,
            defer_cache_bootstrap=ankiweb_media_syncing,
        )

    def _on_done(result: Any) -> None:
        try:
            if (
                not _is_session_current(expected_gen, expected_profile)
                or not _is_collection_ready()
            ):
                return
            if result is None or result is _SKIPPED:
                return

            webresult, input_hash, _silent = result
            if webresult is None:
                return

            with _lock:
                sync_active = _ankiweb_sync_active
            if sync_active:
                if webresult:
                    try:
                        aqt.utils.tooltip(
                            "AnkiCollab updates available. Use AnkiCollab → Update Decks when ready.",
                            parent=mw,
                            period=5000,
                        )
                    except Exception:
                        pass
                return

            if not webresult:
                import_manager.import_webresult((webresult, input_hash, True))
                return

            if not can_apply_startup_updates_now():
                try:
                    aqt.utils.tooltip(
                        "AnkiCollab updates available. Use AnkiCollab → Update Decks when ready.",
                        parent=mw,
                        period=5000,
                    )
                except Exception:
                    pass
                return

            pm = getattr(mw, "pm", None)
            has_ankiweb = bool(
                pm is not None
                and callable(getattr(pm, "sync_auth", None))
                and pm.sync_auth()
            )
            if has_ankiweb and _updates_require_full_sync(webresult, mw.col):
                try:
                    aqt.utils.tooltip(
                        "AnkiCollab update includes note type changes (requires full AnkiWeb sync). Use AnkiCollab → Update Decks when ready.",
                        parent=mw,
                        period=6000,
                    )
                except Exception:
                    pass
                return

            import_manager.import_webresult((webresult, input_hash, True))
        finally:
            release_pull_slot(slot_token)

    def _on_failure(exc: Exception) -> None:
        release_pull_slot(slot_token)
        if (
            not _is_session_current(expected_gen, expected_profile)
            or not _is_collection_ready()
        ):
            return
        if isinstance(
            exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
        ):
            logger.info("Startup auto-sync failed while offline: %s", exc)
            return

        logger.exception("Unexpected error during startup auto-sync: %s", exc)
        try:
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        try:
            aqt.utils.tooltip(
                "An unexpected error occurred while checking for AnkiCollab updates.",
                parent=mw,
            )
        except Exception:
            pass

    try:
        op = QueryOp(parent=mw, op=_bg_op, success=_on_done).failure(_on_failure)
        if point_version() >= 231000:
            op = op.without_collection()
        op.run_in_background()
    except Exception as exc:
        release_pull_slot(slot_token)
        logger.warning("Failed to launch startup auto-sync: %s", exc)


def _start_or_defer_startup_pull(
    expected_gen: int,
    expected_profile: str,
    require_ankiweb_clean: bool,
) -> None:
    """Start startup pull now, or defer until Anki's add-on update flow finishes."""
    global _pending_post_addon_update_pull
    if (
        not _is_session_current(expected_gen, expected_profile)
        or not _is_collection_ready()
    ):
        return
    if _is_addon_update_blocking():
        with _lock:
            _pending_post_addon_update_pull = (
                expected_gen,
                expected_profile,
                require_ankiweb_clean,
            )
        logger.info(
            "Deferring startup auto-sync while Anki add-on update check/dialog is active"
        )
        return
    _start_startup_pull(
        expected_gen, expected_profile, require_ankiweb_clean=require_ankiweb_clean
    )


def _maybe_dispatch_pending_startup_pull(*, skip_reason: Optional[str] = None) -> None:
    """Dispatch deferred startup pull once no Anki add-on update flow is active."""
    global _pending_post_addon_update_pull
    with _lock:
        if _is_addon_update_blocking():
            return
        pending = _pending_post_addon_update_pull
        _pending_post_addon_update_pull = None
    if pending is None:
        return
    if skip_reason:
        logger.info("Skipping startup auto-sync: %s", skip_reason)
        return
    gen, profile_name, require_ankiweb_clean = pending
    if not _is_session_current(gen, profile_name):
        return
    _schedule_on_next_tick(
        lambda: _start_or_defer_startup_pull(gen, profile_name, require_ankiweb_clean)
    )


def on_profile_opened() -> None:
    """Evaluate startup auto-sync preconditions and schedule check if eligible."""
    global _profile_closing, _collection_temporarily_closed
    global _session_generation, _pending_sync_hook

    _detach_pending_sync_hook()

    with _lock:
        _profile_closing = False
        _collection_temporarily_closed = False
        _session_generation += 1
        gen = _session_generation

    profile_name = config_manager.current_profile_name()
    if profile_name is None or not _is_collection_ready():
        return

    with _lock:
        if profile_name in _attempted_profiles:
            return

    if bool(getattr(mw, "safeMode", False)) or bool(
        getattr(mw, "restoring_backup", False)
    ):
        return

    if not _is_pull_on_startup_enabled():
        return

    if not auth_manager.has_stored_token():
        return

    if not _has_subscribed_decks():
        return

    pm = getattr(mw, "pm", None)
    has_ankiweb_auth = bool(pm is not None and pm.sync_auth())
    ankiweb_auto_sync = bool(pm is not None and pm.auto_syncing_enabled())

    if has_ankiweb_auth and not ankiweb_auto_sync:
        logger.info("Skipping startup auto-sync: AnkiWeb auto-sync is disabled by user")
        return

    if has_ankiweb_auth and ankiweb_auto_sync:
        can_auto_sync_fn = getattr(mw, "can_auto_sync", None)
        if callable(can_auto_sync_fn) and not bool(can_auto_sync_fn()):
            logger.info("Skipping startup auto-sync: AnkiWeb cannot auto-sync now")
            return

        def _on_startup_sync_did_finish(*_args: Any, **_kwargs: Any) -> None:
            global _pending_sync_hook
            with _lock:
                if _pending_sync_hook is _on_startup_sync_did_finish:
                    _pending_sync_hook = None
            try:
                gui_hooks.sync_did_finish.remove(_on_startup_sync_did_finish)
            except Exception:
                pass

            if not _is_session_current(gen, profile_name):
                return
            if not _is_pull_on_startup_enabled():
                return
            with _lock:
                if profile_name in _attempted_profiles:
                    return
                _attempted_profiles.add(profile_name)
            _schedule_on_next_tick(
                lambda: _start_or_defer_startup_pull(gen, profile_name, True)
            )

        with _lock:
            _pending_sync_hook = _on_startup_sync_did_finish
        try:
            gui_hooks.sync_did_finish.append(_on_startup_sync_did_finish)
        except Exception:
            with _lock:
                if _pending_sync_hook is _on_startup_sync_did_finish:
                    _pending_sync_hook = None
            raise
        return

    with _lock:
        _attempted_profiles.add(profile_name)
    _schedule_on_next_tick(
        lambda: _start_or_defer_startup_pull(gen, profile_name, False)
    )
