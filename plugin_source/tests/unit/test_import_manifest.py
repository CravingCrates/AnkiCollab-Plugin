"""Mutation-driven tests for import_manager's manifest/bootstrap parser.

This is the externally-supplied-data boundary: cache manifests and archives
come from the server and must be validated before anything touches the user's
collection.
"""

import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest
import requests

from import_manager import (
    CacheArchiveRefreshError,
    CacheBootstrapError,
    _resolve_cache_bootstrap_entries,
    _subscription_from_manifest,
)


def _make_archive(deck_json, path="deck.json"):
    """Build an in-memory zip archive containing the deck JSON at ``path``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(path, json.dumps(deck_json).encode("utf-8"))
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, content=b"", content_length=None, status_code=200):
        self._content = content
        self.headers = {}
        self.status_code = status_code
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def close(self):
        pass


class _FailingChunkResponse(_FakeResponse):
    """Yields ``fail_after`` bytes, then raises a RequestException.

    Used to simulate a broken transfer mid-stream so the retry/resume
    code path can be exercised.
    """

    def __init__(self, content, fail_after=10, status_code=200):
        super().__init__(content, status_code=status_code)
        self._fail_after = fail_after

    def iter_content(self, chunk_size=1024):
        yield self._content[: self._fail_after]
        raise requests.RequestException("simulated interruption")


@pytest.fixture
def mock_session():
    """Patch ``api_client.session_with_retries`` to return a MagicMock session."""
    session = MagicMock()
    with patch("import_manager.api_client.session_with_retries", return_value=session):
        yield session


# ──────────────────────────────────────────────────────────────────────
# _subscription_from_manifest — baseline behaviour
# ──────────────────────────────────────────────────────────────────────


class TestSubscriptionFromManifest:
    def test_missing_archive_url(self, mw_mock, mock_session):
        with pytest.raises(CacheBootstrapError, match="missing archive URL"):
            _subscription_from_manifest("hash1", {}, None)

    def test_download_failure(self, mw_mock, mock_session):
        mock_session.get.side_effect = requests.RequestException("boom")
        with pytest.raises(CacheBootstrapError, match="Unable to download"):
            _subscription_from_manifest("hash1", {}, "http://x/archive.zip")

    def test_missing_deck_path(self, mw_mock, mock_session):
        archive = _make_archive({"deck": {"name": "D"}})
        mock_session.get.return_value = _FakeResponse(archive)
        manifest = {"deck_data": {}, "media": {}}
        with pytest.raises(CacheBootstrapError, match="missing deck data path"):
            _subscription_from_manifest("hash1", manifest, "http://x/a.zip")

    def test_deck_data_missing_in_archive(self, mw_mock, mock_session):
        archive = _make_archive({"deck": {"name": "D"}}, path="other.json")
        mock_session.get.return_value = _FakeResponse(archive)
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        with pytest.raises(CacheBootstrapError, match="not present in cache"):
            _subscription_from_manifest("hash1", manifest, "http://x/a.zip")

    def test_unsupported_compression(self, mw_mock, mock_session):
        archive = _make_archive({"deck": {"name": "D"}})
        mock_session.get.return_value = _FakeResponse(archive)
        manifest = {
            "deck_data": {"path": "deck.json", "compression": "bz2"},
            "media": {},
        }
        with pytest.raises(CacheBootstrapError, match="Unsupported deck compression"):
            _subscription_from_manifest("hash1", manifest, "http://x/a.zip")

    def test_invalid_deck_json(self, mw_mock, mock_session):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", b"not json{")
        mock_session.get.return_value = _FakeResponse(buf.getvalue())
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        with pytest.raises(CacheBootstrapError, match="not valid JSON"):
            _subscription_from_manifest("hash1", manifest, "http://x/a.zip")

    def test_happy_path(self, mw_mock, mock_session):
        deck_payload = {"deck": {"name": "Test", "crowdanki_uuid": "d1"}, "meta": {}}
        archive = _make_archive(deck_payload)
        mock_session.get.return_value = _FakeResponse(archive)
        manifest = {
            "deck_data": {"path": "deck.json"},
            "media": {},
            "source_last_update": "2025-01-01",
        }
        sub = _subscription_from_manifest("hash1", manifest, "http://x/a.zip")
        assert sub["deck"]["name"] == "Test"
        assert sub["deck_last_modified"] == "2025-01-01"

    def test_cancel_during_download(self, mw_mock, mock_session):
        """User cancel mid-download aborts with OperationAbortedError."""
        archive = _make_archive({"deck": {"name": "D"}})

        class _CancellingResponse(_FakeResponse):
            def iter_content(self, chunk_size=1024):
                yield self._content[:4]
                yield b""

        mw_mock.progress.want_cancel.return_value = True
        mock_session.get.return_value = _CancellingResponse(archive)
        from utils import OperationAbortedError

        with pytest.raises(OperationAbortedError):
            _subscription_from_manifest(
                "hash1", {"deck_data": {}, "media": {}}, "http://x/a.zip"
            )


# ──────────────────────────────────────────────────────────────────────
# _subscription_from_manifest — retry / session / Range handling
# ──────────────────────────────────────────────────────────────────────


class TestSubscriptionFromManifestRetries:
    def test_http_401_raises_cache_archive_refresh_error(self, mw_mock, mock_session):
        """401 means the archive URL is expired; caller must refresh."""
        mock_session.get.return_value = _FakeResponse(status_code=401)
        with pytest.raises(CacheArchiveRefreshError, match="expired or was rejected"):
            _subscription_from_manifest(
                "h1", {"deck_data": {}, "media": {}}, "http://x/a.zip"
            )
        # Should not have retried — 401 is a terminal signal.
        assert mock_session.get.call_count == 1

    def test_http_403_raises_cache_archive_refresh_error(self, mw_mock, mock_session):
        mock_session.get.return_value = _FakeResponse(status_code=403)
        with pytest.raises(CacheArchiveRefreshError, match="expired or was rejected"):
            _subscription_from_manifest(
                "h1", {"deck_data": {}, "media": {}}, "http://x/a.zip"
            )
        assert mock_session.get.call_count == 1

    def test_cache_archive_refresh_is_not_retried(self, mw_mock, mock_session):
        """Even with transient failures before it, a 401 stops the loop."""
        mock_session.get.side_effect = [
            requests.RequestException("transient"),
            _FakeResponse(status_code=401),
        ]
        with pytest.raises(CacheArchiveRefreshError):
            _subscription_from_manifest(
                "h1", {"deck_data": {}, "media": {}}, "http://x/a.zip"
            )
        assert mock_session.get.call_count == 2

    def test_retries_exhausted_raises(self, mw_mock, mock_session):
        """Persistent failures hit the 4-attempt limit and raise."""
        mock_session.get.side_effect = requests.RequestException("boom")
        with pytest.raises(CacheBootstrapError, match="Unable to download"):
            _subscription_from_manifest(
                "h1", {"deck_data": {}, "media": {}}, "http://x/a.zip"
            )
        assert mock_session.get.call_count == 4

    def test_transient_failure_then_success(self, mw_mock, mock_session):
        """A single transient failure should be recovered from."""
        archive = _make_archive({"deck": {"name": "Recovered"}})
        mock_session.get.side_effect = [
            requests.RequestException("transient"),
            _FakeResponse(archive),
        ]
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")
        assert sub["deck"]["name"] == "Recovered"
        assert mock_session.get.call_count == 2

    def test_session_is_reused_across_retries(self, mw_mock):
        """session_with_retries must be called once, not per attempt."""
        archive = _make_archive({"deck": {"name": "D"}})
        session = MagicMock()
        session.get.side_effect = [
            requests.RequestException("transient"),
            _FakeResponse(archive),
        ]
        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ) as mock_swr:
            manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
            _subscription_from_manifest("h1", manifest, "http://x/a.zip")
        assert mock_swr.call_count == 1
        assert session.get.call_count == 2


class TestRangeResume:
    def test_range_header_sent_on_resume(self, mw_mock, mock_session):
        """After a partial download, the retry must send a Range header."""
        archive = _make_archive({"deck": {"name": "D"}})
        call_headers = []

        def fake_get(url, headers=None, **kwargs):
            call_headers.append(dict(headers or {}))
            if len(call_headers) == 1:
                return _FailingChunkResponse(archive, fail_after=10)
            # Resume: return the remaining bytes
            resp = _FakeResponse(archive[10:], status_code=206)
            resp.headers["Content-Range"] = (
                f"bytes 10-{len(archive) - 1}/{len(archive)}"
            )
            return resp

        mock_session.get.side_effect = fake_get
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert sub["deck"]["name"] == "D"
        assert "Range" not in call_headers[0]
        assert call_headers[1]["Range"] == "bytes=10-"

    def test_short_transfer_retries(self, mw_mock, mock_session):
        """A response that delivers fewer bytes than Content-Length retries."""
        archive = _make_archive({"deck": {"name": "D"}})

        call_count = [0]

        def fake_get(url, headers=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Content-Length claims full archive, but only 10 bytes come.
                return _FakeResponse(archive[:10], content_length=len(archive))
            resp = _FakeResponse(archive[10:], status_code=206)
            resp.headers["Content-Range"] = (
                f"bytes 10-{len(archive) - 1}/{len(archive)}"
            )
            return resp

        mock_session.get.side_effect = fake_get
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")
        assert sub["deck"]["name"] == "D"
        assert call_count[0] == 2

    def test_http_416_resets_and_retries(self, mw_mock, mock_session):
        """416 (Range Not Satisfiable) must reset download state and retry."""
        archive = _make_archive({"deck": {"name": "D"}})

        call_count = [0]

        def fake_get(url, headers=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _FailingChunkResponse(archive, fail_after=10)
            if call_count[0] == 2:
                return _FakeResponse(status_code=416)
            return _FakeResponse(archive)

        mock_session.get.side_effect = fake_get
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")
        assert sub["deck"]["name"] == "D"
        assert call_count[0] == 3

    def test_server_ignoring_range_three_times_raises(self, mw_mock, mock_session):
        """If server returns 200 to every Range request, give up after 3."""
        archive = _make_archive({"deck": {"name": "D"}})
        mock_session.get.return_value = _FailingChunkResponse(archive, fail_after=10)
        manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
        with pytest.raises(CacheBootstrapError, match="does not support resume"):
            _subscription_from_manifest("h1", manifest, "http://x/a.zip")


# ──────────────────────────────────────────────────────────────────────
# _subscription_from_manifest — compression
# ──────────────────────────────────────────────────────────────────────


class TestCompression:
    def test_gzip_deck_data(self, mw_mock, mock_session):
        import gzip as gzip_mod

        deck_payload = {"deck": {"name": "GZ"}, "meta": {}}
        compressed = gzip_mod.compress(json.dumps(deck_payload).encode("utf-8"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", compressed)

        mock_session.get.return_value = _FakeResponse(buf.getvalue())
        manifest = {
            "deck_data": {"path": "deck.json", "compression": "gzip"},
            "media": {},
        }
        sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")
        assert sub["deck"]["name"] == "GZ"

    def test_gz_alias_is_accepted(self, mw_mock, mock_session):
        import gzip as gzip_mod

        deck_payload = {"deck": {"name": "GZAlias"}, "meta": {}}
        compressed = gzip_mod.compress(json.dumps(deck_payload).encode("utf-8"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", compressed)

        mock_session.get.return_value = _FakeResponse(buf.getvalue())
        manifest = {
            "deck_data": {"path": "deck.json", "compression": "gz"},
            "media": {},
        }
        sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")
        assert sub["deck"]["name"] == "GZAlias"

    def test_gzip_decompression_failure_raises(self, mw_mock, mock_session):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", b"not valid gzip")

        mock_session.get.return_value = _FakeResponse(buf.getvalue())
        manifest = {
            "deck_data": {"path": "deck.json", "compression": "gzip"},
            "media": {},
        }
        with pytest.raises(CacheBootstrapError, match="Unable to decompress"):
            _subscription_from_manifest("h1", manifest, "http://x/a.zip")


# ──────────────────────────────────────────────────────────────────────
# _resolve_cache_bootstrap_entries
# ──────────────────────────────────────────────────────────────────────


class TestResolveCacheBootstrapEntries:
    def test_non_list_returns_as_is(self):
        assert _resolve_cache_bootstrap_entries("not-a-list") == "not-a-list"

    def test_non_bootstrap_entries_passthrough(self):
        entries = [{"mode": "normal", "deck": {}}, "plain"]
        assert _resolve_cache_bootstrap_entries(entries) == entries

    def test_malformed_entry_raises(self):
        entries = [{"mode": "cache-bootstrap", "deck_hash": "h1"}]  # no manifest
        with pytest.raises(CacheBootstrapError, match="Malformed cache bootstrap"):
            _resolve_cache_bootstrap_entries(entries)

    def test_missing_deck_hash_raises(self):
        entries = [
            {
                "mode": "cache-bootstrap",
                "manifest": {"manifest_presigned_url": "http://x/m.json"},
            }
        ]
        with pytest.raises(CacheBootstrapError, match="Malformed cache bootstrap"):
            _resolve_cache_bootstrap_entries(entries)

    def test_valid_entry_resolved(self):
        entry = {
            "mode": "cache-bootstrap",
            "deck_hash": "h1",
            "manifest": {"manifest_presigned_url": "http://x/manifest.json"},
        }
        with (
            patch(
                "import_manager._fetch_manifest", return_value={"deck_data": {}}
            ) as m_fetch,
            patch(
                "import_manager._subscription_from_manifest",
                return_value={"deck": {"name": "Resolved"}},
            ) as m_sub,
        ):
            result = _resolve_cache_bootstrap_entries([entry])
        assert result == [{"deck": {"name": "Resolved"}}]
        m_fetch.assert_called_once_with("http://x/manifest.json")
        m_sub.assert_called_once()

    def test_bootstrap_error_propagates(self):
        entry = {
            "mode": "cache-bootstrap",
            "deck_hash": "h1",
            "manifest": {"manifest_presigned_url": "http://x/manifest.json"},
        }
        with patch(
            "import_manager._fetch_manifest",
            side_effect=CacheBootstrapError("broken"),
        ):
            with pytest.raises(CacheBootstrapError, match="broken"):
                _resolve_cache_bootstrap_entries([entry])

    def test_cache_archive_refresh_error_propagates(self):
        entry = {
            "mode": "cache-bootstrap",
            "deck_hash": "h1",
            "manifest": {"manifest_presigned_url": "http://x/manifest.json"},
        }
        with patch(
            "import_manager._fetch_manifest",
            side_effect=CacheArchiveRefreshError("expired"),
        ):
            with pytest.raises(CacheArchiveRefreshError, match="expired"):
                _resolve_cache_bootstrap_entries([entry])

    def test_archive_url_passed_to_subscription(self):
        entry = {
            "mode": "cache-bootstrap",
            "deck_hash": "h1",
            "manifest": {
                "manifest_presigned_url": "http://x/m.json",
                "archive_presigned_url": "http://x/a.zip",
            },
        }
        with (
            patch("import_manager._fetch_manifest", return_value={"deck_data": {}}),
            patch(
                "import_manager._subscription_from_manifest",
                return_value={"deck": {"name": "X"}},
            ) as m_sub,
        ):
            _resolve_cache_bootstrap_entries([entry])
        # Third positional arg is the archive URL.
        assert m_sub.call_args[0][2] == "http://x/a.zip"


class _RangeAwareSession:
    """Fake requests.Session that honors Range headers and can break mid-body.

    Parameters
    ----------
    content : bytes
        The "full file" the server serves.
    break_on : dict[int, int]
        Maps attempt-number → number of bytes to deliver from *this response's
        body* before raising ``requests.ConnectionError``. Body-relative, so
        an attempt resuming at offset N that should break at absolute offset M
        uses ``break_on[attempt] = M - N``.
    honor_range : bool | dict[int, bool]
        If a bool, applies to every attempt. If a dict, looked up per attempt
        (missing attempts default to True). False means the server responds
        with a 200 and the full body even when a Range header is present.
    """

    def __init__(self, content, break_on=None, honor_range=True):
        self.content = content
        self.break_on = break_on or {}
        self.honor_range = honor_range
        self.attempt = 0
        self.range_headers: list[str | None] = []
        self.request_kwargs: list[dict] = []

    def _honors_range(self, attempt: int) -> bool:
        if isinstance(self.honor_range, bool):
            return self.honor_range
        return self.honor_range.get(attempt, True)

    def get(self, url, headers=None, stream=True, timeout=None, **kwargs):
        self.attempt += 1
        headers = dict(headers or {})
        self.range_headers.append(headers.get("Range"))
        self.request_kwargs.append({"stream": stream, "timeout": timeout, **kwargs})

        start = 0
        if headers.get("Range"):
            start = int(headers["Range"].split("=", 1)[1].rstrip("-"))

        honors = self._honors_range(self.attempt)

        if honors and start > 0:
            body = self.content[start:]
            resp = _FakeResponse(body, status_code=206)
            resp.headers["Content-Range"] = (
                f"bytes {start}-{len(self.content) - 1}/{len(self.content)}"
            )
        else:
            body = self.content
            resp = _FakeResponse(body, content_length=len(self.content))

        yield_amount = self.break_on.get(self.attempt)
        if yield_amount is not None and 0 < yield_amount < len(body):
            resp = _InterruptibleResponse(resp, yield_amount=yield_amount)
        return resp


class _InterruptibleResponse(_FakeResponse):
    """Wraps another response, yielding ``yield_amount`` bytes then raising.

    ``yield_amount`` counts bytes from *this response's body* — do not pass an
    absolute offset from the file.
    """

    def __init__(self, inner: _FakeResponse, yield_amount: int):
        super().__init__(b"", status_code=inner.status_code)
        self.headers = dict(inner.headers)
        self._inner = inner
        self._yield_amount = yield_amount

    def iter_content(self, chunk_size=1024):
        yielded = 0
        for chunk in self._inner.iter_content(chunk_size=chunk_size):
            remaining = self._yield_amount - yielded
            if len(chunk) >= remaining:
                head = chunk[:remaining]
                if head:
                    yield head
                raise requests.ConnectionError("simulated interruption")
            yield chunk
            yielded += len(chunk)
        raise requests.ConnectionError("simulated interruption")


class TestResumeOnInterruption:
    def test_retry_sends_exact_range_header(self, mw_mock):
        """After a partial download, the retry must send Range: bytes=N-."""
        archive = _make_archive({"deck": {"name": "Resumed"}})
        break_at = len(archive) // 2
        session = _RangeAwareSession(archive, break_on={1: break_at})

        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
            sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert sub["deck"]["name"] == "Resumed"
        assert session.range_headers == [None, f"bytes={break_at}-"]

    def test_reassembled_file_is_valid_zip(self, mw_mock):
        """Proves bytes were appended at the right offset, not restarted.

        A zip's central directory lives at the end of the file. If the
        reassembled temp file had a gap (wrong offset) or duplicated head
        (restart-without-truncate), the zip would fail to open.
        """
        deck_payload = {
            "deck": {"name": "Integrity", "crowdanki_uuid": "abc123"},
            "meta": {"notes": ["x" * 200, "y" * 200]},
        }
        archive = _make_archive(deck_payload)
        assert len(archive) > 200

        cut1 = len(archive) // 4
        cut2 = len(archive) // 2
        # Attempt 2 resumes at cut1, so it must deliver (cut2 - cut1) bytes to
        # land the file at absolute offset cut2 before breaking.
        session = _RangeAwareSession(archive, break_on={1: cut1, 2: cut2 - cut1})

        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
            sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert sub["deck"]["name"] == "Integrity"
        assert session.range_headers == [
            None,
            f"bytes={cut1}-",
            f"bytes={cut2}-",
        ]

    def test_resume_does_not_retruncate_file(self, mw_mock):
        """A restart-from-zero would truncate the temp file; verify it doesn't.

        If the code truncated on each retry, attempt 3's Range would be
        ``bytes={cut1}-`` again. Because the file is preserved, it is
        ``bytes={cut2}-``.
        """
        archive = _make_archive({"deck": {"name": "Cumulative"}})
        cut1 = 40
        cut2 = 80
        assert len(archive) > cut2
        session = _RangeAwareSession(archive, break_on={1: cut1, 2: cut2 - cut1})

        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
            _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert session.range_headers[2] == f"bytes={cut2}-"

    def test_server_ignoring_range_resets_downloaded(self, mw_mock):
        """When the server returns 200 to a Range request, the client resets
        ``downloaded`` to 0 — so the next attempt's Range reflects only the
        bytes written since the reset, not the cumulative total.

        Attempt 1: no Range, break at cut1                  → downloaded = cut1
        Attempt 2: Range=bytes=cut1-, server returns 200   → reset to 0,
                   break at cut2 (body-relative)            → downloaded = cut2
        Attempt 3: Range must be bytes=cut2-, NOT
                   bytes={cut1 + cut2}-.
        """
        archive = _make_archive({"deck": {"name": "IgnoredRange"}})
        cut1 = 100
        cut2 = 50
        assert len(archive) > cut1 > cut2 > 0

        session = _RangeAwareSession(
            archive,
            break_on={1: cut1, 2: cut2},
            honor_range={2: False},  # only attempt 2 ignores Range
        )

        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
            sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert sub["deck"]["name"] == "IgnoredRange"
        assert session.range_headers == [
            None,
            f"bytes={cut1}-",
            f"bytes={cut2}-",  # NOT f"bytes={cut1 + cut2}-"
        ]

    def test_resume_sends_stream_true(self, mw_mock):
        """The download uses streaming; a non-streamed request would buffer."""
        archive = _make_archive({"deck": {"name": "Streaming"}})
        session = _RangeAwareSession(archive, break_on={1: len(archive) // 2})

        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            manifest = {"deck_data": {"path": "deck.json"}, "media": {}}
            _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert all(kw["stream"] is True for kw in session.request_kwargs)

    def test_gzip_deck_resumes_across_compression(self, mw_mock):
        """Resume logic must interoperate with gzip-compressed deck payloads."""
        import gzip as gzip_mod

        deck_payload = {
            "deck": {"name": "GZResumed"},
            "meta": {"padding": "z" * 500},
        }
        compressed = gzip_mod.compress(json.dumps(deck_payload).encode("utf-8"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.json", compressed)
        archive = buf.getvalue()

        session = _RangeAwareSession(archive, break_on={1: len(archive) // 2})

        with patch(
            "import_manager.api_client.session_with_retries", return_value=session
        ):
            manifest = {
                "deck_data": {"path": "deck.json", "compression": "gzip"},
                "media": {},
            }
            sub = _subscription_from_manifest("h1", manifest, "http://x/a.zip")

        assert sub["deck"]["name"] == "GZResumed"
        assert session.range_headers[0] is None
        assert session.range_headers[1] is not None
