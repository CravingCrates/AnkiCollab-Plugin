from collections import defaultdict
import json
import shutil
import tempfile
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from datetime import datetime, timedelta, timezone

import aqt
import aqt.utils
from aqt.operations import QueryOp

from aqt.qt import *
from aqt.qt import QDialog, QApplication, QMessageBox
from aqt import mw

from .api_client import api_client

from .dialogs import (
    ChangelogDialog,
    DeletedNotesDialog,
    OptionalTagsDialog,
    AskShareStatsDialog,
    RateAddonDialog,
)

from .crowd_anki.representation import deck_initializer
from .crowd_anki.importer.import_dialog import ImportConfig

from .utils import (
    create_backup,
    DeckManager,
    get_logger,
    get_personal_tags,
    is_collection_available,
    OperationAbortedError,
    check_collection_or_abort,
    BackupFailedError,
    get_noteids_from_uuids,
)

from .stats import ReviewHistory, on_stats_upload_done, update_stats_timestamp

import base64
import gzip

logger = get_logger("ankicollab.import_manager")

CACHE_BOOTSTRAP_MODE = "cache-bootstrap"
DEFAULT_REQUEST_TIMEOUT = (30, 120)  # connect, read
MAX_DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_CHUNK_SIZE = 64 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB — sanity cap on server-supplied size


class CacheBootstrapError(RuntimeError):
    """Raised when a cache bootstrap payload cannot be processed."""


class CacheArchiveRefreshError(CacheBootstrapError):
    """Raised when a fresh cache archive URL is required."""


def _notify_cache_bootstrap_failure() -> None:
    aqt.utils.showCritical(
        "AnkiCollab could not download deck. Please try again later. If the problem persists, please contact support."
    )


def _fetch_manifest(manifest_url: str) -> Dict[str, Any]:
    try:
        response = requests.get(manifest_url, timeout=DEFAULT_REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to download cache manifest %s: %s", manifest_url, exc)
        raise CacheBootstrapError(
            f"Unable to download cache manifest: {manifest_url}"
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        logger.error("Invalid JSON received for manifest %s: %s", manifest_url, exc)
        raise CacheBootstrapError("Cache manifest payload is not valid JSON") from exc


def _safe_destination(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    destination = (root / relative_path).resolve()
    if not destination.is_relative_to(root):
        raise CacheBootstrapError(
            f"Media path escapes target directory: {relative_path}"
        )
    return destination


def _coerce_subscription_payload(
    payload: Any, deck_last_modified: Optional[str]
) -> Dict[str, Any]:
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)

    if not isinstance(payload, dict):
        raise CacheBootstrapError("Cache archive deck data is not a valid object")

    if "deck" not in payload:
        raise CacheBootstrapError("Cache archive deck data missing 'deck' field")

    subscription = dict(payload)

    if deck_last_modified:
        subscription["deck_last_modified"] = deck_last_modified

    return subscription


def _extract_media_entries(archive: zipfile.ZipFile, media_info: Dict[str, Any]) -> int:
    if not media_info:
        return 0

    prefix = media_info.get("path_prefix", "") or ""
    prefix = prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"

    if not mw.col:
        raise CacheBootstrapError("Anki collection is not available for media import")

    media_dir = Path(mw.col.media.dir())
    extracted = 0

    for entry in archive.infolist():
        name = entry.filename
        if entry.is_dir():
            continue
        if prefix and not name.startswith(prefix):
            continue
        if not prefix and name.startswith("__MACOSX/"):
            continue

        relative_name = name[len(prefix) :] if prefix else name
        if not relative_name:
            continue

        if (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise CacheBootstrapError(f"Symlink in archive rejected: {entry.filename}")

        destination = _safe_destination(media_dir, relative_name)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            # Skip existing files to avoid unnecessary overwrites
            continue

        with archive.open(entry) as source, destination.open("wb") as target:
            shutil.copyfileobj(source, target)

        extracted += 1

    return extracted


def _subscription_from_manifest(
    deck_hash: str,
    manifest: Dict[str, Any],
    archive_url: Optional[str] = None,
) -> Dict[str, Any]:
    if not archive_url:
        raise CacheBootstrapError("Cache manifest missing archive URL")

    tmp_path: Optional[Path] = None

    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_path = Path(tmp_file.name)
            try:
                session = api_client.session_with_retries()
                downloaded = 0
                ignored_range = 0
                total_size = 0
                last_ui_update = 0.0
                started_at = time.monotonic()
                last_rate_sample_at = started_at
                last_rate_sample_bytes = 0
                smoothed_bps = 0.0

                def _format_eta(seconds: int) -> str:
                    eta_seconds = max(0, seconds)
                    if eta_seconds < 10:
                        return ""
                    if eta_seconds < 60:
                        return "less than a minute left"
                    if eta_seconds < 3600:
                        minutes = int((eta_seconds + 59) // 60)
                        return f"about {minutes} min left"
                    hours = eta_seconds // 3600
                    minutes = int(((eta_seconds % 3600) + 59) // 60)
                    if minutes == 60:
                        hours += 1
                        minutes = 0
                    if minutes == 0:
                        return f"about {hours} h left"
                    return f"about {hours} h {minutes} min left"

                def update_progress(
                    dl_bytes: int, tot_bytes: int, speed_bps: float, elapsed: float
                ) -> None:
                    if not getattr(aqt.mw, "progress", None):
                        return
                    if tot_bytes > 0:
                        bar_value = min(1000, int((dl_bytes / tot_bytes) * 1000))
                        label = "Downloading deck..."
                        if speed_bps > 0 and elapsed >= 8 and dl_bytes < tot_bytes:
                            remaining_seconds = int((tot_bytes - dl_bytes) / speed_bps)
                            eta = _format_eta(remaining_seconds)
                            if eta:
                                label = f"Downloading deck... {eta}"
                        aqt.mw.progress.update(label=label, value=bar_value, max=1000)
                    else:
                        aqt.mw.progress.update(label="Downloading deck...")

                for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
                    headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
                    response = None
                    try:
                        response = session.get(
                            archive_url,
                            headers=headers,
                            stream=True,
                            timeout=DEFAULT_REQUEST_TIMEOUT,
                            verify=True,
                        )

                        if response.status_code in (401, 403):
                            raise CacheArchiveRefreshError(
                                "Cache archive URL expired or was rejected"
                            )

                        if response.status_code == 416:
                            downloaded = 0
                            tmp_file.seek(0)
                            tmp_file.truncate()
                            response.close()
                            continue

                        if downloaded and response.status_code == 200:
                            ignored_range += 1
                            logger.warning(
                                "Cache archive server ignored Range request for %s "
                                "(%d/3)",
                                deck_hash,
                                ignored_range,
                            )
                            if ignored_range >= 3:
                                raise CacheBootstrapError(
                                    "Cache archive endpoint does not support resume"
                                )
                            downloaded = 0
                            tmp_file.seek(0)
                            tmp_file.truncate()
                        elif response.status_code not in (200, 206):
                            response.raise_for_status()

                        if downloaded == 0:
                            total_size_str = response.headers.get("Content-Length")
                            total_size = (
                                int(total_size_str)
                                if total_size_str and total_size_str.isdigit()
                                else 0
                            )
                            content_range = response.headers.get("Content-Range", "")
                            if "/" in content_range:
                                try:
                                    total_size = int(content_range.rsplit("/", 1)[1])
                                except ValueError:
                                    pass

                            # Guard against a bogus/hostile Content-Length.
                            if total_size > MAX_ARCHIVE_BYTES:
                                raise CacheBootstrapError(
                                    f"Cache archive too large "
                                    f"({total_size} bytes, max {MAX_ARCHIVE_BYTES})"
                                )

                        if downloaded:
                            tmp_file.seek(downloaded)

                        cancelled = False
                        for chunk in response.iter_content(
                            chunk_size=DOWNLOAD_CHUNK_SIZE
                        ):
                            if (
                                getattr(aqt.mw, "progress", None)
                                and aqt.mw.progress.want_cancel()
                            ):
                                cancelled = True
                                break

                            if chunk:
                                tmp_file.write(chunk)
                                downloaded += len(chunk)

                                # Guard against streams without Content-Length
                                # (chunked transfer, HTTP/2 without length).
                                if downloaded > MAX_ARCHIVE_BYTES:
                                    raise CacheBootstrapError(
                                        f"Cache archive exceeded size limit "
                                        f"({MAX_ARCHIVE_BYTES} bytes)"
                                    )

                                now = time.monotonic()
                                rate_window = now - last_rate_sample_at
                                if rate_window >= 0.5:
                                    window_bytes = downloaded - last_rate_sample_bytes
                                    instant_bps = (
                                        window_bytes / rate_window
                                        if rate_window > 0
                                        else 0.0
                                    )
                                    if smoothed_bps <= 0:
                                        smoothed_bps = instant_bps
                                    else:
                                        smoothed_bps = (0.12 * instant_bps) + (
                                            0.88 * smoothed_bps
                                        )
                                    last_rate_sample_at = now
                                    last_rate_sample_bytes = downloaded

                                should_update = now - last_ui_update >= 0.4
                                if total_size > 0 and downloaded >= total_size:
                                    should_update = True

                                if should_update:
                                    aqt.mw.taskman.run_on_main(
                                        lambda d=downloaded, t=total_size, s=smoothed_bps, e=(
                                            now - started_at
                                        ): update_progress(
                                            d, t, s, e
                                        )
                                    )
                                    last_ui_update = now

                        if cancelled:
                            raise OperationAbortedError(
                                "Cache bootstrap download cancelled by user"
                            )

                        if total_size and downloaded < total_size:
                            raise requests.exceptions.ChunkedEncodingError(
                                "Cache archive transfer ended before Content-Length"
                            )

                        if downloaded > 0:
                            aqt.mw.taskman.run_on_main(
                                lambda d=downloaded, t=total_size, s=smoothed_bps, e=(
                                    time.monotonic() - started_at
                                ): update_progress(d, t, s, e)
                            )
                        break
                    except CacheArchiveRefreshError:
                        raise
                    except OperationAbortedError:
                        raise
                    except requests.RequestException as exc:
                        logger.warning(
                            "Cache archive transfer interrupted for %s at %d bytes "
                            "(attempt %d/%d): %s",
                            deck_hash,
                            downloaded,
                            attempt + 1,
                            MAX_DOWNLOAD_ATTEMPTS,
                            exc,
                        )
                        if attempt == MAX_DOWNLOAD_ATTEMPTS - 1:
                            raise CacheBootstrapError(
                                "Unable to download cache archive"
                            ) from exc
                    finally:
                        if response is not None:
                            response.close()
            finally:
                tmp_file.flush()

        with zipfile.ZipFile(tmp_path) as archive:
            deck_info = manifest.get("deck_data", {})
            deck_path = deck_info.get("path")
            if not deck_path:
                raise CacheBootstrapError("Cache manifest missing deck data path")

            try:
                deck_bytes = archive.read(deck_path)
            except KeyError as exc:
                logger.error(
                    "Deck data path %s not found in archive for %s",
                    deck_path,
                    deck_hash,
                )
                raise CacheBootstrapError(
                    "Deck data not present in cache archive"
                ) from exc

            compression = (deck_info.get("compression") or "none").lower()
            if compression in {"gzip", "gz"}:
                try:
                    deck_bytes = gzip.decompress(deck_bytes)
                except (OSError, EOFError) as exc:
                    logger.error(
                        "Failed to decompress deck data for %s (path %s): %s",
                        deck_hash,
                        deck_path,
                        exc,
                    )
                    raise CacheBootstrapError("Unable to decompress deck data") from exc
            elif compression not in {"none", ""}:
                raise CacheBootstrapError(
                    f"Unsupported deck compression: {compression}"
                )

            try:
                deck_payload = json.loads(deck_bytes.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                logger.error("Invalid deck JSON for %s: %s", deck_hash, exc)
                raise CacheBootstrapError("Deck data is not valid JSON") from exc

            deck_last_modified = manifest.get("source_last_update")
            subscription = _coerce_subscription_payload(
                deck_payload, deck_last_modified
            )

            media_info = manifest.get("media", {})
            extracted_media = _extract_media_entries(archive, media_info)
            logger.info(
                "Cache bootstrap for %s extracted %d media files",
                deck_hash,
                extracted_media,
            )

            return subscription
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _resolve_cache_bootstrap_entries(entries: List[Any]) -> List[Any]:
    if not isinstance(entries, list):
        return entries

    resolved: List[Any] = []
    for idx, entry in enumerate(entries):
        try:
            if isinstance(entry, dict) and entry.get("mode") == CACHE_BOOTSTRAP_MODE:
                manifest_meta = entry.get("manifest") or {}
                manifest_url = manifest_meta.get("manifest_presigned_url")
                deck_hash = entry.get("deck_hash")

                if not manifest_url or not deck_hash:
                    logger.error(
                        "Cache bootstrap entry missing manifest URL or deck hash"
                    )
                    raise CacheBootstrapError("Malformed cache bootstrap entry")

                manifest = _fetch_manifest(manifest_url)

                archive_url = manifest_meta.get("archive_presigned_url")
                subscription = _subscription_from_manifest(
                    deck_hash, manifest, archive_url
                )
                resolved.append(subscription)
            else:
                resolved.append(entry)
        except CacheBootstrapError as cbe:
            logger.exception(
                "CacheBootstrapError while resolving cache bootstrap entry: %s", cbe
            )
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected error while resolving cache bootstrap entry: %s", exc
            )
            raise

    filtered = [entry for entry in resolved if entry is not None]
    return filtered


def update_optional_tag_config(given_deck_hash, optional_tags):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["optional_tags"] = optional_tags


def get_optional_tags(given_deck_hash) -> dict:
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details is None:
        return {}

    return details.get("optional_tags", {})


def check_optional_tag_changes(deck_hash, optional_tags):
    sorted_old = sorted(get_optional_tags(deck_hash).keys())
    sorted_new = sorted(optional_tags)
    return sorted_old != sorted_new


def update_timestamp(given_deck_hash):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["timestamp"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )


def update_deck_stats_enabled(given_deck_hash, stats_enabled):
    with DeckManager() as decks:
        details = decks.get_by_hash(given_deck_hash)

        if details:
            details["stats_enabled"] = stats_enabled
            if not stats_enabled:
                details["last_stats_timestamp"] = (
                    0  # Reset last stats timestamp if stats are disabled
                )


def delete_notes(nids):
    if not nids:
        return
    aqt.mw.col.remove_notes(nids)  # type: ignore
    aqt.mw.col.reset()  # type: ignore # deprecated
    mw.reset()
    aqt.mw.taskman.run_on_main(
        lambda: aqt.utils.tooltip(
            "Deleted %d notes." % len(nids), parent=QApplication.focusWidget()
        )
    )


def open_browser_with_nids(nids):
    if not nids:
        return
    browser = aqt.dialogs.open("Browser", aqt.mw)
    browser.form.searchEdit.lineEdit().setText(
        "nid:" + " or nid:".join(str(nid) for nid in nids)
    )
    browser.onSearchActivated()


def update_stats() -> None:
    """Update stats for decks where stats sharing is already enabled."""
    decks = DeckManager()

    for deck_hash, details in decks:
        if details.get("stats_enabled", False):
            # Only upload stats if the user has already agreed to share them
            share_stats = details.get("share_stats", False)
            if share_stats:
                last_stats_timestamp = details.get("last_stats_timestamp", 0)
                rh = ReviewHistory(deck_hash)
                op = QueryOp(
                    parent=mw,
                    op=lambda _: rh.upload_review_history(last_stats_timestamp),
                    success=on_stats_upload_done,
                )
                op.with_progress("Uploading Review History...").run_in_background()
                update_stats_timestamp(deck_hash)


def wants_to_share_stats(deck_hash) -> tuple[bool, int]:
    """Get stats sharing preference without showing dialog."""
    with DeckManager() as decks:
        details = decks.get_by_hash(deck_hash)
        if details is None:
            return False, 0

        last_stats_timestamp = details.get("last_stats_timestamp", 0)
        stats_enabled = details.get("share_stats", False)
        return stats_enabled, last_stats_timestamp


def _install_deck_op(deck, config, map_cache=None, note_type_data=None):
    """Background operation to install deck updates.

    This function runs in a background thread. It includes collection availability
    checks to abort gracefully if the collection is closed during operation.
    """
    # Use check_collection_or_abort instead of assert for graceful abort
    col = check_collection_or_abort("deck_installation_start")

    logger.info("Saving metadata.")
    deck.save_metadata(col, config.home_deck)

    # Re-check after metadata save
    check_collection_or_abort("after_metadata_save")

    logger.info("Saving decks and notes.")
    # Create media result structure
    med_res = {
        "success": True,
        "message": f"Unknown Media download error",
        "downloaded": 0,
        "skipped": 0,
    }

    total_notes = deck.calculate_total_work()
    logger.info(f"Total notes: {total_notes}")
    progress_tracker = deck.create_unified_progress_tracker(total_notes)
    logger.info("Workload calculated, starting bulk save.")

    # The save_decks_and_notes_bulk method has its own collection checks
    return deck.save_decks_and_notes_bulk(
        collection=col,
        progress_tracker=progress_tracker,  # Use unified tracker
        import_config=config,
    )


def _on_deck_installed(
    install_result, deck, subscription, input_hash=None, update_timestamp_after=False
):
    """Success callback after deck installation."""
    # Runs on Main Thread

    # Re-check collection availability after background operation
    if not is_collection_available():
        logger.warning("Collection became unavailable after deck installation")
        aqt.utils.showWarning(
            "Import may be incomplete: collection became unavailable during the operation.",
            parent=mw,
        )
        return

    deck.on_success_wrapper(install_result)

    deleted_notes = subscription.get("deleted_notes", [])
    deck_name = deck.anki_dict["name"]
    deck_hash = subscription["deck_hash"]

    # unfortunately, the db scalar shits itself when called from the success callback after the deck has been installed
    if deleted_notes:
        logger.info(f"Processing {len(deleted_notes)} deleted notes...")
        # Handle deleted Notes
        deleted_nids = get_noteids_from_uuids(logger, subscription["deleted_notes"])
        logger.info(f"Found {len(deleted_nids)} note IDs for deleted notes.")
        if deleted_nids:
            del_notes_dialog = DeletedNotesDialog(deleted_nids, deck_hash, parent=mw)
            del_notes_choice = del_notes_dialog.exec()

            if del_notes_choice == QDialog.DialogCode.Accepted:
                delete_notes(deleted_nids)
            elif del_notes_choice == QDialog.DialogCode.Rejected:
                open_browser_with_nids(deleted_nids)

    # Handle new deck registration if input_hash is provided
    if input_hash:
        with DeckManager() as decks:
            details = decks.get_by_hash(input_hash)
            if (
                details and details["deckId"] == 0 and mw.col
            ):  # should only be the case once when they add a new subscription and never ambiguous
                details["deckId"] = aqt.mw.col.decks.id(deck_name)  # type: ignore
                # large decks use cached data that may be a day old, so we need to update the timestamp to force a refresh
                details["timestamp"] = (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).strftime("%Y-%m-%d %H:%M:%S")
                details["stats_enabled"] = subscription["stats_enabled"]
    else:
        # Only ask for a rating if they are updating a deck and not adding a new deck to avoid spam popups
        ask_for_rating()

    # Update timestamp if requested (for changelog updates)
    if update_timestamp_after:
        update_timestamp(subscription["deck_hash"])

    # if the deck was cached, we have the actual last modified time as rfc3339 from the server
    if "deck_last_modified" in subscription:
        try:
            last_modified_dt = datetime.fromisoformat(
                subscription["deck_last_modified"].replace("Z", "+00:00")
            )
            formatted_last_modified = last_modified_dt.strftime("%Y-%m-%d %H:%M:%S")
            with DeckManager() as decks:
                details = decks.get_by_hash(deck_hash)
                if details:
                    details["timestamp"] = formatted_last_modified
        except Exception as e:
            logger.error(f"Error parsing last modified time: {e}")
            # Don't let this error break the import process
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:
                pass

    # Now handle stats sharing dialog AFTER import is complete
    deck_hash = subscription["deck_hash"]
    if subscription["stats_enabled"]:
        _handle_stats_sharing_after_import(deck_hash, deck_name)

    mw.reset()  # Reset the main window to reflect changes

    return deck_name


def _handle_stats_sharing_after_import(deck_hash, deck_name=None):
    """Handle stats sharing dialog after import is complete."""
    try:
        with DeckManager() as decks:
            details = decks.get_by_hash(deck_hash)
            if details is None:
                return

            stats_enabled = details.get("share_stats")
            if stats_enabled is None:
                # Only show dialog if not already decided
                if deck_name:
                    dialog = AskShareStatsDialog(deck_name, parent=mw)
                    choice = dialog.exec()
                    stats_enabled = choice == QDialog.DialogCode.Accepted

                    if dialog.isChecked():
                        details["share_stats"] = stats_enabled
    except Exception as e:
        logger.error(f"Error handling stats sharing dialog: {e}")
        # Don't let this error break the import process
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(e)
        except Exception:
            pass


def install_update(subscription, input_hash=None, update_timestamp_after=False):
    logger.info(f"Installing update for deck: {subscription['deck_hash']}")
    deck_hash = subscription["deck_hash"]
    parent_widget = QApplication.focusWidget() or mw

    if check_optional_tag_changes(deck_hash, subscription["optional_tags"]):
        dialog = OptionalTagsDialog(
            get_optional_tags(deck_hash), subscription["optional_tags"], parent=mw
        )
        dialog.exec()
        update_optional_tag_config(deck_hash, dialog.get_selected_tags())
    subscribed_tags = get_optional_tags(deck_hash)
    logger.debug("Optional Tags done.")
    deck = deck_initializer.from_json(subscription["deck"])
    logger.debug("Deck initialized.")
    config = prep_config(
        subscription["protected_fields"],
        [tag for tag, value in subscribed_tags.items() if value],
        True if subscription["optional_tags"] else False,
        deck_hash,
    )
    logger.debug("Config prepared.")

    map_cache = defaultdict(dict)
    note_type_data = {}
    # deck.handle_notetype_changes(mw.col, map_cache, note_type_data)
    logger.debug("Handled note type changes.")

    def _on_install_failure(e: Exception):
        """Handle installation failure, with special handling for graceful aborts."""
        if isinstance(e, OperationAbortedError):
            logger.warning(f"Import aborted gracefully at phase '{e.phase}': {e}")
            # Safely get parent widget - mw might be None if profile was closed
            parent_widget = None
            try:
                parent_widget = QApplication.focusWidget() or mw
            except Exception:
                pass

            try:
                aqt.utils.showInfo(
                    "Import was cancelled because the collection was closed.\n\n"
                    "Please reopen your profile and try again.",
                    parent=parent_widget,
                    title="Import Cancelled",
                )
            except Exception as dialog_error:
                # If we can't show a dialog (Anki shutting down), just log it
                logger.warning(f"Could not show abort dialog: {dialog_error}")
        else:
            logger.error(f"Import failed with error: {e}")
            # Let the default error handler show the error
            raise e

    # Start QueryOp for collection operations
    op = QueryOp(
        parent=parent_widget,
        op=lambda col: _install_deck_op(deck, config, map_cache, note_type_data),
        success=lambda res: _on_deck_installed(
            res, deck, subscription, input_hash, update_timestamp_after
        ),
    ).failure(_on_install_failure)
    op.with_progress("Installing/Updating deck...")
    op.run_in_background()

    # Return the deck name (note: this will be returned before the operation completes)
    return deck.anki_dict["name"]


def abort_update(deck_hash):
    update_timestamp(deck_hash)


def postpone_update():
    pass


def prep_config(protected_fields, optional_tags, has_optional_tags, deck_hash):
    home_deck = get_home_deck(deck_hash)
    new_notes_home_deck = get_new_notes_home_deck(deck_hash)
    personal_tags = get_personal_tags(deck_hash)

    config = ImportConfig(
        optional_tags=optional_tags,
        has_optional_tags=has_optional_tags,
        use_notes=True,
        use_media=False,
        ignore_deck_movement=get_deck_movement_status(),
        suspend_new_cards=get_card_suspension_status(),
        keep_empty_subdecks=get_keep_empty_subdeck_status(),
        home_deck=home_deck or "",  # Provide empty string as default
        new_notes_home_deck=new_notes_home_deck
        or "",  # Provide empty string as default
        deck_hash=deck_hash,
        personal_tags=personal_tags,
    )
    for protected_field in protected_fields:
        model_name = protected_field["name"]
        for field in protected_field["fields"]:
            field_name = field["name"]
            config.add_field(model_name, field_name)

    return config


def show_changelog_popup(subscription):
    changelog = subscription["changelog"]
    deck_hash = subscription["deck_hash"]

    update_deck_stats_enabled(deck_hash, subscription["stats_enabled"])

    if changelog:
        dialog = ChangelogDialog(changelog, deck_hash, parent=mw)
        choice = dialog.exec()

        if choice == QDialog.DialogCode.Accepted:
            install_update(subscription, update_timestamp_after=True)
        elif choice == QDialog.DialogCode.Rejected:
            postpone_update()
        else:
            abort_update(deck_hash)
    else:  # Skip changelog window if there is no message for the user
        install_update(subscription, update_timestamp_after=True)


def ask_for_rating():
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and "settings" in strings_data:
        if "pull_counter" in strings_data["settings"]:
            pull_counter = strings_data["settings"]["pull_counter"]
            strings_data["settings"]["pull_counter"] = pull_counter + 1
            if pull_counter % 30 == 0:  # every 30 pulls
                last_ratepls = strings_data["settings"]["last_ratepls"]
                last_ratepls_dt = datetime.strptime(
                    last_ratepls, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_ratepls_dt).days > 30:
                    if not strings_data["settings"][
                        "rated_addon"
                    ]:  # only ask if they haven't rated the addon yet
                        strings_data["settings"]["last_ratepls"] = datetime.now(
                            timezone.utc
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        dialog = RateAddonDialog(parent=mw)
                        dialog.exec()
            mw.addonManager.writeConfig(__name__, strings_data)


def import_webresult(data):
    webresult, input_hash, silent = data  # gotta unpack the tuple

    # None indicates an error or cancellation path where a dedicated message
    # was already shown (or intentionally suppressed).
    if webresult is None:
        return

    # Ensure collection is available before proceeding
    if not is_collection_available():
        aqt.utils.showWarning(
            "Cannot import: Anki collection is not available. "
            "Please ensure a profile is loaded and try again.",
            parent=mw,
        )
        return

    # if webresult is empty, tell user that there are no updates
    if not webresult:
        if silent:
            aqt.utils.tooltip("All synced.", parent=mw, period=1500)
        else:
            aqt.utils.tooltip("Your deck is up to date.", parent=mw, period=2000)

        update_stats()
        return

    # Create backup before doing anything - this is CRITICAL
    # If backup fails, abort the import to protect user data
    try:
        create_backup(critical=True)
    except BackupFailedError as e:
        logger.error(f"Import aborted due to backup failure: {e}")
        aqt.utils.showWarning(
            "Import aborted: Could not create a backup of your collection.\n\n"
            "This may indicate a problem with your collection. Please check that Anki "
            "is working correctly and try again.\n\n"
            "No changes have been made to your collection.",
            parent=mw,
            title="Backup Failed",
        )
        return

    for subscription in webresult:
        if input_hash:  # New deck
            install_update(subscription, input_hash)
        else:  # Update deck
            show_changelog_popup(subscription)

    update_stats()


def get_card_suspension_status():
    strings_data = mw.addonManager.getConfig(__name__)
    val = False
    if strings_data is not None and strings_data["settings"] is not None:
        val = bool(strings_data["settings"]["suspend_new_cards"])
    return val


def get_deck_movement_status():
    strings_data = mw.addonManager.getConfig(__name__)
    val = True
    if strings_data is not None and strings_data["settings"] is not None:
        val = bool(strings_data["settings"]["auto_move_cards"])
    return val


def get_keep_empty_subdeck_status():
    strings_data = mw.addonManager.getConfig(__name__)
    val = False
    if strings_data is not None and strings_data.get("settings") is not None:
        val = bool(strings_data["settings"].get("keep_empty_subdecks", False))
    return val


def get_home_deck(given_deck_hash):
    decks = DeckManager()
    details = decks.get_by_hash(given_deck_hash)

    if details and details["deckId"] != 0 and mw.col:
        return mw.col.decks.name_if_exists(details["deckId"])


def get_new_notes_home_deck(given_deck_hash):
    """Get the home deck for new notes (notes not in user's collection)"""
    try:
        decks = DeckManager()
        details = decks.get_by_hash(given_deck_hash)

        if details and mw.col:
            # Check if new_notes_home_deck is set
            new_notes_deck_id = details.get("new_notes_home_deck", None)
            if new_notes_deck_id and new_notes_deck_id != 0:
                deck_name = mw.col.decks.name_if_exists(new_notes_deck_id)
                if deck_name:
                    return deck_name

            # Fall back to regular home deck
            return get_home_deck(given_deck_hash)
    except Exception as e:
        logger.error(f"Error getting new notes home deck: {e}")
        # Fall back to regular home deck
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(e)
        except Exception:
            pass
        return get_home_deck(given_deck_hash)

    return None


def remove_nonexistent_decks():
    strings_data = mw.addonManager.getConfig(__name__)

    if strings_data is not None and len(strings_data) > 0:
        # Create a copy of the config data
        strings_data_copy = strings_data.copy()

        # backwards compatibility
        if "settings" in strings_data_copy:
            del strings_data_copy["settings"]

        if "auth" in strings_data_copy:
            del strings_data_copy["auth"]

        # Only include the specific hash if it exists
        strings_data_to_send = strings_data_copy

        payload = {"deck_hashes": list(strings_data_to_send.keys())}
        try:
            response = api_client.post_json(
                "/CheckDeckAlive", payload, auth=False, timeout=30
            )
            if response.status_code == 200:
                if response.content == "Error":
                    infot = "A Server Error occurred. Please notify us!"
                    aqt.mw.taskman.run_on_main(
                        lambda: aqt.utils.tooltip(
                            infot, parent=QApplication.focusWidget()
                        )
                    )
                else:
                    webresult = json.loads(response.content)  # type: ignore
                    # we need to remove all the decks that don't exist anymore from the strings_data
                    strings_data = mw.addonManager.getConfig(__name__)
                    if strings_data is not None and len(strings_data) > 0:
                        for deck_hash in webresult:
                            if deck_hash in strings_data:
                                del strings_data[deck_hash]
                            else:
                                logger.debug("deck_hash not found in strings_data")
                        mw.addonManager.writeConfig(__name__, strings_data)
                    else:
                        logger.debug("strings_data is None or empty")
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error checking deck alive: {e}")
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"Unexpected error checking deck alive: {e}")
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:
                pass


def _remove_subscription_from_config(deck_hash: str) -> None:
    strings_data = mw.addonManager.getConfig(__name__)
    if not isinstance(strings_data, dict):
        return

    details = strings_data.get(deck_hash)
    if isinstance(details, dict) and details.get("deckId", 0) == 0:
        del strings_data[deck_hash]
        mw.addonManager.writeConfig(__name__, strings_data)
        logger.info(
            "Removed pending subscription %s from config after cancellation", deck_hash
        )


# Kinda ugly, but for backwards compatibility we need to handle both the old and new format
def async_start_pull(input_hash, silent=False):
    remove_nonexistent_decks()
    strings_data = mw.addonManager.getConfig(__name__)
    if strings_data is not None and len(strings_data) > 0:
        # Create a copy of the config data
        strings_data_copy = strings_data.copy()

        # backwards compatibility
        if "settings" in strings_data_copy:
            del strings_data_copy["settings"]

        if "auth" in strings_data_copy:
            del strings_data_copy["auth"]

        # Create the data to send based on whether input_hash is provided
        if input_hash is None:
            strings_data_to_send = strings_data_copy
        else:
            # Only include the specific hash if it exists
            strings_data_to_send = (
                {input_hash: strings_data_copy[input_hash]}
                if input_hash in strings_data_copy
                else {}
            )

        try:
            response = api_client.post_json(
                "/pullChanges", strings_data_to_send, auth=False
            )
            if response.status_code == 200:
                compressed_data = base64.b64decode(response.content)  # type: ignore
                decompressed_data = gzip.decompress(compressed_data)
                webresult = json.loads(decompressed_data.decode("utf-8"))

                try:
                    webresult = _resolve_cache_bootstrap_entries(webresult)
                except CacheArchiveRefreshError as exc:
                    logger.warning(
                        "Cache archive URL rejected; requesting a fresh cache URL: %s",
                        exc,
                    )
                    response = api_client.post_json(
                        "/pullChanges", strings_data_to_send, auth=False
                    )
                    if response.status_code == 200:
                        compressed_data = base64.b64decode(response.content)  # type: ignore
                        decompressed_data = gzip.decompress(compressed_data)
                        webresult = json.loads(decompressed_data.decode("utf-8"))
                        try:
                            webresult = _resolve_cache_bootstrap_entries(webresult)
                        except CacheBootstrapError as refresh_exc:
                            logger.error(
                                "Cache bootstrap failed after refreshing archive URL: %s",
                                refresh_exc,
                            )
                            aqt.mw.taskman.run_on_main(_notify_cache_bootstrap_failure)
                            return (None, None, silent)
                    else:
                        logger.error(
                            "Unable to refresh cache archive URL (HTTP %s)",
                            response.status_code,
                        )
                        aqt.mw.taskman.run_on_main(_notify_cache_bootstrap_failure)
                        return (None, None, silent)
                except CacheBootstrapError as exc:
                    logger.error("Cache bootstrap failed: %s", exc)
                    # Surface a user-visible error on the main thread.
                    aqt.mw.taskman.run_on_main(_notify_cache_bootstrap_failure)
                    return (None, None, silent)

                return (webresult, input_hash, silent)
            elif response.status_code == 401:
                return (None, None, silent)
            else:
                infot = "A Server Error occurred. Please notify us!"
                aqt.mw.taskman.run_on_main(
                    lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
                )
                return (None, None, silent)
        except OperationAbortedError as e:
            logger.info("Pull cancelled: %s", e)
            if input_hash:
                _remove_subscription_from_config(input_hash)
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(
                    "Download cancelled.",
                    parent=QApplication.focusWidget(),
                    period=2000,
                )
            )
            return (None, None, True)
        except (requests.exceptions.RequestException, OSError, ValueError, gzip.BadGzipFile, base64.binascii.Error) as e:  # type: ignore
            logger.error(f"Error pulling changes: {e}")
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:
                pass
            infot = "A Network Error occurred while fetching changes."
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
            )
            return (None, None, silent)
        except Exception as e:
            logger.exception(f"Unexpected error pulling changes: {e}")
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:
                pass
            infot = "An unexpected error occurred while fetching changes."
            aqt.mw.taskman.run_on_main(
                lambda: aqt.utils.tooltip(infot, parent=QApplication.focusWidget())
            )
            return (None, None, silent)


def handle_pull(input_hash, silent=False):
    QueryOp(
        parent=mw,
        op=lambda _: async_start_pull(input_hash, silent),
        success=import_webresult,
    ).with_progress("Fetching Changes from AnkiCollab...").run_in_background()
