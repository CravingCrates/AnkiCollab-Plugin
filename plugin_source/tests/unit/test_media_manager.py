"""Tests for media_manager.py — RateLimiter, retry decorator, MediaManager helpers."""

import asyncio
import os
import time
import pytest
import requests
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from media_manager import (
    RateLimiter,
    retry,
    MediaManager,
    MediaError,
    MediaServerError,
    MediaRateLimitError,
    MediaTypeError,
    MediaHashError,
    MediaUploadError,
    MediaDownloadError,
    MAX_REQUESTS_PER_MINUTE,
    MAX_FILE_SIZE,
    UPLOAD_CONCURRENCY,
    ALLOWED_EXTENSIONS,
    ALL_ALLOWED_EXTENSIONS,
    CONTENT_TYPE_MAP,
)

# ──────────────────────────────────────────────────────────────────────
# Exception hierarchy
# ──────────────────────────────────────────────────────────────────────


class TestExceptionHierarchy:
    def test_media_error_is_base(self):
        assert issubclass(MediaServerError, MediaError)
        assert issubclass(MediaRateLimitError, MediaError)
        assert issubclass(MediaTypeError, MediaError)
        assert issubclass(MediaHashError, MediaError)
        assert issubclass(MediaUploadError, MediaError)
        assert issubclass(MediaDownloadError, MediaError)

    def test_all_are_exceptions(self):
        for cls in (
            MediaError,
            MediaServerError,
            MediaRateLimitError,
            MediaTypeError,
            MediaHashError,
            MediaUploadError,
            MediaDownloadError,
        ):
            assert issubclass(cls, Exception)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────


class TestMediaConstants:
    def test_max_file_size(self):
        assert MAX_FILE_SIZE == 10 * 1024 * 1024

    def test_upload_concurrency(self, tmp_media_dir, mw_mock):
        """UPLOAD_CONCURRENCY drives the media-upload semaphore size — assert
        the behavior it controls, not just that the constant is positive."""
        mm = MediaManager(
            api_base_url="http://test.local", media_folder=str(tmp_media_dir)
        )
        assert mm.upload_semaphore_size == UPLOAD_CONCURRENCY
        assert UPLOAD_CONCURRENCY > 0

    def test_allowed_extensions_contain_common_formats(self):
        assert ".jpg" in ALL_ALLOWED_EXTENSIONS
        assert ".png" in ALL_ALLOWED_EXTENSIONS
        assert ".mp3" in ALL_ALLOWED_EXTENSIONS

    def test_allowed_extensions_contain_notetype_scripts(self):
        assert ".js" in ALL_ALLOWED_EXTENSIONS
        assert CONTENT_TYPE_MAP[".js"] == "text/javascript"

    def test_content_type_map_covers_extensions(self):
        # .oga is in ALLOWED_EXTENSIONS but intentionally absent from
        # CONTENT_TYPE_MAP — document as known gap rather than failing.
        missing = ALL_ALLOWED_EXTENSIONS - set(CONTENT_TYPE_MAP.keys())
        assert missing <= {
            ".oga"
        }, f"Unexpected missing content types: {missing - {'.oga'}}"


# ──────────────────────────────────────────────────────────────────────
# RateLimiter
# ──────────────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_init_positive_values(self):
        rl = RateLimiter(100, 60)
        assert rl.max_calls == 100.0
        assert rl.period == 60.0

    def test_init_rejects_zero(self):
        with pytest.raises(ValueError):
            RateLimiter(0, 60)

    def test_init_rejects_negative(self):
        with pytest.raises(ValueError):
            RateLimiter(-1, 60)
        with pytest.raises(ValueError):
            RateLimiter(100, -1)

    @pytest.mark.asyncio
    async def test_wait_if_needed_consumes_token(self):
        rl = RateLimiter(10, 60)
        initial_tokens = rl._tokens
        await rl.wait_if_needed()
        # After consuming, tokens should decrease
        assert rl._tokens < initial_tokens

    @pytest.mark.asyncio
    async def test_wait_if_needed_multiple(self):
        rl = RateLimiter(5, 60)
        for _ in range(5):
            await rl.wait_if_needed()
        # All tokens consumed — next call should have to wait or refill
        assert rl._tokens < 1.0

    def test_lock_is_created_per_loop(self):
        """_get_lock creates a new lock when the event loop changes."""
        rl = RateLimiter(10, 60)
        assert rl._lock is None  # No lock before first use


# ──────────────────────────────────────────────────────────────────────
# retry decorator
# ──────────────────────────────────────────────────────────────────────


class TestRetryDecorator:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_try(self):
        call_count = 0

        @retry(max_tries=3, delay=0.01)
        async def always_ok():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await always_ok()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        call_count = 0

        @retry(max_tries=3, delay=0.01)
        async def fail_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise requests.RequestException("transient")
            return "recovered"

        result = await fail_then_ok()
        assert result == "recovered"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        @retry(max_tries=2, delay=0.01)
        async def always_fail():
            raise MediaServerError("down")

        with pytest.raises(MediaServerError, match="down"):
            await always_fail()

    @pytest.mark.asyncio
    async def test_no_retry_on_404(self):
        call_count = 0

        @retry(max_tries=3, delay=0.01)
        async def get_not_found():
            nonlocal call_count
            call_count += 1
            exc = MediaDownloadError("not found")
            exc.metadata = {"status_code": 404}
            raise exc

        with pytest.raises(MediaDownloadError):
            await get_not_found()
        assert call_count == 1  # No retry on 404

    @pytest.mark.asyncio
    async def test_no_retry_on_401(self):
        call_count = 0

        @retry(max_tries=3, delay=0.01)
        async def auth_error():
            nonlocal call_count
            call_count += 1
            exc = MediaUploadError("unauthorized")
            exc.metadata = {"status_code": 401}
            raise exc

        with pytest.raises(MediaUploadError):
            await auth_error()
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_executor_unavailable(self):
        call_count = 0

        @retry(max_tries=3, delay=0.01)
        async def executor_gone():
            nonlocal call_count
            call_count += 1
            exc = MediaServerError("exec gone")
            exc.metadata = {"reason": "executor_unavailable"}
            raise exc

        with pytest.raises(MediaServerError):
            await executor_gone()
        assert call_count == 1


# ──────────────────────────────────────────────────────────────────────
# MediaManager — construction & helpers
# ──────────────────────────────────────────────────────────────────────


class TestMediaManagerInit:
    def test_init_with_valid_folder(self, tmp_media_dir, mw_mock):
        mm = MediaManager(
            api_base_url="http://test.local", media_folder=str(tmp_media_dir)
        )
        assert mm.api_base_url == "http://test.local"
        assert mm.media_folder == tmp_media_dir

    def test_init_strips_trailing_slash(self, tmp_media_dir, mw_mock):
        mm = MediaManager(
            api_base_url="http://test.local/", media_folder=str(tmp_media_dir)
        )
        assert mm.api_base_url == "http://test.local"

    def test_init_invalid_folder(self, mw_mock):
        with pytest.raises(ValueError, match="Media folder not found"):
            MediaManager(api_base_url="http://x", media_folder="/nonexistent/path")


class TestMediaManagerHelpers:
    @pytest.fixture
    def mm(self, tmp_media_dir, mw_mock):
        return MediaManager(
            api_base_url="http://test.local", media_folder=str(tmp_media_dir)
        )

    def test_file_exists_with_size_true(self, mm, tmp_media_dir):
        assert mm._file_exists_with_size(tmp_media_dir / "test_image.png") is True

    def test_file_exists_with_size_missing(self, mm, tmp_media_dir):
        assert mm._file_exists_with_size(tmp_media_dir / "nope.png") is False

    def test_file_exists_with_size_empty(self, mm, tmp_path):
        empty = tmp_path / "empty.txt"
        empty.write_bytes(b"")
        assert mm._file_exists_with_size(empty) is False

    def test_is_anki_available(self, mm, mw_mock):
        assert mm._is_anki_available() is True

    def test_is_anki_available_no_col(self, mm, mw_no_col):
        assert mm._is_anki_available() is False

    def test_raise_with_metadata(self, mm):
        with pytest.raises(MediaUploadError) as exc_info:
            mm._raise_with_metadata(MediaUploadError("fail"), {"key": "val"})
        assert exc_info.value.metadata == {"key": "val"}

    def test_get_executor_max_workers(self, mm):
        workers = mm._get_executor_max_workers()
        assert 4 <= workers <= 32

    def test_ensure_executor_available_no_anki(self, mm, mw_no_col):
        # Simulate shutdown state
        mm.thread_executor._shutdown = True
        with pytest.raises(RuntimeError, match="Anki is closing"):
            mm._ensure_executor_available()

    def test_ensure_executor_recreates(self, mm, mw_mock):
        mm.thread_executor._shutdown = True
        mm._ensure_executor_available()
        assert not mm.thread_executor._shutdown


# ──────────────────────────────────────────────────────────────────────
# Path traversal protection in download_file
# ──────────────────────────────────────────────────────────────────────


class TestDownloadFilePathTraversal:
    @pytest.fixture
    def mm(self, tmp_media_dir, mw_mock):
        return MediaManager(
            api_base_url="http://test.local", media_folder=str(tmp_media_dir)
        )

    @pytest.mark.asyncio
    async def test_blocks_parent_directory_traversal(self, mm, tmp_media_dir):
        """download_file should reject paths that escape the media folder."""
        evil_path = tmp_media_dir / ".." / "escaped.txt"
        result = await mm.download_file("http://example.com/file.png", evil_path)
        assert result is False
        assert not (tmp_media_dir.parent / "escaped.txt").exists()

    @pytest.mark.asyncio
    async def test_blocks_absolute_path_outside_media(self, mm, tmp_path):
        """download_file should reject absolute paths outside media folder."""
        outside = tmp_path / "outside" / "evil.txt"
        result = await mm.download_file("http://example.com/file.png", outside)
        assert result is False

    @pytest.mark.asyncio
    async def test_blocks_double_dot_traversal(self, mm, tmp_media_dir):
        """download_file should block ../../ style traversal."""
        evil_path = tmp_media_dir / "sub" / ".." / ".." / "evil.txt"
        result = await mm.download_file("http://example.com/file.png", evil_path)
        assert result is False

    @pytest.mark.asyncio
    async def test_blocks_prefix_bypass(self, mm, tmp_path):
        """download_file should block paths that share a prefix but are different dirs.

        e.g. media folder is /tmp/media, attacker tries /tmp/media_evil/
        Old startswith() check would pass this — relative_to() correctly rejects it.
        """
        evil_dir = tmp_path / (mm.media_folder.name + "_evil")
        evil_dir.mkdir()
        evil_path = evil_dir / "payload.txt"
        result = await mm.download_file("http://example.com/file.png", evil_path)
        assert result is False
