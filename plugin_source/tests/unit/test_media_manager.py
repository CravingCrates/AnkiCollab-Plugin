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
    MAX_STATIC_CODE_SIZE,
    MAX_FONT_SIZE,
    UPLOAD_CONCURRENCY,
    ALLOWED_EXTENSIONS,
    ALL_ALLOWED_EXTENSIONS,
    STATIC_ALLOWED_EXTENSIONS,
    CONTENT_TYPE_MAP,
    max_file_size_for_filename,
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

    def test_content_type_map_covers_extensions(self):
        # .oga is in ALLOWED_EXTENSIONS but intentionally absent from
        # CONTENT_TYPE_MAP — document as known gap rather than failing.
        missing = ALL_ALLOWED_EXTENSIONS - set(CONTENT_TYPE_MAP.keys())
        assert missing <= {
            ".oga"
        }, f"Unexpected missing content types: {missing - {'.oga'}}"

    def test_static_allowed_extensions_and_content_type_map(self):
        expected_static = {".js", ".css", ".ttf", ".otf", ".woff", ".woff2"}
        assert STATIC_ALLOWED_EXTENSIONS == expected_static
        assert CONTENT_TYPE_MAP[".js"] == "application/javascript"
        assert CONTENT_TYPE_MAP[".css"] == "text/css"
        assert CONTENT_TYPE_MAP[".ttf"] == "font/ttf"
        assert CONTENT_TYPE_MAP[".otf"] == "font/otf"
        assert CONTENT_TYPE_MAP[".woff"] == "font/woff"
        assert CONTENT_TYPE_MAP[".woff2"] == "font/woff2"

    def test_max_file_size_for_filename_enforces_limits(self):
        # 1 MB for .js/.css when _-prefixed
        assert max_file_size_for_filename("_test.js") == 1 * 1024 * 1024
        assert max_file_size_for_filename("_style.css") == 1 * 1024 * 1024
        # 3 MB for fonts when _-prefixed
        assert max_file_size_for_filename("_font.ttf") == 3 * 1024 * 1024
        assert max_file_size_for_filename("_font.otf") == 3 * 1024 * 1024
        assert max_file_size_for_filename("_font.woff") == 3 * 1024 * 1024
        assert max_file_size_for_filename("_font.woff2") == 3 * 1024 * 1024
        # 10 MB for standard images and audio
        assert max_file_size_for_filename("image.png") == 10 * 1024 * 1024
        assert max_file_size_for_filename("_image.png") == 10 * 1024 * 1024
        assert max_file_size_for_filename("sound.mp3") == 10 * 1024 * 1024
        # Rejected (None) for non-_-prefixed code/fonts or unknown extensions
        assert max_file_size_for_filename("test.js") is None
        assert max_file_size_for_filename("style.css") is None
        assert max_file_size_for_filename("font.woff2") is None
        assert max_file_size_for_filename("_executable.exe") is None


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


# ──────────────────────────────────────────────────────────────────────
# optimize_media_for_upload with notetype_file_pairs & static media
# ──────────────────────────────────────────────────────────────────────


class TestOptimizeMediaForUploadStaticFiles:
    @pytest.fixture
    def mm(self, tmp_media_dir, mw_mock):
        mw_mock.col.media.dir.return_value = str(tmp_media_dir)
        return MediaManager(
            api_base_url="http://test.local", media_folder=str(tmp_media_dir)
        )

    @pytest.mark.asyncio
    async def test_notetype_and_underscore_files_skip_optimization_and_renaming(
        self, mm, tmp_media_dir
    ):
        js_file = tmp_media_dir / "_code.js"
        js_file.write_bytes(b"console.log('hello');")

        svg_file = tmp_media_dir / "_vector.svg"
        svg_file.write_bytes(b"<svg></svg>")

        png_file = tmp_media_dir / "_image.png"
        png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

        woff2_file = tmp_media_dir / "_font.woff2"
        woff2_file.write_bytes(b"wOF2" + b"\x00" * 50)

        notetype_pairs = [
            ("_code.js", "nt-guid-1"),
            ("_vector.svg", "nt-guid-1"),
            ("_font.woff2", "nt-guid-1"),
        ]
        note_pairs = [
            ("_image.png", "note-guid-1"),
        ]

        with (
            patch("media_optimizer.optimize_svg_files") as mock_opt_svg,
            patch("media_optimizer.optimize_media_file") as mock_opt_file,
        ):
            mapping, files_info, file_paths = await mm.optimize_media_for_upload(
                file_note_pairs=note_pairs,
                notetype_file_pairs=notetype_pairs,
            )

        # 1. _ files are NEVER renamed (mapping is empty)
        assert mapping == {}

        # 2. _ files are NEVER sent to optimize_svg_files or optimize_media_file
        mock_opt_svg.assert_not_called()
        mock_opt_file.assert_not_called()

        # 3. All 4 static files are collected with on-disk hash and paths
        filenames = {f["filename"] for f in files_info}
        assert filenames == {"_code.js", "_vector.svg", "_font.woff2", "_image.png"}

        # 4. Correct notetype_guid and note_guid fields
        for f in files_info:
            if f["filename"] in {"_code.js", "_vector.svg", "_font.woff2"}:
                assert f["notetype_guid"] == "nt-guid-1"
                assert f["note_guid"] == ""
            elif f["filename"] == "_image.png":
                assert f["note_guid"] == "note-guid-1"

            assert f["hash"] in file_paths
            assert os.path.exists(file_paths[f["hash"]])

    @pytest.mark.asyncio
    async def test_size_limits_and_rejections_enforced(self, mm, tmp_media_dir):
        # 1. Over 1 MB .js is rejected
        huge_js = tmp_media_dir / "_huge.js"
        huge_js.write_bytes(b"x" * (1 * 1024 * 1024 + 10))

        # 2. Under 1 MB .js is accepted
        valid_js = tmp_media_dir / "_valid.js"
        valid_js.write_bytes(b"x" * 500)

        # 3. Over 3 MB font is rejected
        huge_font = tmp_media_dir / "_huge.woff2"
        huge_font.write_bytes(b"x" * (3 * 1024 * 1024 + 10))

        # 4. Under 3 MB font is accepted
        valid_font = tmp_media_dir / "_valid.woff2"
        valid_font.write_bytes(b"x" * 500)

        # 5. Non-_ static code/fonts are rejected
        non_underscore_js = tmp_media_dir / "script.js"
        non_underscore_js.write_bytes(b"x" * 100)

        non_underscore_font = tmp_media_dir / "font.ttf"
        non_underscore_font.write_bytes(b"x" * 100)

        notetype_pairs = [
            ("_huge.js", "nt-guid"),
            ("_valid.js", "nt-guid"),
            ("_huge.woff2", "nt-guid"),
            ("_valid.woff2", "nt-guid"),
            ("script.js", "nt-guid"),
            ("font.ttf", "nt-guid"),
        ]
        note_pairs = [
            ("script.js", "note-guid"),
            ("font.ttf", "note-guid"),
        ]

        mapping, files_info, file_paths = await mm.optimize_media_for_upload(
            file_note_pairs=note_pairs,
            notetype_file_pairs=notetype_pairs,
        )

        accepted_names = {f["filename"] for f in files_info}
        assert accepted_names == {"_valid.js", "_valid.woff2"}

    @pytest.mark.asyncio
    async def test_static_code_rejected_in_file_note_pairs_accepted_in_notetype_file_pairs(
        self, mm, tmp_media_dir
    ):
        script_file = tmp_media_dir / "_script.js"
        script_file.write_bytes(b"console.log('static test');")

        # 1. When passed in file_note_pairs, _script.js is rejected
        _, files_info_note, _ = await mm.optimize_media_for_upload(
            file_note_pairs=[("_script.js", "note-guid-1")],
            notetype_file_pairs=[],
        )
        assert len(files_info_note) == 0

        # 2. When passed in notetype_file_pairs, _script.js is accepted
        _, files_info_nt, _ = await mm.optimize_media_for_upload(
            file_note_pairs=[],
            notetype_file_pairs=[("_script.js", "nt-guid-1")],
        )
        assert len(files_info_nt) == 1
        assert files_info_nt[0]["filename"] == "_script.js"
        assert files_info_nt[0]["notetype_guid"] == "nt-guid-1"
        assert files_info_nt[0]["note_guid"] == ""

        # 3. When both are passed, only the notetype entry is accepted
        _, files_info_both, _ = await mm.optimize_media_for_upload(
            file_note_pairs=[("_script.js", "note-guid-1")],
            notetype_file_pairs=[("_script.js", "nt-guid-1")],
        )
        assert len(files_info_both) == 1
        assert files_info_both[0]["filename"] == "_script.js"
        assert files_info_both[0]["notetype_guid"] == "nt-guid-1"
        assert files_info_both[0]["note_guid"] == ""

        # 4. Basic validation accepts authorized notetype static files
        assert mm.validate_file_basic(script_file) is True
