"""Tests for downloading and unpacking a deck's cache archive.

``import_manager._subscription_from_manifest`` is the resumable downloader that
turns a manifest plus an archive into a subscription payload.  It runs on the
user's connection, so the failures worth testing are the ones that could
silently produce a truncated or corrupt deck: a partial transfer that is
treated as complete, a retry storm, an archive that grows past the size cap, a
cancellation that is ignored, or a temp file left behind.

Everything here asserts transfer and unpacking behaviour — never log text or
progress wording.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import zipfile
from unittest.mock import MagicMock, patch

import pytest
import requests

import import_manager
from import_manager import (
    MAX_DOWNLOAD_ATTEMPTS,
    CacheArchiveRefreshError,
    CacheBootstrapError,
    _subscription_from_manifest,
)
from utils import OperationAbortedError

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal stand-in for a streamed ``requests`` response."""

    def __init__(self, body=b"", status_code=200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


def _make_zip(deck_payload=None, media=None, compression=None, target_size=None):
    if deck_payload is None:
        deck_payload = {"deck": {"name": "Remote"}, "meta": 1}
    deck_bytes = json.dumps(deck_payload).encode("utf-8")
    if compression == "gzip":
        deck_bytes = gzip.compress(deck_bytes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("deck.json", deck_bytes)
        for name, payload in (media or {}).items():
            zf.writestr(name, payload)

    base = buf.getvalue()
    if target_size and target_size > len(base):
        with zipfile.ZipFile(buf, "a") as zf:
            zf.comment = b"p" * (target_size - len(base))
        assert len(buf.getvalue()) == target_size
    return buf.getvalue()


def _manifest(path="deck.json", compression=None, media=None, updated="2025-05-05"):
    return {
        "deck_data": {"path": path, "compression": compression},
        "media": media if media is not None else {},
        "source_last_update": updated,
    }


def _run_manifest(mw_mock, responses, manifest=None, archive_url="http://x/a"):
    """Drive ``_subscription_from_manifest`` with a mocked session."""
    session = MagicMock(name="session")
    if isinstance(responses, list):
        session.get.side_effect = responses
    else:
        session.get.return_value = responses

    created = []
    real_named = import_manager.tempfile.NamedTemporaryFile

    def _factory(*args, **kwargs):
        handle = real_named(*args, **kwargs)
        created.append(handle.name)
        return handle

    with (
        patch.object(
            import_manager.api_client, "session_with_retries", return_value=session
        ),
        patch("import_manager.tempfile.NamedTemporaryFile", side_effect=_factory),
    ):
        result = _subscription_from_manifest(
            "deck-hash", manifest if manifest is not None else _manifest(), archive_url
        )
    return result, session, created


def _ok(body, status_code=200):
    return _FakeResponse(
        body, status_code=status_code, headers={"Content-Length": str(len(body))}
    )


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────


class TestManifestValidation:
    @pytest.mark.parametrize("archive_url", [None, ""])
    def test_a_manifest_without_an_archive_url_is_rejected(self, mw_mock, archive_url):
        with pytest.raises(CacheBootstrapError):
            _subscription_from_manifest("h", _manifest(), archive_url)

    def test_a_manifest_without_a_deck_path_is_rejected(self, mw_mock):
        body = _make_zip()
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _ok(body), manifest=_manifest(path=None))

    def test_a_deck_path_missing_from_the_archive_is_rejected(self, mw_mock):
        body = _make_zip()
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _ok(body), manifest=_manifest(path="other.json"))

    def test_an_unsupported_compression_is_rejected(self, mw_mock):
        body = _make_zip()
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _ok(body), manifest=_manifest(compression="lzma"))

    def test_a_corrupt_compressed_deck_is_rejected(self, mw_mock):
        body = _make_zip()  # not actually gzip
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _ok(body), manifest=_manifest(compression="gzip"))

    def test_a_deck_that_is_not_json_is_rejected(self, mw_mock):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", b"\xff\xfe not json")
        body = buf.getvalue()
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _ok(body))

    def test_a_deck_payload_without_the_deck_field_is_rejected(self, mw_mock):
        body = _make_zip(deck_payload={"other": 1})
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _ok(body))


# ──────────────────────────────────────────────────────────────────────
# Successful unpacking
# ──────────────────────────────────────────────────────────────────────


class TestManifestUnpacking:
    def test_a_plain_deck_is_returned_with_its_modification_time(self, mw_mock):
        payload = {"deck": {"name": "Remote"}, "meta": 1}
        body = _make_zip(deck_payload=payload)

        result, _session, created = _run_manifest(mw_mock, _ok(body))

        assert result["deck"] == {"name": "Remote"}
        assert result["meta"] == 1
        assert result["deck_last_modified"] == "2025-05-05"
        # The temporary download file never survives the call.
        assert created and not os.path.exists(created[0])

    def test_a_missing_modification_time_is_not_invented(self, mw_mock):
        body = _make_zip()
        result, _s, _c = _run_manifest(
            mw_mock, _ok(body), manifest=_manifest(updated=None)
        )
        assert "deck_last_modified" not in result

    @pytest.mark.parametrize("compression", ["gzip", "gz", "GZIP", "Gz"])
    def test_a_compressed_deck_is_decompressed(self, mw_mock, compression):
        body = _make_zip(deck_payload={"deck": {"name": "Zipped"}}, compression="gzip")
        result, _s, _c = _run_manifest(
            mw_mock, _ok(body), manifest=_manifest(compression=compression)
        )
        assert result["deck"] == {"name": "Zipped"}

    @pytest.mark.parametrize("compression", ["", "none", None])
    def test_an_uncompressed_deck_is_accepted(self, mw_mock, compression):
        body = _make_zip()
        result, _s, _c = _run_manifest(
            mw_mock, _ok(body), manifest=_manifest(compression=compression)
        )
        assert result["deck"] == {"name": "Remote"}

    def test_the_archive_is_streamed_and_closed(self, mw_mock):
        body = _make_zip()
        resp = _ok(body)
        _result, session, _created = _run_manifest(mw_mock, resp)

        assert session.get.call_args.args[0] == "http://x/a"
        assert session.get.call_args.kwargs["stream"] is True
        assert session.get.call_args.kwargs["headers"] == {}
        assert resp.closed is True

    def test_media_files_land_in_the_media_folder(self, mw_mock, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)
        body = _make_zip(media={"media/img.png": b"png"})

        _run_manifest(
            mw_mock, _ok(body), manifest=_manifest(media={"path_prefix": "media/"})
        )

        assert (media_dir / "img.png").read_bytes() == b"png"

    @pytest.mark.parametrize("prefix", ["media/", "/media", "/media/"])
    def test_a_media_prefix_is_normalised(self, mw_mock, tmp_path, prefix):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)
        body = _make_zip(media={"media/img.png": b"png", "other/skip.png": b"png"})

        _run_manifest(
            mw_mock, _ok(body), manifest=_manifest(media={"path_prefix": prefix})
        )

        assert (media_dir / "img.png").exists()
        assert not (media_dir / "skip.png").exists()

    def test_a_root_prefix_keeps_the_archive_layout(self, mw_mock, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)
        body = _make_zip(
            media={"a.png": b"a", "__MACOSX/._a.png": b"junk", "sub/b.png": b"b"}
        )

        _run_manifest(
            mw_mock, _ok(body), manifest=_manifest(media={"path_prefix": "/"})
        )

        assert (media_dir / "a.png").exists()
        assert (media_dir / "sub" / "b.png").exists()
        # macOS resource forks are not media.
        assert not (media_dir / "__MACOSX").exists()

    def test_missing_media_metadata_skips_extraction(self, mw_mock, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        mw_mock.col.media.dir.return_value = str(media_dir)
        body = _make_zip(media={"a.png": b"a"})
        manifest = _manifest()
        del manifest["media"]

        _run_manifest(mw_mock, _ok(body), manifest=manifest)

        assert not (media_dir / "a.png").exists()


# ──────────────────────────────────────────────────────────────────────
# The download state machine
# ──────────────────────────────────────────────────────────────────────


class TestDownloadFailures:
    def test_rejected_credentials_ask_for_a_fresh_archive_url(self, mw_mock):
        for status in (401, 403):
            with pytest.raises(CacheArchiveRefreshError):
                _run_manifest(mw_mock, _FakeResponse(b"", status_code=status))

    def test_a_server_error_is_not_treated_as_an_expired_url(self, mw_mock):
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, _FakeResponse(b"", status_code=500))

    def test_retries_are_exhausted_before_giving_up(self, mw_mock):
        full = _make_zip()
        truncated = _FakeResponse(
            full[:5], status_code=206, headers={"Content-Length": str(len(full))}
        )

        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, [truncated] * MAX_DOWNLOAD_ATTEMPTS)

    def test_a_short_transfer_is_never_reported_as_complete(self, mw_mock):
        """A body shorter than Content-Length must not be unpacked."""
        full = _make_zip()
        truncated = _FakeResponse(
            full[:5], status_code=206, headers={"Content-Length": str(len(full))}
        )
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, truncated)

    def test_a_total_from_content_range_is_enforced(self, mw_mock):
        body = _make_zip()
        size = len(body)
        resp = _FakeResponse(
            body,
            status_code=206,
            headers={
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size * 5}",
            },
        )
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, [resp] * MAX_DOWNLOAD_ATTEMPTS)

    def test_a_declared_oversized_archive_is_rejected_up_front(self, mw_mock):
        body = _make_zip()
        resp = _FakeResponse(
            body,
            headers={"Content-Length": str(import_manager.MAX_ARCHIVE_BYTES + 1)},
        )
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, resp)

    def test_an_undeclared_oversized_stream_is_rejected(self, mw_mock):
        body = _make_zip()
        resp = _FakeResponse(body)  # no Content-Length at all
        with patch("import_manager.MAX_ARCHIVE_BYTES", 10):
            with pytest.raises(CacheBootstrapError):
                _run_manifest(mw_mock, resp)

    def test_a_nonsense_content_length_is_ignored(self, mw_mock):
        body = _make_zip()
        resp = _FakeResponse(body, headers={"Content-Length": "not-a-number"})
        result, _s, _c = _run_manifest(mw_mock, resp)
        assert result["deck"] == {"name": "Remote"}

    def test_a_failed_download_leaves_no_temp_file_behind(self, mw_mock):
        resp = _FakeResponse(b"", status_code=500)
        created = []
        real_named = import_manager.tempfile.NamedTemporaryFile

        def _factory(*args, **kwargs):
            handle = real_named(*args, **kwargs)
            created.append(handle.name)
            return handle

        session = MagicMock()
        session.get.return_value = resp
        with (
            patch.object(
                import_manager.api_client, "session_with_retries", return_value=session
            ),
            patch("import_manager.tempfile.NamedTemporaryFile", side_effect=_factory),
        ):
            with pytest.raises(CacheBootstrapError):
                _subscription_from_manifest("h", _manifest(), "http://x/a")

        assert created and not os.path.exists(created[0])

    def test_cancellation_aborts_the_download(self, mw_mock):
        body = _make_zip()
        mw_mock.progress.want_cancel.return_value = True
        with pytest.raises(OperationAbortedError):
            _run_manifest(mw_mock, _ok(body))

    def test_a_missing_progress_object_does_not_break_the_download(self, mw_mock):
        body = _make_zip()
        with patch.object(
            import_manager.aqt, "mw", MagicMock(progress=None, taskman=mw_mock.taskman)
        ):
            result, _s, _c = _run_manifest(mw_mock, _ok(body))
        assert result["deck"] == {"name": "Remote"}


class TestDownloadResume:
    def test_an_expired_range_is_retried_from_scratch(self, mw_mock):
        body = _make_zip()
        rejected = _FakeResponse(b"", status_code=416)
        retry = _ok(body)

        result, session, _created = _run_manifest(mw_mock, [rejected, retry])

        assert result["deck"] == {"name": "Remote"}
        assert session.get.call_count == 2
        assert session.get.call_args_list[0].kwargs["headers"] == {}

    def test_a_truncated_transfer_is_resumed_with_a_range_request(self, mw_mock):
        full = _make_zip()
        half = len(full) // 2
        first = _FakeResponse(
            full[:half], status_code=206, headers={"Content-Length": str(len(full))}
        )
        second = _FakeResponse(
            full[half:],
            status_code=206,
            headers={
                "Content-Length": str(len(full)),
                "Content-Range": f"bytes {half}-{len(full) - 1}/{len(full)}",
            },
        )

        result, session, _created = _run_manifest(mw_mock, [first, second])

        assert result["deck"] == {"name": "Remote"}
        assert session.get.call_args_list[1].kwargs["headers"] == {
            "Range": f"bytes={half}-"
        }

    def test_a_server_that_ignores_ranges_still_completes(self, mw_mock):
        full = _make_zip()
        half = len(full) // 2
        first = _FakeResponse(
            full[:half], status_code=206, headers={"Content-Length": str(len(full))}
        )
        ignored = _FakeResponse(full, status_code=200)

        result, session, _created = _run_manifest(mw_mock, [first, ignored])

        assert result["deck"] == {"name": "Remote"}
        assert session.get.call_count == 2

    def test_a_server_that_always_ignores_ranges_is_abandoned(self, mw_mock):
        full = _make_zip()
        truncated = _FakeResponse(
            full[:5], status_code=200, headers={"Content-Length": str(len(full))}
        )
        with pytest.raises(CacheBootstrapError):
            _run_manifest(mw_mock, [truncated] * MAX_DOWNLOAD_ATTEMPTS)
