"""Tests for import_manager.py — manifest fetching, payload coercion,
path safety, optional tags, note ID lookups.
"""

import os
import zipfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
import base64
import gzip
import json

from utils import OperationAbortedError

from import_manager import (
    CacheBootstrapError,
    _fetch_manifest,
    _safe_destination,
    _coerce_subscription_payload,
    _extract_media_entries,
    update_optional_tag_config,
    get_optional_tags,
    check_optional_tag_changes,
    update_timestamp,
    update_deck_stats_enabled,
    delete_notes,
    wants_to_share_stats,
    CacheBootstrapError,
    CacheArchiveRefreshError,
    async_start_pull,
)

# ──────────────────────────────────────────────────────────────────────
# CacheBootstrapError
# ──────────────────────────────────────────────────────────────────────


class TestCacheBootstrapError:
    def test_is_runtime_error(self):
        assert issubclass(CacheBootstrapError, RuntimeError)


# ──────────────────────────────────────────────────────────────────────
# _fetch_manifest
# ──────────────────────────────────────────────────────────────────────


class TestFetchManifest:
    @patch("import_manager.requests.get")
    def test_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"version": 1, "files": []}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_manifest("http://example.com/manifest.json")
        assert result == {"version": 1, "files": []}

    @patch("import_manager.requests.get")
    def test_network_error(self, mock_get):
        import requests

        mock_get.side_effect = requests.RequestException("timeout")
        with pytest.raises(CacheBootstrapError, match="Unable to download"):
            _fetch_manifest("http://example.com/manifest.json")

    @patch("import_manager.requests.get")
    def test_invalid_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = ValueError("bad json")
        mock_get.return_value = mock_resp

        with pytest.raises(CacheBootstrapError, match="not valid JSON"):
            _fetch_manifest("http://example.com/bad.json")


# ──────────────────────────────────────────────────────────────────────
# _safe_destination
# ──────────────────────────────────────────────────────────────────────


class TestSafeDestination:
    def test_normal_path(self, tmp_path):
        result = _safe_destination(tmp_path, "image.png")
        assert result == (tmp_path / "image.png").resolve()

    def test_nested_path(self, tmp_path):
        result = _safe_destination(tmp_path, "subdir/image.png")
        assert str(result).startswith(str(tmp_path.resolve()))

    def test_traversal_raises(self, tmp_path):
        with pytest.raises(CacheBootstrapError, match="escapes target"):
            _safe_destination(tmp_path, "../../etc/passwd")

    def test_absolute_path_stays_within_root(self, tmp_path):
        """Relative path that resolves safely should succeed."""
        result = _safe_destination(tmp_path, "subdir/../image.png")
        assert result == (tmp_path / "image.png").resolve()


# ──────────────────────────────────────────────────────────────────────
# _coerce_subscription_payload
# ──────────────────────────────────────────────────────────────────────


class TestCoerceSubscriptionPayload:
    def test_dict_with_deck_key(self):
        payload = {"deck": {"name": "Test"}, "meta": True}
        result = _coerce_subscription_payload(payload, "2025-01-01")
        assert result["deck"]["name"] == "Test"
        assert result["deck_last_modified"] == "2025-01-01"

    def test_list_extracts_first_dict(self):
        payload = ["not_a_dict", {"deck": {"name": "X"}}]
        result = _coerce_subscription_payload(payload, None)
        assert result["deck"]["name"] == "X"

    def test_missing_deck_key_raises(self):
        with pytest.raises(CacheBootstrapError, match="missing 'deck'"):
            _coerce_subscription_payload({"other": 1}, None)

    def test_non_dict_raises(self):
        with pytest.raises(CacheBootstrapError, match="not a valid object"):
            _coerce_subscription_payload("string_data", None)

    def test_list_with_no_dicts_raises(self):
        """List containing only non-dict items should fail."""
        with pytest.raises(CacheBootstrapError, match="not a valid object"):
            _coerce_subscription_payload(["a", 42, None], None)

    def test_no_timestamp_skips_field(self):
        result = _coerce_subscription_payload({"deck": {}}, None)
        assert "deck_last_modified" not in result


# ──────────────────────────────────────────────────────────────────────
# _extract_media_entries
# ──────────────────────────────────────────────────────────────────────


class TestExtractMediaEntries:
    def test_extracts_files(self, tmp_path, mw_mock):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)

        # Create a zip with one media file
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("image.png", b"\x89PNG" + b"\x00" * 100)

        with zipfile.ZipFile(zip_path, "r") as zf:
            count = _extract_media_entries(zf, {"path_prefix": ""})

        assert count == 1
        assert (media_dir / "image.png").exists()

    def test_skips_existing_files(self, tmp_path, mw_mock):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        (media_dir / "existing.png").write_bytes(b"already here")
        mw_mock.col.media.dir.return_value = str(media_dir)

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("existing.png", b"new content")

        with zipfile.ZipFile(zip_path, "r") as zf:
            count = _extract_media_entries(zf, {"path_prefix": ""})

        assert count == 0
        # Original content preserved
        assert (media_dir / "existing.png").read_bytes() == b"already here"

    def test_empty_media_info(self, mw_mock):
        zip_path = MagicMock()
        assert _extract_media_entries(zip_path, {}) == 0
        assert _extract_media_entries(zip_path, None) == 0

    def test_raises_when_no_collection(self, mw_no_col):
        zip_path = MagicMock()
        with pytest.raises(CacheBootstrapError, match="not available"):
            _extract_media_entries(zip_path, {"path_prefix": "media/"})

    def test_respects_path_prefix(self, tmp_path, mw_mock):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("prefix/img.png", b"\x89PNG" + b"\x00" * 100)
            zf.writestr("other/skip.png", b"\x89PNG" + b"\x00" * 100)

        with zipfile.ZipFile(zip_path, "r") as zf:
            count = _extract_media_entries(zf, {"path_prefix": "prefix/"})

        assert count == 1
        assert (media_dir / "img.png").exists()
        assert not (media_dir / "skip.png").exists()


# ──────────────────────────────────────────────────────────────────────
# Optional tags
# ──────────────────────────────────────────────────────────────────────


class TestOptionalTags:
    @pytest.fixture(autouse=True)
    def _deck_config(self, mw_mock):
        config = {
            "settings": {},
            "hash_ot": {
                "deckId": 1,
                "timestamp": "2025-01-01 00:00:00",
                "optional_tags": {"tag_a": True, "tag_b": False},
            },
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

    def test_get_optional_tags(self, mw_mock):
        tags = get_optional_tags("hash_ot")
        assert tags == {"tag_a": True, "tag_b": False}

    def test_get_optional_tags_missing_hash(self, mw_mock):
        assert get_optional_tags("missing") == {}

    def test_check_optional_tag_changes_no_change(self, mw_mock):
        assert check_optional_tag_changes("hash_ot", ["tag_a", "tag_b"]) is False

    def test_check_optional_tag_changes_with_change(self, mw_mock):
        assert check_optional_tag_changes("hash_ot", ["tag_a", "tag_c"]) is True


# ──────────────────────────────────────────────────────────────────────
# wants_to_share_stats
# ──────────────────────────────────────────────────────────────────────


class TestWantsToShareStats:
    def test_unknown_deck_returns_false(self, mw_mock):
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: {"settings": {}}
        enabled, ts = wants_to_share_stats("nonexistent")
        assert enabled is False
        assert ts == 0

    def test_enabled_deck(self, mw_mock):
        config = {
            "settings": {},
            "hash_s": {
                "deckId": 1,
                "timestamp": "2025-01-01 00:00:00",
                "share_stats": True,
                "last_stats_timestamp": 12345,
            },
        }
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)
        enabled, ts = wants_to_share_stats("hash_s")
        assert enabled is True
        assert ts == 12345


# ──────────────────────────────────────────────────────────────────────
# _extract_media_entries — new hardening (symlinks, macOS junk, dirs)
# ──────────────────────────────────────────────────────────────────────


class TestExtractMediaEntriesHardening:
    def test_rejects_symlink_entries(self, tmp_path, mw_mock):
        """Archives must not be able to plant symlinks in the media folder."""
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zi = zipfile.ZipInfo("evil_link")
            zi.create_system = 3  # Unix
            zi.external_attr = 0o120777 << 16  # S_IFLNK | 0777
            zf.writestr(zi, "/etc/passwd")

        with zipfile.ZipFile(zip_path, "r") as zf:
            with pytest.raises(CacheBootstrapError, match="Symlink"):
                _extract_media_entries(zf, {"path_prefix": ""})

    def test_skips_macosx_entries_when_no_prefix(self, tmp_path, mw_mock):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("__MACOSX/._foo", b"macosx metadata")
            zf.writestr("foo.png", b"\x89PNG" + b"\x00" * 100)

        with zipfile.ZipFile(zip_path, "r") as zf:
            count = _extract_media_entries(zf, {"path_prefix": ""})

        assert count == 1
        assert (media_dir / "foo.png").exists()
        assert not (media_dir / "._foo").exists()

    def test_skips_directory_entries(self, tmp_path, mw_mock):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(zipfile.ZipInfo("subdir/"), "")
            zf.writestr("subdir/img.png", b"\x89PNG" + b"\x00" * 100)

        with zipfile.ZipFile(zip_path, "r") as zf:
            count = _extract_media_entries(zf, {"path_prefix": ""})

        assert count == 1
        assert (media_dir / "subdir" / "img.png").exists()


# ──────────────────────────────────────────────────────────────────────
# async_start_pull — cache archive refresh / abort / error handling
# ──────────────────────────────────────────────────────────────────────


def _encode_pull_response(payload: dict) -> bytes:
    """Mirror the server's base64(gzip(json)) encoding."""
    return base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8")))


def _fake_pull_response(payload: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = _encode_pull_response(payload)
    return resp


class TestAsyncStartPullCacheRefresh:
    def test_expired_archive_url_triggers_refresh(self, mw_mock):
        """CacheArchiveRefreshError should cause a second /pullChanges call."""
        config = {"h1": {"deckId": 0, "timestamp": "2025-01-01 00:00:00"}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = _fake_pull_response({"h1": "payload"})

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json", return_value=fake_resp
            ) as mock_post,
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=[
                    CacheArchiveRefreshError("expired"),
                    [{"deck": {"name": "Refreshed"}}],
                ],
            ),
        ):
            result = async_start_pull(None)

        assert result == ([{"deck": {"name": "Refreshed"}}], None, False)
        assert mock_post.call_count == 2  # initial + refresh

    def test_refresh_failure_surfaces_error(self, mw_mock):
        """If the refreshed URL also fails, we must abort with a user error."""
        config = {"h1": {"deckId": 0}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = _fake_pull_response({"h1": "payload"})

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=fake_resp),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=[
                    CacheArchiveRefreshError("expired"),
                    CacheBootstrapError("still broken"),
                ],
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as mock_notify,
        ):
            result = async_start_pull(None)

        assert result == (None, None, False)
        mock_notify.assert_called_once()

    def test_cache_bootstrap_error_surfaces_error(self, mw_mock):
        """A plain CacheBootstrapError shows the user-facing error, no refresh."""
        config = {"h1": {"deckId": 0}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = _fake_pull_response({"h1": "payload"})

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch(
                "import_manager.api_client.post_json", return_value=fake_resp
            ) as mock_post,
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=CacheBootstrapError("bad manifest"),
            ),
            patch("import_manager._notify_cache_bootstrap_failure") as mock_notify,
        ):
            result = async_start_pull(None)

        assert result == (None, None, False)
        mock_notify.assert_called_once()
        assert mock_post.call_count == 1  # no refresh attempted

    def test_abort_removes_pending_subscription(self, mw_mock):
        """OperationAbortedError with an input_hash must clean up the config."""
        config = {"h1": {"deckId": 0, "timestamp": "2025-01-01 00:00:00"}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = _fake_pull_response({"h1": "payload"})

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=fake_resp),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=OperationAbortedError("cancelled"),
            ),
            patch("import_manager._remove_subscription_from_config") as mock_remove,
        ):
            result = async_start_pull("h1")

        assert result == (None, None, True)
        mock_remove.assert_called_once_with("h1")

    def test_abort_without_input_hash_keeps_config(self, mw_mock):
        """An update (no input_hash) must not purge the subscription."""
        config = {"h1": {"deckId": 1, "timestamp": "2025-01-01 00:00:00"}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = _fake_pull_response({"h1": "payload"})

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=fake_resp),
            patch(
                "import_manager._resolve_cache_bootstrap_entries",
                side_effect=OperationAbortedError("cancelled"),
            ),
            patch("import_manager._remove_subscription_from_config") as mock_remove,
        ):
            result = async_start_pull(None)

        assert result == (None, None, True)
        mock_remove.assert_not_called()

    def test_server_error_response_returns_none(self, mw_mock):
        """Non-200/401 responses must return the sentinel tuple, not crash."""
        config = {"h1": {"deckId": 0}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = MagicMock()
        fake_resp.status_code = 500

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=fake_resp),
        ):
            result = async_start_pull(None)

        assert result == (None, None, False)

    def test_unauthorized_response_returns_none(self, mw_mock):
        config = {"h1": {"deckId": 0}}
        mw_mock.addonManager.getConfig.side_effect = lambda *a, **kw: dict(config)

        fake_resp = MagicMock()
        fake_resp.status_code = 401

        with (
            patch("import_manager.remove_nonexistent_decks"),
            patch("import_manager.api_client.post_json", return_value=fake_resp),
        ):
            result = async_start_pull(None)

        assert result == (None, None, False)
