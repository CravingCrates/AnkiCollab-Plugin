"""Tests for media_optimizer.py — filename validation, sanitization, optimization.

These verify:
- Filename validation (allowed characters, extensions, length, traversal)
- Filename sanitization (invalid → valid random name)
- Image optimization availability detection
- Edge cases in filename handling
"""

import ast
import base64
import os
from pathlib import Path
import struct
import subprocess
import sys
from unittest.mock import MagicMock, patch
import zlib

import pytest

import media_optimizer
from media_optimizer import (
    ALLOWED_EXTENSIONS,
    FORMAT_TO_EXTENSION,
    OPTIMIZABLE_INPUT_EXTENSIONS,
    _bmp_to_temp_png,
    _get_webp_tool_path,
    _is_animated_gif,
    _is_valid_webp_file,
    can_optimize,
    is_allowed_filename,
    optimize_image,
    optimize_media_file,
    sanitize_filename,
)

# ──────────────────────────────────────────────────────────────────────
# Filename validation
# ──────────────────────────────────────────────────────────────────────


class TestIsAllowedFilename:
    """Validates the safety criteria for filenames matching the Rust backend."""

    # Valid cases
    def test_simple_valid_filename(self):
        assert is_allowed_filename("image_01.png") is True

    def test_valid_with_spaces(self):
        assert is_allowed_filename("my image file.jpg") is True

    def test_valid_with_parentheses(self):
        assert is_allowed_filename("image (1).png") is True

    def test_valid_with_dashes(self):
        assert is_allowed_filename("my-image-file.webp") is True

    def test_valid_audio_extension(self):
        assert is_allowed_filename("audio_01.mp3") is True

    def test_valid_ogg_extension(self):
        assert is_allowed_filename("audio_01.ogg") is True

    def test_valid_underscore_start(self):
        assert is_allowed_filename("_hidden.png") is True

    def test_valid_with_plus(self):
        assert is_allowed_filename("file+name.jpg") is True

    def test_valid_with_comma(self):
        assert is_allowed_filename("file,name.jpg") is True

    def test_valid_with_percent(self):
        assert is_allowed_filename("file%20name.png") is True

    def test_valid_with_ampersand(self):
        assert is_allowed_filename("file&name.png") is True

    # Invalid cases
    def test_empty_filename(self):
        assert is_allowed_filename("") is False

    def test_none_filename(self):
        assert is_allowed_filename(None) is False

    def test_too_short(self):
        assert is_allowed_filename("a.b") is False

    def test_too_long(self):
        assert is_allowed_filename("a" * 252 + ".png") is False

    def test_path_separator_forward_slash(self):
        assert is_allowed_filename("dir/file.png") is False

    def test_path_separator_backslash(self):
        assert is_allowed_filename("dir\\file.png") is False

    def test_path_traversal(self):
        assert is_allowed_filename("..\\..\\etc\\passwd.png") is False

    def test_double_dot(self):
        assert is_allowed_filename("file..png") is False

    def test_trailing_dot(self):
        assert is_allowed_filename("file.png.") is False

    def test_trailing_space(self):
        assert is_allowed_filename("file.png ") is False

    def test_unicode_characters(self):
        assert is_allowed_filename("файл.png") is False

    def test_narrow_no_break_space(self):
        """U+202F should be rejected."""
        assert is_allowed_filename("file\u202fname.png") is False

    def test_starts_with_dot(self):
        assert is_allowed_filename(".hidden.png") is False

    def test_no_alphanumeric(self):
        assert is_allowed_filename("----.png") is False

    def test_whitespace_only(self):
        assert is_allowed_filename("     ") is False


# ──────────────────────────────────────────────────────────────────────
# Filename sanitization
# ──────────────────────────────────────────────────────────────────────


class TestSanitizeFilename:
    def test_valid_filename_unchanged(self):
        assert sanitize_filename("good_file.png") == "good_file.png"

    def test_invalid_gets_randomized(self):
        result = sanitize_filename("файл.png")
        assert result is not None
        assert result.endswith(".png")
        assert result.startswith("img_")

    def test_empty_filename(self):
        assert sanitize_filename("") is False

    def test_none_filename(self):
        assert sanitize_filename(None) is False

    def test_invalid_extension_returns_none(self):
        result = sanitize_filename("file.exe")
        assert result is None

    def test_no_extension_returns_false(self):
        result = sanitize_filename("noextension")
        assert result is False

    def test_preserves_valid_extension(self):
        result = sanitize_filename("bad\u202fname.jpg")
        assert result is not None
        assert result.endswith(".jpg")

    def test_allowed_extensions_list(self):
        """All allowed extensions should pass sanitization."""
        for ext in ALLOWED_EXTENSIONS:
            filename = f"test_file.{ext}"
            result = sanitize_filename(filename)
            assert (
                result is not None and result is not False
            ), f"Extension .{ext} should be allowed but got {result}"


# ──────────────────────────────────────────────────────────────────────
# Optimization availability
# ──────────────────────────────────────────────────────────────────────


class TestOptimizationAvailability:
    def test_can_optimize_returns_bool(self):
        result = can_optimize()
        assert isinstance(result, bool)

    def test_optimizable_extensions_subset(self):
        """All optimizable extensions should be in the full allowed list."""
        for ext in OPTIMIZABLE_INPUT_EXTENSIONS:
            bare = ext.lstrip(".")
            assert (
                bare in ALLOWED_EXTENSIONS
            ), f"Optimizable ext {ext} not in allowed list"

    def test_format_to_extension_mapping(self):
        """All format mappings should produce known extensions."""
        for fmt, ext in FORMAT_TO_EXTENSION.items():
            bare = ext.lstrip(".")
            assert (
                bare in ALLOWED_EXTENSIONS
            ), f"Format {fmt} maps to {ext} which is not allowed"


# ──────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_exactly_5_chars_minimum(self):
        """Minimum length is 5 characters."""
        assert is_allowed_filename("a.png") is True  # 5 chars
        assert is_allowed_filename("abcd") is False  # 4 chars, no ext

    def test_exactly_255_chars_maximum(self):
        """Maximum length is 255 characters."""
        name = "a" * 251 + ".png"  # 255 chars
        assert is_allowed_filename(name) is True

    def test_256_chars_too_long(self):
        name = "a" * 252 + ".png"  # 256 chars
        assert is_allowed_filename(name) is False

    def test_multiple_dots_valid(self):
        """Multiple dots are OK as long as no double-dot."""
        assert is_allowed_filename("my.image.file.png") is True

    def test_webp_not_in_optimizable(self):
        """WebP should NOT be in optimizable (already target format)."""
        assert ".webp" not in OPTIMIZABLE_INPUT_EXTENSIONS


# ──────────────────────────────────────────────────────────────────────
# Helpers for building synthetic image payloads without Pillow
# ──────────────────────────────────────────────────────────────────────


def _make_solid_png(path: Path, width: int, height: int, rgba=(255, 0, 0, 128)) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgba) * width
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(row * height, level=6)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _make_uncompressed_bmp(
    path: Path, width: int, height: int, bpp: int = 24, top_down: bool = False
) -> None:
    row_stride = ((width * bpp + 31) // 32) * 4
    pixel_size = row_stride * height
    dib_height = -height if top_down else height
    header = struct.pack("<2sIHHI", b"BM", 54 + pixel_size, 0, 0, 54)
    dib = struct.pack(
        "<IiiHHIIiiII", 40, width, dib_height, 1, bpp, 0, pixel_size, 2835, 2835, 0, 0
    )
    pixels = bytearray(pixel_size)
    for y in range(height):
        for x in range(width):
            off = y * row_stride + x * (bpp // 8)
            pixels[off : off + 3] = bytes((20, 120, 220))  # BGR
            if bpp == 32:
                pixels[off + 3] = 180
    path.write_bytes(header + dib + bytes(pixels))


def _make_minimal_gif(path: Path, animated: bool = False) -> None:
    if not animated:
        path.write_bytes(
            b"GIF87a\x02\x00\x02\x00\x81\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00,\x00\x00\x00\x00\x02\x00\x02\x00\x00\x08\x06\x00\x01\x08"
            b"\x04\x10\x10\x00;"
        )
        return

    path.write_bytes(
        b"GIF89a\x02\x00\x02\x00\x81\x00\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00!\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00!\xf9\x04\x00\n\x00"
        b"\x00\x00,\x00\x00\x00\x00\x02\x00\x02\x00\x00\x08\x06\x00\x01\x08\x04"
        b"\x10\x10\x00!\xf9\x04\x01\n\x00\x01\x00,\x00\x00\x00\x00\x02\x00\x02\x00"
        b"\x81\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x08\x06\x00\x01\x08"
        b"\x04\x10\x10\x00;"
    )


def _parse_webp_header(data: bytes) -> dict:
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    chunk = data[12:16]
    has_anim = b"ANIM" in data and b"ANMF" in data
    has_alpha = b"ALPH" in data or chunk == b"VP8L"
    if chunk == b"VP8X" and len(data) >= 21:
        flags = data[20]
        has_alpha = bool(flags & 0x10)
        has_anim = bool(flags & 0x02)
    return {"chunk": chunk, "animated": has_anim, "alpha": has_alpha}


# ──────────────────────────────────────────────────────────────────────
# No Pillow dependency & binary resolution
# ──────────────────────────────────────────────────────────────────────


class TestNoPillowDependency:
    def test_media_optimizer_has_no_pil_imports(self):
        src = Path(media_optimizer.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("PIL")
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("PIL")


class TestWebpBinaryResolution:
    @pytest.mark.parametrize(
        "plat,machine,expected_suffix",
        [
            ("win32", "AMD64", os.path.join("windows", "x86_64", "cwebp.exe")),
            ("win32", "ARM64", os.path.join("windows", "x86_64", "cwebp.exe")),
            ("linux", "x86_64", os.path.join("linux", "x86_64", "cwebp")),
            ("linux", "aarch64", os.path.join("linux", "aarch64", "cwebp")),
            ("darwin", "arm64", os.path.join("macos", "arm64", "cwebp")),
            ("darwin", "x86_64", os.path.join("macos", "x86_64", "cwebp")),
        ],
    )
    def test_resolves_platform_binary(self, plat, machine, expected_suffix):
        with (
            patch.object(media_optimizer.sys, "platform", plat),
            patch.object(media_optimizer.platform, "machine", return_value=machine),
            patch.object(Path, "is_file", return_value=True),
            patch.object(media_optimizer.os, "access", return_value=True),
        ):
            resolved = _get_webp_tool_path("cwebp")
            assert resolved is not None
            assert str(resolved).endswith(expected_suffix)

    def test_unix_sets_executable_bit_when_missing(self):
        with (
            patch.object(media_optimizer.sys, "platform", "darwin"),
            patch.object(media_optimizer.platform, "machine", return_value="arm64"),
            patch.object(Path, "is_file", return_value=True),
            patch.object(media_optimizer.os, "access", return_value=False),
            patch.object(Path, "stat", return_value=MagicMock(st_mode=0o100644)),
            patch.object(media_optimizer.os, "chmod") as mock_chmod,
        ):
            resolved = _get_webp_tool_path("cwebp")
            assert resolved is not None
            mock_chmod.assert_called_once()
            assert mock_chmod.call_args[0][1] & 0o111

    def test_unknown_arch_falls_back_to_system_which(self):
        with (
            patch.object(media_optimizer.sys, "platform", "linux"),
            patch.object(media_optimizer.platform, "machine", return_value="riscv64"),
            patch.object(
                media_optimizer.shutil, "which", return_value="/usr/bin/cwebp"
            ),
        ):
            resolved = _get_webp_tool_path("cwebp")
            assert resolved == Path("/usr/bin/cwebp")


# ──────────────────────────────────────────────────────────────────────
# Real vendored libwebp conversion tests
# ──────────────────────────────────────────────────────────────────────


class TestRealWebpConversion:
    @pytest.fixture(autouse=True)
    def _require_vendored_tools(self):
        if (
            _get_webp_tool_path("cwebp") is None
            or _get_webp_tool_path("gif2webp") is None
        ):
            pytest.skip("Vendored cwebp/gif2webp binaries not installed in dist/webp/")

    def test_png_with_alpha_converts_to_webp(self, tmp_path):
        src = tmp_path / "alpha.png"
        dst = tmp_path / "alpha.webp"
        _make_solid_png(src, 64, 64, rgba=(255, 0, 0, 128))

        out_path, converted = optimize_image(src, dst, convert_to_webp=True)
        assert converted is True
        assert out_path == dst
        assert _is_valid_webp_file(dst)
        info = _parse_webp_header(dst.read_bytes())
        assert info["alpha"] is True

    def test_jpeg_and_tiff_convert_to_webp(self, tmp_path):
        jpg = tmp_path / "photo.jpg"
        tif = tmp_path / "scan.tif"
        jpg.write_bytes(
            base64.b64decode(
                "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8S"
                "EhEPERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEU"
                "Hh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAAR"
                "CAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
                "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
                "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG"
                "h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl"
                "5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
                "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYk"
                "NOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
                "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
                "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDjKKKK/Xj8cP/Z"
            )
        )
        tif.write_bytes(
            b"II*\x00\x08\x00\x00\x00\x0b\x00\x00\x01\x04\x00\x01\x00\x00\x00\x02\x00"
            b"\x00\x00\x01\x01\x04\x00\x01\x00\x00\x00\x02\x00\x00\x00\x02\x01\x03\x00"
            b"\x04\x00\x00\x00\x92\x00\x00\x00\x03\x01\x03\x00\x01\x00\x00\x00\x01\x00"
            b"\x00\x00\x06\x01\x03\x00\x01\x00\x00\x00\x02\x00\x00\x00\x11\x01\x04\x00"
            b"\x01\x00\x00\x00\x9a\x00\x00\x00\x15\x01\x03\x00\x01\x00\x00\x00\x04\x00"
            b"\x00\x00\x16\x01\x04\x00\x01\x00\x00\x00\x02\x00\x00\x00\x17\x01\x04\x00"
            b"\x01\x00\x00\x00\x10\x00\x00\x00\x1c\x01\x03\x00\x01\x00\x00\x00\x01\x00"
            b"\x00\x00R\x01\x03\x00\x01\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00\x08"
            b"\x00\x08\x00\x08\x00\x08\x00\nx\xc8\x80\nx\xc8\x80\nx\xc8\x80\nx\xc8\x80"
        )

        for src in (jpg, tif):
            dst = src.with_suffix(".webp")
            out_path, converted = optimize_image(src, dst, convert_to_webp=True)
            assert converted is True
            assert out_path == dst
            assert _is_valid_webp_file(dst)

    def test_bmp_converts_to_webp_windows_and_bridge(self, tmp_path):
        bmp = tmp_path / "image.bmp"
        _make_uncompressed_bmp(bmp, 32, 32, bpp=24)

        dst1 = tmp_path / "image_win.webp"
        out1, ok1 = optimize_image(bmp, dst1, convert_to_webp=True)
        assert ok1 is True and out1 == dst1 and _is_valid_webp_file(dst1)

        # Force POSIX BMP->PNG bridge path cleanly across all host OSes
        real_cwebp = _get_webp_tool_path("cwebp")
        dst2 = tmp_path / "image_bridge.webp"
        with (
            patch.object(media_optimizer.sys, "platform", "linux"),
            patch.object(
                media_optimizer,
                "_get_webp_tool_path",
                return_value=real_cwebp,
            ),
        ):
            out2, ok2 = optimize_image(bmp, dst2, convert_to_webp=True)
        assert ok2 is True and out2 == dst2 and _is_valid_webp_file(dst2)
        assert not any(p.name.endswith(".png") for p in tmp_path.iterdir())

    def test_static_and_animated_gif_convert_to_webp(self, tmp_path):
        static_gif = tmp_path / "static.gif"
        anim_gif = tmp_path / "anim.gif"
        _make_minimal_gif(static_gif, animated=False)
        _make_minimal_gif(anim_gif, animated=True)

        assert _is_animated_gif(static_gif) is False
        assert _is_animated_gif(anim_gif) is True

        static_dst = tmp_path / "static.webp"
        out_s, ok_s = optimize_image(static_gif, static_dst, convert_to_webp=True)
        assert ok_s is True and out_s == static_dst and _is_valid_webp_file(static_dst)
        assert _parse_webp_header(static_dst.read_bytes())["animated"] is False

        anim_dst = tmp_path / "anim.webp"
        out_a, ok_a = optimize_image(anim_gif, anim_dst, convert_to_webp=True)
        assert ok_a is True and out_a == anim_dst and _is_valid_webp_file(anim_dst)
        assert _parse_webp_header(anim_dst.read_bytes())["animated"] is True

    def test_unicode_spaces_and_leading_dash_paths(self, tmp_path):
        sub = tmp_path / "Üser Space Dir"
        sub.mkdir()
        spaced = sub / "my image (1).png"
        dashed = sub / "-leading-dash.png"
        _make_solid_png(spaced, 32, 32)
        _make_solid_png(dashed, 32, 32)

        for src in (spaced, dashed):
            dst = src.with_suffix(".webp")
            out_path, converted = optimize_image(src, dst, convert_to_webp=True)
            assert converted is True
            assert out_path == dst
            assert _is_valid_webp_file(dst)

    def test_misnamed_gif_and_already_webp_bytes(self, tmp_path):
        misnamed_gif = tmp_path / "actually_gif.png"
        _make_minimal_gif(misnamed_gif, animated=True)
        gif_dst = tmp_path / "actually_gif.webp"
        out_g, ok_g = optimize_image(misnamed_gif, gif_dst, convert_to_webp=True)
        assert ok_g is True and out_g == gif_dst
        assert _parse_webp_header(gif_dst.read_bytes())["animated"] is True

        # Already-WebP file misnamed as .jpg should copy cleanly without re-encoding
        fake_jpg = tmp_path / "already.jpg"
        fake_jpg.write_bytes(gif_dst.read_bytes())
        webp_dst = tmp_path / "already.webp"
        out_w, ok_w = optimize_image(fake_jpg, webp_dst, convert_to_webp=True)
        assert ok_w is True and out_w == webp_dst
        assert webp_dst.read_bytes() == fake_jpg.read_bytes()


# ──────────────────────────────────────────────────────────────────────
# BMP -> PNG pure-Python bridge & failure/safety paths
# ──────────────────────────────────────────────────────────────────────


class TestBmpToPngBridge:
    @pytest.mark.parametrize(
        "bpp,top_down",
        [(24, False), (32, False), (24, True), (32, True)],
    )
    def test_pure_python_bmp_to_png_produces_valid_png(self, tmp_path, bpp, top_down):
        bmp = tmp_path / f"test_{bpp}_{top_down}.bmp"
        png = tmp_path / f"test_{bpp}_{top_down}.png"
        _make_uncompressed_bmp(bmp, 17, 13, bpp=bpp, top_down=top_down)

        with patch.object(
            media_optimizer,
            "_get_real_qt_image_classes",
            return_value=(None, None, None),
        ):
            assert _bmp_to_temp_png(bmp, png) is True

        data = png.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")

        cwebp = _get_webp_tool_path("cwebp")
        if cwebp is not None:
            webp = tmp_path / f"test_{bpp}_{top_down}.webp"
            out_path, ok = optimize_image(png, webp, convert_to_webp=True)
            assert ok is True and out_path == webp and _is_valid_webp_file(webp)


class TestFailureAndSafetyPaths:
    def test_corrupt_image_leaves_no_temp_or_destination(self, tmp_path):
        bad = tmp_path / "corrupt.jpg"
        bad.write_bytes(b"definitely not a valid image")
        dst = tmp_path / "corrupt.webp"

        out_path, ok = optimize_image(bad, dst, convert_to_webp=True)
        assert ok is False
        assert out_path == bad
        assert not dst.exists()
        assert [p.name for p in tmp_path.iterdir()] == ["corrupt.jpg"]

    def test_missing_source_none_dest_and_unsupported_ext(self, tmp_path):
        missing = tmp_path / "missing.png"
        assert optimize_image(missing, tmp_path / "out.webp") == (missing, False)

        real_png = tmp_path / "real.png"
        _make_solid_png(real_png, 8, 8)
        assert optimize_image(real_png, None) == (real_png, False)

        mp3 = tmp_path / "sound.mp3"
        mp3.write_bytes(b"ID3")
        assert optimize_image(mp3, tmp_path / "sound.webp") == (mp3, False)

    def test_timeout_cleans_up_temp_files(self, tmp_path):
        src = tmp_path / "slow.png"
        dst = tmp_path / "slow.webp"
        _make_solid_png(src, 16, 16)

        with (
            patch.object(
                media_optimizer.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="cwebp", timeout=30),
            ),
            patch.object(media_optimizer, "_convert_with_qt", return_value=False),
        ):
            out_path, ok = optimize_image(src, dst, convert_to_webp=True)

        assert ok is False
        assert out_path == src
        assert not dst.exists()
        assert [p.name for p in tmp_path.iterdir()] == ["slow.png"]

    def test_animated_gif_never_flattened_by_qt_fallback(self, tmp_path):
        anim = tmp_path / "anim.gif"
        dst = tmp_path / "anim.webp"
        _make_minimal_gif(anim, animated=True)

        with (
            patch.object(media_optimizer, "_get_webp_tool_path", return_value=None),
            patch.object(media_optimizer, "_convert_with_qt") as mock_qt,
        ):
            out_path, ok = optimize_image(anim, dst, convert_to_webp=True)

        assert ok is False
        assert out_path == anim
        mock_qt.assert_not_called()

    def test_disabled_optimization_flag_skips(self, tmp_path):
        src = tmp_path / "img.png"
        dst = tmp_path / "img.webp"
        _make_solid_png(src, 8, 8)

        with patch.object(media_optimizer, "OPTIMIZATION_AVAILABLE", False):
            assert can_optimize() is False
            assert optimize_image(src, dst) == (src, False)


# ──────────────────────────────────────────────────────────────────────
# Async optimize_media_file workflow
# ──────────────────────────────────────────────────────────────────────


class TestOptimizeMediaFileAsync:
    async def test_static_prefix_and_small_and_webp_skipped(self, tmp_path):
        static_file = tmp_path / "_font.png"
        _make_solid_png(static_file, 16, 16)
        assert await optimize_media_file("_font.png", static_file) == (
            str(static_file),
            "_font.png",
            False,
        )

        small = tmp_path / "small.png"
        _make_solid_png(small, 16, 16)
        assert await optimize_media_file("small.png", small) == (
            str(small),
            "small.png",
            False,
        )

        existing_webp = tmp_path / "done.webp"
        existing_webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
        assert await optimize_media_file("done.webp", existing_webp) == (
            str(existing_webp),
            "done.webp",
            False,
        )

    async def test_large_png_and_invalid_filename_converted(self, tmp_path):
        if _get_webp_tool_path("cwebp") is None:
            pytest.skip("Vendored cwebp binary not installed in dist/webp/")

        # Uncompressible (>100KB) noisy RGBA PNG
        w, h = 256, 256
        raw_rows = bytearray()
        noise = os.urandom(w * 4)
        for _ in range(h):
            raw_rows.append(0)
            raw_rows.extend(noise)
        # Make each row distinct so zlib keeps > 100 KB
        distinct_rows = bytearray()
        for y in range(h):
            distinct_rows.append(0)
            distinct_rows.extend(os.urandom(w * 4))

        def _write_large_png(p: Path):
            def chunk(tag: bytes, data: bytes) -> bytes:
                crc = zlib.crc32(tag + data) & 0xFFFFFFFF
                return (
                    struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)
                )

            ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
            idat = zlib.compress(bytes(distinct_rows), level=1)
            p.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", idat)
                + chunk(b"IEND", b"")
            )
            assert p.stat().st_size >= 100 * 1024

        large = tmp_path / "large.png"
        _write_large_png(large)
        out_path, out_name, did_opt = await optimize_media_file("large.png", large)
        assert did_opt is True
        assert out_name == "large.webp"
        assert _is_valid_webp_file(Path(out_path))

        bad_name = "файл_large.png"
        bad_path = tmp_path / bad_name
        _write_large_png(bad_path)
        out_p2, out_n2, did_opt2 = await optimize_media_file(bad_name, bad_path)
        assert did_opt2 is True
        assert out_n2.startswith("img_") and out_n2.endswith(".webp")
        assert _is_valid_webp_file(Path(out_p2))
