import asyncio
import hashlib
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
import uuid
import zlib
from pathlib import Path
from typing import Optional

from aqt import mw

from .utils import get_logger

logger = get_logger("ankicollab.media_optimizer")

WEBP_TOOL_TIMEOUT_SECONDS = 30
_CONVERSION_SEMAPHORE = threading.Semaphore(min(4, os.cpu_count() or 2))

# Configuration
WEBP_QUALITY = 85
JPEG_QUALITY = 85
PNG_COMPRESSION = 9

OPTIMIZABLE_INPUT_EXTENSIONS = [
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    # WebP excluded - already in target format, no re-optimization needed
    # SVG handled separately, audio files not optimized
]

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_STATIC_CODE_SIZE = 1 * 1024 * 1024  # 1 MB
MAX_FONT_SIZE = 3 * 1024 * 1024  # 3 MB

STATIC_ALLOWED_EXTENSIONS = {".js", ".css", ".ttf", ".otf", ".woff", ".woff2"}
STATIC_CODE_EXTENSIONS = {".js", ".css"}
STATIC_FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}

CONTENT_TYPE_MAP = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".js": "application/javascript",
    ".css": "text/css",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

ALLOWED_EXTENSIONS = [
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "svg",
    "bmp",
    "tif",
    "tiff",
    "mp3",
    "ogg",
]

FORMAT_TO_EXTENSION = {  # the bugfix we use this for does not apply to audio files so we don't need to include them
    "JPEG": ".jpg",  # Standardize to .jpg
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tif",  # Standardize to .tif
}


def is_allowed_filename(filename):
    """
    Check if a filename meets the required safety criteria.
    Implements the same logic as the Rust function is_allowed_extension.

    Args:
        filename: The filename to check

    Returns:
        bool: True if the filename is allowed, False otherwise
    """
    # Check length
    if not filename or len(filename) > 255 or len(filename) < 5:
        return False

    # Check for path separators or traversal patterns
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or filename.endswith(".")
        or filename.endswith(" ")
    ):
        return False

    # Check for empty after trimming
    if not filename.strip():
        return False

    # Check for valid characters (ASCII only)
    # Replace \s with a literal space to avoid matching unicode whitespace like U+202F
    allowed_chars = re.compile(r"^[a-zA-Z0-9\.\-_ \(\)\+\,\%\&]*$")
    if not allowed_chars.match(filename):
        # Additionally check if all characters are within the basic ASCII range
        # This catches characters not explicitly listed but still potentially problematic
        if not all(ord(c) < 128 for c in filename):
            return False
        # If the regex failed but all chars are ASCII, it means an unlisted but valid ASCII char was used.
        # This case *shouldn't* happen with the current regex, but added as a safeguard.
        # If it *does* fail the regex but *is* all ASCII, we still reject it based on the regex.
        return False

    # Check first character is alphanumeric or underscore
    if not (filename[0].isalnum() or filename[0] == "_"):
        return False

    # Check contains at least one alphanumeric
    if not any(c.isalnum() for c in filename):
        return False

    return True


def sanitize_filename(filename):
    """
    Sanitize a filename, replacing invalid filenames with random valid ones
    while preserving the extension if it's allowed.

    Args:
        filename: The original filename

    Returns:
        str: A valid filename (either the original or sanitized version)
    """
    # Extract the extension
    if not filename:
        return False

    path = Path(filename)
    suffix = path.suffix.lower()
    ext = suffix.lstrip(".")

    if not ext:
        return False

    # Check if extension is valid (standard media or static media)
    is_static_ext = suffix in STATIC_ALLOWED_EXTENSIONS
    if ext not in ALLOWED_EXTENSIONS and not is_static_ext:
        return None  # Invalid extension, can't sanitize

    # Files starting with '_' (static media) are NEVER renamed
    if filename.startswith("_"):
        return filename

    # Non-_ files cannot have static extensions (.js, .css, fonts)
    if is_static_ext:
        return None

    if is_allowed_filename(filename):
        return filename

    # Generate a random name with the original extension
    random_id = str(uuid.uuid4())[:8]  # Use first 8 chars of UUID for readability
    new_filename = f"img_{random_id}.{ext}"

    logger.debug(f"Renamed invalid filename '{filename}' to '{new_filename}'")
    return new_filename


def _get_real_qt_image_classes():
    """Return real (QImage, QImageReader, QImageWriter) classes or (None, None, None)."""
    try:
        from aqt.qt import QImage, QImageReader, QImageWriter

        if not (
            isinstance(QImage, type)
            and isinstance(QImageReader, type)
            and isinstance(QImageWriter, type)
        ):
            return None, None, None
        return QImage, QImageReader, QImageWriter
    except Exception:
        return None, None, None


def _qt_supported_formats(cls) -> set[str]:
    if cls is None:
        return set()
    try:
        raw = cls.supportedImageFormats()
        if not isinstance(raw, (list, tuple)):
            return set()
        formats = set()
        for fmt in raw:
            try:
                formats.add(bytes(fmt).decode("ascii", "ignore").lower())
            except Exception:
                continue
        return formats
    except Exception:
        return set()


def _qt_supports_webp() -> bool:
    _, _, writer_cls = _get_real_qt_image_classes()
    return "webp" in _qt_supported_formats(writer_cls)


def _get_webp_tool_path(tool_name: str) -> Optional[Path]:
    """Locate vendored cwebp/gif2webp binary for current OS/arch, or fall back to PATH."""
    webp_root = Path(__file__).resolve().parent / "dist" / "webp"
    arch = platform.machine().lower()

    rel_subpath: Optional[Path] = None
    if sys.platform == "win32":
        rel_subpath = Path("windows") / "x86_64" / f"{tool_name}.exe"
    elif sys.platform.startswith("linux"):
        if arch in ("x86_64", "amd64", "x64"):
            rel_subpath = Path("linux") / "x86_64" / tool_name
        elif arch in ("aarch64", "arm64"):
            rel_subpath = Path("linux") / "aarch64" / tool_name
    elif sys.platform.startswith("darwin"):
        if arch in ("arm64", "aarch64"):
            rel_subpath = Path("macos") / "arm64" / tool_name
        elif arch in ("x86_64", "amd64"):
            rel_subpath = Path("macos") / "x86_64" / tool_name

    if rel_subpath is not None:
        candidate = webp_root / rel_subpath
        if candidate.is_file():
            if sys.platform != "win32" and not os.access(candidate, os.X_OK):
                try:
                    mode = candidate.stat().st_mode
                    os.chmod(
                        candidate,
                        mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
                    )
                except OSError as exc:
                    logger.debug(f"Could not set executable bit on {candidate}: {exc}")
            return candidate

    system_tool = shutil.which(tool_name)
    if system_tool:
        return Path(system_tool)
    return None


def _detect_optimization_available() -> bool:
    return (
        _get_webp_tool_path("cwebp") is not None
        or _get_webp_tool_path("gif2webp") is not None
        or _qt_supports_webp()
    )


OPTIMIZATION_AVAILABLE = _detect_optimization_available()
if not OPTIMIZATION_AVAILABLE:
    logger.warning("WebP encoder not available - image optimization disabled")


def can_optimize():
    """Check if WebP optimization backend is available."""
    return bool(OPTIMIZATION_AVAILABLE)


def _sniff_image_format(filepath: Path) -> str:
    try:
        with filepath.open("rb") as fh:
            head = fh.read(12)
    except OSError:
        return "OTHER"

    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if head.startswith(b"BM"):
        return "BMP"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return "OTHER"


def _is_valid_webp_file(filepath: Path) -> bool:
    try:
        if not filepath.is_file() or filepath.stat().st_size < 12:
            return False
        with filepath.open("rb") as fh:
            head = fh.read(12)
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    except OSError:
        return False


def _is_animated_gif(filepath: Path) -> bool:
    """Return True when the GIF file contains more than one image frame."""
    _, reader_cls, _ = _get_real_qt_image_classes()
    if reader_cls is not None:
        try:
            reader = reader_cls(str(filepath))
            if reader.canRead() and reader.imageCount() > 1:
                return True
        except Exception:
            pass

    try:
        data = filepath.read_bytes()
    except OSError:
        return False

    if len(data) < 13 or not data.startswith((b"GIF87a", b"GIF89a")):
        return False

    pos = 10
    packed = data[pos]
    pos += 3
    if packed & 0x80:
        gct_size = 3 * (1 << ((packed & 0x07) + 1))
        pos += gct_size

    def _skip_subblocks(idx: int) -> int:
        while idx < len(data):
            block_len = data[idx]
            idx += 1
            if block_len == 0:
                return idx
            idx += block_len
        return len(data)

    frames = 0
    while pos < len(data):
        introducer = data[pos]
        pos += 1
        if introducer == 0x3B:  # Trailer
            break
        if introducer == 0x21:  # Extension
            if pos >= len(data):
                break
            pos += 1  # Label
            pos = _skip_subblocks(pos)
        elif introducer == 0x2C:  # Image Descriptor
            if pos + 9 > len(data):
                break
            img_packed = data[pos + 8]
            pos += 9
            if img_packed & 0x80:
                pos += 3 * (1 << ((img_packed & 0x07) + 1))
            if pos >= len(data):
                break
            pos += 1  # LZW minimum code size
            pos = _skip_subblocks(pos)
            frames += 1
            if frames > 1:
                return True
        else:
            break
    return False


def _write_rgba_png(png_path: Path, width: int, height: int, rows_rgba: bytes) -> None:
    """Write a minimal valid 8-bit RGBA PNG using only stdlib struct + zlib."""

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # Filter type 0 (None)
        start = y * stride
        raw.extend(rows_rgba[start : start + stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
    png_path.write_bytes(png_bytes)


def _bmp_bytes_to_png(bmp_bytes: bytes, png_path: Path) -> bool:
    """Decode uncompressed 24-bit BGR / 32-bit BGRA BMP bytes into a PNG file."""
    if len(bmp_bytes) < 54 or bmp_bytes[:2] != b"BM":
        return False

    pixel_offset = struct.unpack_from("<I", bmp_bytes, 10)[0]
    dib_size = struct.unpack_from("<I", bmp_bytes, 14)[0]
    if dib_size < 40 or len(bmp_bytes) < 14 + dib_size:
        return False

    width, raw_height = struct.unpack_from("<ii", bmp_bytes, 18)
    planes, bpp, compression = struct.unpack_from("<HHI", bmp_bytes, 26)
    if planes != 1 or width <= 0 or raw_height == 0:
        return False
    if bpp not in (24, 32) or compression not in (0, 3):
        return False

    top_down = raw_height < 0
    height = abs(raw_height)
    row_stride = ((width * bpp + 31) // 32) * 4
    required = pixel_offset + row_stride * height
    if len(bmp_bytes) < required:
        return False

    rgba = bytearray(width * height * 4)
    for dst_y in range(height):
        src_y = dst_y if top_down else (height - 1 - dst_y)
        src_row = pixel_offset + src_y * row_stride
        dst_row = dst_y * width * 4
        if bpp == 24:
            for x in range(width):
                s = src_row + x * 3
                d = dst_row + x * 4
                rgba[d] = bmp_bytes[s + 2]
                rgba[d + 1] = bmp_bytes[s + 1]
                rgba[d + 2] = bmp_bytes[s]
                rgba[d + 3] = 255
        else:
            for x in range(width):
                s = src_row + x * 4
                d = dst_row + x * 4
                rgba[d] = bmp_bytes[s + 2]
                rgba[d + 1] = bmp_bytes[s + 1]
                rgba[d + 2] = bmp_bytes[s]
                rgba[d + 3] = bmp_bytes[s + 3]

    _write_rgba_png(png_path, width, height, bytes(rgba))
    return True


def _bmp_to_temp_png(filepath: Path, png_path: Path) -> bool:
    """Convert BMP to a temporary PNG via Qt QImage or pure-Python BMP decoder."""
    qimage_cls, _, _ = _get_real_qt_image_classes()
    if qimage_cls is not None:
        try:
            img = qimage_cls(str(filepath))
            if not img.isNull() and img.save(str(png_path), "PNG"):
                return True
        except Exception as exc:
            logger.debug(f"Qt BMP->PNG bridge failed for {filepath.name}: {exc}")

    try:
        return _bmp_bytes_to_png(filepath.read_bytes(), png_path)
    except OSError:
        return False


def _run_webp_tool(cmd: list[str], timeout: int = WEBP_TOOL_TIMEOUT_SECONDS) -> bool:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        result = subprocess.run(cmd, **kwargs)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(f"WebP tool execution failed ({cmd[0]}): {exc}")
        return False

    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        logger.warning(f"WebP tool returned {result.returncode} for {cmd[0]}: {err}")
        return False
    return True


def _convert_with_qt(
    filepath: Path, tmp_out: Path, target_format: str, quality: int
) -> bool:
    qimage_cls, _, writer_cls = _get_real_qt_image_classes()
    if qimage_cls is None or writer_cls is None:
        return False

    fmt_lower = target_format.lower()
    if fmt_lower not in _qt_supported_formats(writer_cls):
        return False

    try:
        img = qimage_cls(str(filepath))
        if img.isNull():
            return False

        if fmt_lower in ("jpg", "jpeg") and img.hasAlphaChannel():
            try:
                from aqt.qt import QColor, QPainter

                if isinstance(QColor, type) and isinstance(QPainter, type):
                    flattened = qimage_cls(img.size(), qimage_cls.Format.Format_RGB32)
                    flattened.fill(QColor(255, 255, 255))
                    painter = QPainter(flattened)
                    painter.drawImage(0, 0, img)
                    painter.end()
                    img = flattened
            except Exception:
                pass

        writer = writer_cls(str(tmp_out), fmt_lower.encode("ascii"))
        if quality >= 0:
            writer.setQuality(quality)
        return bool(writer.write(img))
    except Exception as exc:
        logger.debug(f"Qt conversion failed for {filepath.name}: {exc}")
        return False


def _convert_to_webp_file(
    filepath: Path, tmp_out: Path, will_change_format: bool
) -> bool:
    sniffed = _sniff_image_format(filepath)
    suffix = filepath.suffix.lower()

    if sniffed == "WEBP":
        if will_change_format:
            shutil.copyfile(filepath, tmp_out)
            return True
        return False

    if sniffed == "GIF" or suffix == ".gif":
        gif2webp = _get_webp_tool_path("gif2webp")
        if gif2webp is not None and _run_webp_tool(
            [
                str(gif2webp),
                "-mixed",
                "-q",
                str(WEBP_QUALITY),
                "-quiet",
                "-o",
                str(tmp_out),
                "--",
                str(filepath),
            ]
        ):
            return True

        # Never flatten animated GIFs into a static WebP via QImage fallback.
        if _is_animated_gif(filepath):
            return False
        return _convert_with_qt(filepath, tmp_out, "webp", WEBP_QUALITY)

    cwebp = _get_webp_tool_path("cwebp")

    if sniffed == "BMP" or suffix == ".bmp":
        if sys.platform == "win32" and cwebp is not None:
            if _run_webp_tool(
                [
                    str(cwebp),
                    "-q",
                    str(WEBP_QUALITY),
                    "-quiet",
                    "-o",
                    str(tmp_out),
                    "--",
                    str(filepath),
                ]
            ):
                return True

        if cwebp is not None:
            tmp_png = tmp_out.with_name(f".{tmp_out.stem}.bmp2png.png")
            try:
                if _bmp_to_temp_png(filepath, tmp_png) and _run_webp_tool(
                    [
                        str(cwebp),
                        "-q",
                        str(WEBP_QUALITY),
                        "-quiet",
                        "-o",
                        str(tmp_out),
                        "--",
                        str(tmp_png),
                    ]
                ):
                    return True
            finally:
                tmp_png.unlink(missing_ok=True)

        return _convert_with_qt(filepath, tmp_out, "webp", WEBP_QUALITY)

    if cwebp is not None and _run_webp_tool(
        [
            str(cwebp),
            "-q",
            str(WEBP_QUALITY),
            "-quiet",
            "-o",
            str(tmp_out),
            "--",
            str(filepath),
        ]
    ):
        return True

    return _convert_with_qt(filepath, tmp_out, "webp", WEBP_QUALITY)


def optimize_image(filepath, destination=None, convert_to_webp=True):
    """
    Optimize an image file to reduce size.

    Args:
        filepath: Path to the original image
        destination: Destination path for optimized output
        convert_to_webp: Whether to convert to WebP format

    Returns:
        Tuple[Path, bool]:
            - Path: The path to the file that should be used (original or optimized/converted).
            - bool: True if the intended `destination` file was successfully created and is the returned path, False otherwise.
    """
    if not OPTIMIZATION_AVAILABLE:
        logger.warning("Optimization skipped: WebP encoder not available.")
        return Path(filepath), False

    filepath = Path(filepath)

    if not filepath.is_file():
        logger.error(f"Source file not found or is not a file: {filepath}")
        return filepath, False
    try:
        original_size = filepath.stat().st_size
    except OSError as e:
        logger.error(f"Could not get stats for source file {filepath}: {e}")
        return filepath, False

    if filepath.suffix.lower() not in OPTIMIZABLE_INPUT_EXTENSIONS:
        logger.debug(
            f"Skipping optimization for unsupported file type: {filepath.name}"
        )
        return filepath, False

    des_path = Path(destination) if destination else None

    if not des_path:
        logger.error("Optimize_image called without a destination path.")
        return filepath, False

    will_change_format = (convert_to_webp and filepath.suffix.lower() != ".webp") or (
        not convert_to_webp and des_path.suffix.lower() != filepath.suffix.lower()
    )

    if des_path == filepath and not will_change_format:
        logger.debug(
            f"Skipping optimization: Destination same as source, and no format change intended for {filepath.name}."
        )
        return filepath, False

    tmp_out: Optional[Path] = None
    try:
        des_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_suffix = ".webp" if convert_to_webp else (des_path.suffix or ".tmp")
        tmp_out = des_path.parent / (
            f".{des_path.stem}.{uuid.uuid4().hex[:8]}.tmp{tmp_suffix}"
        )

        if convert_to_webp:
            converted = _convert_to_webp_file(filepath, tmp_out, will_change_format)
            if not converted or not _is_valid_webp_file(tmp_out):
                logger.warning(
                    f"WebP conversion failed for {filepath.name}. Skipping optimization."
                )
                return filepath, False
        else:
            ext = des_path.suffix.lower()
            if ext in (".jpg", ".jpeg"):
                fmt, quality = "jpeg", JPEG_QUALITY
            elif ext == ".png":
                fmt, quality = "png", max(0, min(100, 100 - PNG_COMPRESSION * 10))
            else:
                fmt, quality = ext.lstrip("."), -1

            if not fmt or not _convert_with_qt(filepath, tmp_out, fmt, quality):
                logger.warning(
                    f"Image optimization failed for {filepath.name}. Skipping optimization."
                )
                return filepath, False

        try:
            optimized_size = tmp_out.stat().st_size
        except OSError as e:
            logger.error(
                f"Could not get stats for temporary destination file {tmp_out}: {e}"
            )
            return filepath, False

        if optimized_size <= 0:
            return filepath, False

        use_destination = optimized_size < original_size or will_change_format
        if use_destination:
            os.replace(tmp_out, des_path)
            tmp_out = None
            logger.debug(
                f"Optimization successful for {filepath.name}. Using {des_path.name} ({original_size} -> {optimized_size} bytes)"
            )
            return des_path, True

        logger.debug(
            f"Optimization did not improve {filepath.name} sufficiently ({original_size} -> {optimized_size} bytes). Using original."
        )
        return filepath, False

    except Exception as e:
        logger.error(f"Error optimizing {filepath.name}: {str(e)}", exc_info=False)
        return filepath, False
    finally:
        if tmp_out is not None and tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError as unlink_err:
                logger.error(
                    f"Error cleaning up temporary optimization file {tmp_out.name}: {unlink_err}"
                )


async def optimize_media_file(filename, filepath_obj):
    """
    Optimize a media file if it's an image, converting to WebP if possible.

    Args:
        filename: Original filename
        filepath_obj: Path object to the file

    Returns:
        Tuple of (optimized filepath string, new filename, whether optimization occurred)
    """
    # Static media files starting with '_' are NEVER renamed and NEVER optimized
    if filename.startswith("_"):
        logger.debug(f"Skipping optimization and renaming for static file: {filename}")
        return str(filepath_obj), filename, False

    sanitized_name = sanitize_filename(filename)
    if sanitized_name is None:
        logger.warning(f"Skipping file with invalid extension: {filename}")
        return None, None, False

    if sanitized_name != filename:
        new_filepath = filepath_obj.parent / sanitized_name
        try:
            # Check for collision before rename
            if new_filepath.exists() and not filepath_obj.samefile(new_filepath):
                logger.warning(
                    f"Sanitized filename {sanitized_name} already exists. Skipping rename/processing for {filename}."
                )
                return None, None, False
            elif not new_filepath.exists():
                shutil.move(str(filepath_obj), str(new_filepath))
                filepath_obj = new_filepath
                filename = sanitized_name
                logger.info(f"Renamed invalid file '{filename}' to '{sanitized_name}'")

        except Exception as e:
            logger.error(
                f"Failed to rename invalid file '{filename}' to '{sanitized_name}': {str(e)}"
            )
            return None, None, False

    current_filepath_obj = filepath_obj
    current_filename = filename
    current_filepath_str = str(current_filepath_obj)

    if not can_optimize():
        logger.debug("Optimization skipped: WebP encoder not available.")
        return str(filepath_obj), filename, False

    file_extension = current_filepath_obj.suffix.lower()

    # Skip WebP files entirely - they're already in target format and don't need re-optimization
    # Filename sanitization has already been applied above, so we're done
    if file_extension == ".webp":
        logger.debug(
            f"Skipping re-optimization for already-optimized WebP file: {current_filename}"
        )
        return current_filepath_str, current_filename, False

    # Skip optimization for small files - the compression savings aren't worth the processing overhead
    # Threshold: 100KB - small enough that network transfer is fast, savings are minimal
    try:
        file_size = current_filepath_obj.stat().st_size
        if file_size < 100 * 1024:  # 100KB
            logger.debug(
                f"Skipping optimization for small file: {current_filename} ({file_size} bytes)"
            )
            return current_filepath_str, current_filename, False
    except OSError as e:
        logger.warning(
            f"Could not check file size for {current_filename}: {e}. Proceeding with optimization."
        )

    is_optimizable_input = (
        file_extension in OPTIMIZABLE_INPUT_EXTENSIONS
    )  # Reuse list from optimize_image

    if is_optimizable_input:
        intended_webp_filename = f"{current_filepath_obj.stem}.webp"
        intended_webp_filepath = current_filepath_obj.with_name(intended_webp_filename)

        def _run_bounded_optimize():
            with _CONVERSION_SEMAPHORE:
                return optimize_image(
                    current_filepath_obj,
                    destination=intended_webp_filepath,
                    convert_to_webp=True,
                )

        final_path_obj, used_webp_destination = await asyncio.to_thread(
            _run_bounded_optimize
        )

        final_filepath_str = str(final_path_obj)
        final_filename = final_path_obj.name

        optimization_occurred = used_webp_destination

        return final_filepath_str, final_filename, optimization_occurred

    else:
        # Not an image type we attempt to optimize
        logger.debug(
            f"Skipping optimization for non-image or unsupported type: {current_filename}"
        )
        return current_filepath_str, current_filename, False


def sanitize_svg_files(svg_file_list):
    """
    Pre-sanitize SVG files

    Args:
        svg_file_list: List of tuples (filename, filepath_obj)

    Returns:
        Dictionary mapping original filenames to (sanitized_content, hash) tuples
    """
    MAX_BATCH_SIZE = 250  # Files per batch
    MAX_BATCH_BYTES = 1 * 1024 * 1024  # 1MB per batch

    svg_contents = []
    for filename, filepath_obj in svg_file_list:
        try:
            with open(filepath_obj, "rb") as f:
                svg_content = f.read().decode("utf-8")
                if not svg_content:
                    logger.error(f"SVG {filename} is empty")
                    continue
                svg_contents.append((filename, svg_content))
        except Exception as e:
            logger.error(f"Error reading SVG {filename}: {str(e)}")

    # Split into properly sized batches
    batches = []
    current_batch = []
    current_batch_size = 0

    for filename, content in svg_contents:
        content_size = len(content.encode("utf-8"))

        # Check if adding this file would exceed batch limits
        if (
            len(current_batch) >= MAX_BATCH_SIZE
            or current_batch_size + content_size > MAX_BATCH_BYTES
        ) and current_batch:
            # Current batch is full, append it and start a new one
            batches.append(current_batch)
            current_batch = []
            current_batch_size = 0

        # Add file to current batch
        current_batch.append((filename, content))
        current_batch_size += content_size

    # Add the last batch if not empty
    if current_batch:
        batches.append(current_batch)

    logger.debug(f"Processing {len(svg_contents)} SVG files in {len(batches)} batches")

    # Process all batches
    sanitized_map = {}
    for batch_index, batch in enumerate(batches):
        logger.debug(
            f"Processing SVG batch {batch_index+1}/{len(batches)} with {len(batch)} files"
        )

        payload = {
            "svg_files": [{"filename": f, "content": c} for f, c in batch],
        }

        try:
            from .api_client import api_client

            response = api_client.post_json(
                "/media/sanitize/svg",
                payload,
                timeout=30,
            )

            if response.status_code != 200:
                logger.error(
                    f"Error sanitizing SVGs (batch {batch_index+1}): {response.text}"
                )
                continue

            result = response.json()

            # Update our sanitized map with results from this batch
            for item in result["sanitized_files"]:
                content = item.get("content", "")
                expected_hash = item.get("hash")
                computed_hash = hashlib.md5(content.encode("utf-8")).hexdigest()

                if expected_hash and expected_hash != computed_hash:
                    logger.warning(
                        f"Sanitized SVG hash mismatch for {item.get('filename')}: expected={expected_hash}, computed={computed_hash}"
                    )

                sanitized_map[item["filename"]] = (
                    content,
                    expected_hash,
                    computed_hash,
                )

        except Exception as e:
            logger.error(
                f"Exception during SVG sanitization (batch {batch_index+1}): {str(e)}"
            )

    logger.debug(
        f"Successfully sanitized {len(sanitized_map)}/{len(svg_contents)} SVG files"
    )
    return sanitized_map


async def optimize_svg_files(svg_file_list):
    """
    Optimize SVG files

    Args:
        svg_file_list: List of tuples (filename, filepath_obj)

    Returns:
        Dictionary mapping original filenames to (filepath, expected_hash, was_optimized)
    """
    # Sanitize all SVGs in batches
    sanitized_map = sanitize_svg_files(svg_file_list)
    # Write optimized contents back and prepare result map
    result_map = {}
    for filename, filepath_obj in svg_file_list:
        if filename in sanitized_map:
            sanitized_content, exp_hash, computed_hash = sanitized_map[filename]

            # Prefer the computed hash from the sanitized content; fall back to exp_hash if missing
            # Trust the server-provided hash when available to avoid subtle JSON/whitespace
            # re-encoding differences between the response body and what was hashed on the
            # server. If it's missing, fall back to our recomputed value.
            result_hash = exp_hash or computed_hash or ""
            try:
                # Write sanitized content back to file
                with open(filepath_obj, "wb") as f:
                    f.write(sanitized_content.encode("utf-8"))

                # If the provided hash doesn't match what we wrote, log and prefer the on-disk hash later
                if exp_hash and exp_hash != result_hash:
                    logger.warning(
                        f"Sanitized SVG hash mismatch for {filename}: expected={exp_hash}, computed={result_hash}"
                    )

                result_map[filename] = (str(filepath_obj), result_hash, True)
            except Exception as e:
                logger.error(f"Error writing optimized SVG {filename}: {str(e)}")
        else:
            # If sanitization failed or wasn't performed
            result_map[filename] = (str(filepath_obj), "", False)

    return result_map
