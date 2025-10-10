import asyncio
import hashlib
import logging
import mimetypes
import os
import time
import uuid
import requests
import base64
import binascii
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, cast
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from asyncio import Lock as AsyncLock, get_running_loop # Use asyncio Lock

from aqt import mw
from .utils import get_logger
from .sentry_integration import capture_media_exception, capture_media_message

logger = get_logger("ankicollab.media_manager")

# --- Add File Logging ---
# log_file_path = os.path.join(os.path.dirname(__file__), 'ankicollab_media_errors.log')
# file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
# file_handler.setLevel(logging.DEBUG)
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# file_handler.setFormatter(formatter)
# if not any(isinstance(h, logging.FileHandler) and h.baseFilename == file_handler.baseFilename for h in logger.handlers):
#     logger.addHandler(file_handler)
# --- End File Logging ---


MAX_REQUESTS_PER_MINUTE = 1000
REQUEST_TRACKING_WINDOW = 60
MAX_FILE_SIZE = 2 * 1024 * 1024 # 2 MB
CHUNK_SIZE = 131072  # 128KB
REQUEST_TIMEOUT = 30
VERIFY_SSL = True

ALLOWED_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"},
    "audio": {".mp3", ".ogg"},
}
ALL_ALLOWED_EXTENSIONS = set().union(*ALLOWED_EXTENSIONS.values())

# --- Exception Classes  ---
class MediaError(Exception): pass
class MediaServerError(MediaError): pass
class MediaRateLimitError(MediaError): pass
class MediaTypeError(MediaError): pass
class MediaHashError(MediaError): pass
class MediaUploadError(MediaError): pass
class MediaDownloadError(MediaError): pass
# --- End Exception Classes ---

# --- RateLimiter Class  ---
class RateLimiter:
    def __init__(self, max_calls: int, period: int):
        self.max_calls = max_calls
        self.period = period
        self.calls = []

    async def wait_if_needed(self) -> None:
        now = time.time()
        self.calls = [t for t in self.calls if now - t < self.period]
        if len(self.calls) >= self.max_calls:
            oldest_call = self.calls[0]
            wait_time = self.period - (now - oldest_call)
            if wait_time > 0:
                logger.debug(f"Rate limit reached. Waiting for {wait_time:.2f} seconds")
                await asyncio.sleep(wait_time)
        self.calls.append(now)
# --- End RateLimiter Class ---

# --- retry decorator  ---
def retry(max_tries=3, delay=1, backoff=2, exceptions=(requests.RequestException, TimeoutError, MediaServerError, MediaUploadError, MediaDownloadError)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            mtries, mdelay = max_tries, delay
            last_exception = None
            attempt_index = 0
            while mtries > 0:
                try:
                    result = await func(*args, **kwargs)
                    if attempt_index > 0:
                        capture_media_message(
                            "media-operation-recovered",
                            level="warning",
                            context={
                                "stage": func.__name__,
                                "attempts": attempt_index + 1,
                                "max_tries": max_tries,
                            },
                        )
                    return result
                except exceptions as e:
                    last_exception = e
                    attempt_index += 1

                    metadata = getattr(e, "metadata", {})
                    metadata_dict = metadata if isinstance(metadata, dict) else {}
                    status_code = metadata_dict.get("status_code")

                    if status_code is None and isinstance(e, requests.HTTPError) and e.response is not None:
                        status_code = e.response.status_code

                    if status_code == 404:
                        logger.warning(f"Resource not found (404) in {func.__name__}. Not retrying.")
                        raise e

                    # Don't retry auth errors - token won't suddenly become valid
                    if isinstance(e, (MediaUploadError, MediaDownloadError)) and metadata_dict.get("status_code") == 401:
                        logger.error(f"Authentication error (401) in {func.__name__} - not retrying")
                        raise e
                    
                    # Detect rate limiting and use exponential backoff
                    is_rate_limit = False
                    if isinstance(e, requests.HTTPError) and e.response.status_code == 429:
                        is_rate_limit = True
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            mdelay = max(mdelay, int(retry_after))
                        else:
                            # Exponential backoff for 429: delay * backoff^attempt
                            mdelay = delay * (backoff ** attempt_index)
                        logger.warning(f"Rate limited (429) in {func.__name__}. Retrying in {mdelay}s... ({mtries-1} tries left)")
                    elif isinstance(e, (MediaUploadError, MediaDownloadError)):
                        # Check metadata for 429
                        if metadata_dict.get("status_code") == 429:
                            is_rate_limit = True
                            mdelay = delay * (backoff ** attempt_index)
                            logger.warning(f"Rate limited (429) in {func.__name__}. Retrying in {mdelay}s... ({mtries-1} tries left)")
                        else:
                            logger.warning(f"Upload/Download error: {str(e)}. Retrying in {mdelay}s... ({mtries-1} tries left)")
                    elif isinstance(e, MediaServerError):
                        logger.warning(f"Server error encountered: {str(e)}. Retrying in {mdelay}s... ({mtries-1} tries left)")
                    else:
                        logger.warning(f"Request failed: {str(e)}. Retrying in {mdelay}s... ({mtries-1} tries left)")

                    await asyncio.sleep(mdelay)
                    mtries -= 1
                    # Only apply backoff if not rate limited (rate limit uses exponential above)
                    if not is_rate_limit:
                        mdelay *= backoff
                    mdelay *= backoff

            logger.error(f"Maximum retries reached for {func.__name__}. Last error: {last_exception}")
            # Re-raise the last exception encountered
            if last_exception:
                metadata = getattr(last_exception, "metadata", None)
                context = {
                    "stage": func.__name__,
                    "attempts": attempt_index,
                    "max_tries": max_tries,
                }
                if isinstance(metadata, dict):
                    context.update(metadata)
                capture_media_exception(last_exception, context=context)
                raise last_exception
            else:
                # Should not happen, but raise a generic error if it does
                raise MediaServerError(f"Failed after {max_tries} tries without specific exception")
        return wrapper
    return decorator
# --- End retry decorator ---

class MediaManager:
    def __init__(self, api_base_url: str, media_folder: str):
        self.api_base_url = api_base_url.rstrip("/")
        self.media_folder = Path(media_folder)
        self.rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE, REQUEST_TRACKING_WINDOW)

        if not self.media_folder.exists():
            raise ValueError(f"Media folder not found at: {media_folder}")

        self.optimize_images = True
        self.hash_cache = {}
        self.optimization_cache = {}  # Cache for optimization results

        self.session = requests.Session()
        self.session.verify = VERIFY_SSL

        self.semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_lock = AsyncLock() # Use asyncio's Lock
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
        # Reasonable thread pool sizing - the bottleneck is image processing, not our code
        cpu_count = os.cpu_count() or 2
        max_worker = min(32, max(4, cpu_count * 2))  # Simple: 2x cores, max 32
        
        self.thread_executor = ThreadPoolExecutor(max_workers=max_worker)
        logger.debug(f"Initialized thread pool with {max_worker} workers")
        mimetypes.add_type('image/webp', '.webp') # how the fuck is this not a default

    async def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazily initializes the semaphore for the current event loop."""
        try:
            current_loop = get_running_loop()
        except RuntimeError:
            # This should ideally not happen if called from within an async context
            logger.error("_get_semaphore called without a running event loop!")
            raise RuntimeError("Cannot get semaphore without a running event loop")

        # Check if semaphore is None OR if it belongs to a different loop
        if self.semaphore is None or self._semaphore_loop is not current_loop:
            async with self._semaphore_lock:
                # Double-check after acquiring lock
                if self.semaphore is None or self._semaphore_loop is not current_loop:
                    logger.debug(f"Initializing asyncio.Semaphore for loop {id(current_loop)}")
                    self.semaphore = asyncio.Semaphore(5)
                    self._semaphore_loop = current_loop # Store the loop it's associated with
        return cast(asyncio.Semaphore, self.semaphore)

    async def async_request(self, method, url, **kwargs):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("async_request called without a running event loop!")
            # try get_event_loop as fallback?
            raise RuntimeError("No running event loop found for async_request")

        # Check if executor is still available
        if not hasattr(self, 'thread_executor') or self.thread_executor._shutdown:
            raise RuntimeError(f"Thread executor is shut down, cannot make request to {url}")

        if "timeout" not in kwargs:
            kwargs["timeout"] = REQUEST_TIMEOUT

        # Ensure SSL verification is always enabled
        if "verify" not in kwargs:
            kwargs["verify"] = VERIFY_SSL

        def do_request():
            try:
                response = self.session.request(method, url, **kwargs)
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                return response
            except requests.exceptions.RequestException as req_exc:
                 # Log details before raising, helps debugging network issues
                 logger.error(f"Network request failed: {method.upper()} {url} -> {req_exc}")
                 raise # Re-raise the original exception

        # Run the blocking request in the thread pool
        return await loop.run_in_executor(self.thread_executor, do_request)

    def clear_caches(self):
        """Clear all internal caches to free memory or force re-computation."""
        self.hash_cache.clear()
        self.optimization_cache.clear()
        logger.info("All media manager caches cleared")

    def set_media_folder(self, media_folder: str):
        self.media_folder = Path(media_folder)
        if not self.media_folder.exists():
            raise ValueError(f"Media folder not found at: {media_folder}")

    async def close(self):
        if hasattr(self, 'thread_executor'):
            self.thread_executor.shutdown(wait=True) # Wait for tasks to complete
        if hasattr(self, 'session'):
            self.session.close()
    
    async def _calculate_file_hash(self, filepath: Path) -> str:
        """Centralized hash calculation to avoid code duplication."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("_calculate_file_hash called without a running event loop!")
            raise RuntimeError("No running event loop found for hash computation")

        # Check if executor is still available
        if not hasattr(self, 'thread_executor') or self.thread_executor._shutdown:
            raise MediaHashError(f"Thread executor is shut down, cannot calculate hash for {filepath}")

        def calculate_hash():
            md5_hash = hashlib.md5()
            try:
                with open(filepath, "rb") as f:
                    while True:
                        byte_block = f.read(CHUNK_SIZE)
                        if not byte_block:
                            break
                        md5_hash.update(byte_block)
                return md5_hash.hexdigest()
            except IOError as io_err:
                raise MediaHashError(f"IOError reading file for hashing {filepath}: {io_err}") from io_err

        return await loop.run_in_executor(self.thread_executor, calculate_hash)

    def _is_allowed_file_type(self, filename: Union[str, Path]) -> bool:
        ext = Path(filename).suffix.lower()
        return ext in ALL_ALLOWED_EXTENSIONS

    async def compute_file_hash_and_size(self, filepath: Union[str, Path]) -> Tuple[str, int]:
        filepath = Path(filepath)
        if not filepath.exists() or not filepath.is_file():
            # Use specific exception type
            raise FileNotFoundError(f"File not found or not a file: {filepath}")

        file_size = filepath.stat().st_size
        cache_key = str(filepath)

        try:
            mtime = os.path.getmtime(filepath)
            if cache_key in self.hash_cache and self.hash_cache[cache_key][0] == mtime:
                return (self.hash_cache[cache_key][1], file_size)
        except OSError as e:
            # Log error but proceed to calculate hash if possible
            logger.warning(f"Could not get mtime for hash cache check: {filepath} - {e}")
        except KeyError:
             # Cache entry might exist but tuple access failed - recalculate
             pass

        try:
            file_hash = await self._calculate_file_hash(filepath)

            # Store in cache with modification time
            try:
                # Get mtime again after hashing, in case it changed
                mtime_after = os.path.getmtime(filepath)
                self.hash_cache[cache_key] = (mtime_after, file_hash)
            except OSError:
                # File might have been deleted during hashing
                logger.warning(f"File {filepath} possibly deleted during hash calculation.")
                pass # Hash is calculated, but won't be cached reliably

            return (file_hash, file_size)

        except MediaHashError: # Re-raise specific error from calculate_hash
             raise
        except Exception as e:
            logger.error(f"Unexpected error computing hash for {filepath}: {str(e)}")
            # Wrap unexpected errors
            raise MediaHashError(f"Unexpected error computing file hash for {filepath}: {str(e)}") from e

    def validate_file_basic(self, filepath: Path) -> bool:
        try:
            if not filepath.exists():
                logger.warning(f"File does not exist: {filepath}")
                return False

            try:
                file_size = filepath.stat().st_size
            except OSError as stat_err:
                logger.warning(f"Could not read file size for {filepath}: {stat_err}")
                return False

            if file_size == 0:
                logger.warning(f"File is empty: {filepath}")
                return False

            if file_size > MAX_FILE_SIZE:
                logger.warning(f"File exceeds size limit ({MAX_FILE_SIZE} bytes): {filepath}")
                return False

            if not self._is_allowed_file_type(filepath):
                logger.warning(f"File type not allowed: {filepath.suffix}")
                return False
            
            with open(filepath, "rb") as f:
                header = f.read(512)
                if not header:
                    logger.warning(f"Unable to read header for file: {filepath}")
                    return False

                ext = filepath.suffix.lower()

                if ext in ['.jpg', '.jpeg']:
                    if len(header) < 3 or not header.startswith(b'\xFF\xD8\xFF'):
                        logger.warning(f"Invalid JPEG signature for file: {filepath}")
                        return False
                elif ext == '.png':
                    if len(header) < 8 or not header.startswith(b'\x89PNG\r\n\x1A\n'):
                        logger.warning(f"Invalid PNG signature for file: {filepath}")
                        return False
                elif ext == '.gif':
                    if len(header) < 6 or not (header.startswith(b'GIF87a') or header.startswith(b'GIF89a')):
                        logger.warning(f"Invalid GIF signature for file: {filepath}")
                        return False
                elif ext == '.webp':
                    if len(header) < 12 or not header.startswith(b'RIFF') or header[8:12] != b'WEBP':
                        logger.warning(f"Invalid WebP signature for file: {filepath}")
                        return False
                elif ext == '.svg':
                    text_header = header.decode('utf-8', errors='ignore').lower()
                    if '<svg' not in text_header:
                        logger.warning(f"Invalid SVG content for file: {filepath}")
                        return False
                elif ext == '.bmp':
                    if len(header) < 2 or not header.startswith(b'BM'):
                        logger.warning(f"Invalid BMP signature for file: {filepath}")
                        return False
                elif ext in ['.tif', '.tiff']:
                    if len(header) < 4 or not (header.startswith(b'II*\x00') or header.startswith(b'MM\x00*')):
                        logger.warning(f"Invalid TIFF signature for file: {filepath}")
                        return False
                elif ext == '.mp3':
                    # MP3 files can have ID3 tags, padding, or start directly with frame sync
                    # Search for valid MP3 frame sync (0xFF followed by 0xE0-0xFF) within header
                    has_valid_mp3_marker = False
                    if header.startswith(b'ID3'):
                        has_valid_mp3_marker = True
                    else:
                        # Search for frame sync pattern in the header
                        for i in range(len(header) - 1):
                            if header[i] == 0xFF and (header[i + 1] & 0xE0) == 0xE0:
                                has_valid_mp3_marker = True
                                break
                    
                    if not has_valid_mp3_marker:
                        logger.warning(f"Invalid MP3 signature for file: {filepath}")
                        return False
                elif ext == '.ogg':
                    if len(header) < 4 or not header.startswith(b'OggS'):
                        logger.warning(f"Invalid OGG signature for file: {filepath}")
                        return False
            return True
        except Exception as e:
            logger.error(f"Error validating file {filepath}: {str(e)}")
            return False

    @retry(max_tries=3, delay=2, backoff=3)
    async def upload_file(self, upload_url: str, filepath: Union[str, Path], file_hash: Optional[str] = None) -> bool:
        filepath = Path(filepath)

        base_context = {
            "stage": "upload_file",
            "filename": filepath.name,
            "upload_host": urlparse(upload_url).netloc if upload_url else None,
        }

        if not filepath.exists() or not filepath.is_file():
            exc = FileNotFoundError(f"Missing file for upload: {filepath}")
            setattr(exc, "metadata", {**base_context, "reason": "file_missing"})
            raise exc

        file_size = filepath.stat().st_size
        base_context["file_size"] = file_size

        if file_size > MAX_FILE_SIZE:
            exc = MediaTypeError(f"File exceeds size limit: {file_size} bytes")
            setattr(exc, "metadata", {**base_context, "reason": "file_too_large"})
            raise exc

        if not self.validate_file_basic(filepath):
            exc = MediaTypeError(f"File failed basic validation: {filepath}")
            setattr(exc, "metadata", {**base_context, "reason": "validation_failed"})
            raise exc

        await self.rate_limiter.wait_if_needed()
        
        semaphore = await self._get_semaphore()
        async with semaphore:
            try:
                file_ext = Path(filepath).suffix.lower()
                content_type_map = {
                    '.webp': 'image/webp', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.png': 'image/png', '.gif': 'image/gif', '.svg': 'image/svg+xml',
                    '.bmp': 'image/bmp', '.tif': 'image/tiff', '.tiff': 'image/tiff',
                    '.mp3': 'audio/mpeg', '.ogg': 'audio/ogg'
                }
                content_type = content_type_map.get(file_ext, mimetypes.guess_type(str(filepath))[0] or "application/octet-stream")

                # the fact that we cant use sha256 as default in aws s3 is embarassing
                if file_hash is None:
                    file_hash = await self._calculate_file_hash(filepath)

                # Ensure file_hash is not None before using it
                if not file_hash:
                    raise MediaHashError(f"Failed to calculate MD5 hash for {filepath}")

                base_context["file_hash"] = file_hash

                binary_hash = binascii.unhexlify(file_hash)
                base64_md5 = base64.b64encode(binary_hash).decode('ascii')
                headers = {"Content-Type": content_type, "Content-MD5": base64_md5}

                # Check if executor is still available
                if not hasattr(self, 'thread_executor') or self.thread_executor._shutdown:
                    raise MediaUploadError(f"Thread executor is shut down, cannot upload {filepath}")

                # File upload is I/O intensive - use thread pool
                def do_upload():
                    try:
                        with open(filepath, "rb") as f:
                            file_content = f.read()
                            if len(file_content) != filepath.stat().st_size:
                                raise IOError(f"File size changed during read: {filepath}")

                        response = self.session.put(
                            upload_url,
                            data=file_content,
                            headers=headers,
                            timeout=REQUEST_TIMEOUT * 2,
                            verify=VERIFY_SSL,
                        )
                        response.raise_for_status()
                        return response
                    except requests.exceptions.RequestException as req_exc:
                        logger.error(
                            f"Network error during media upload for {filepath}: {req_exc}"
                        )
                        raise
                    except IOError as io_err:
                        logger.error(
                            f"IOError preparing media upload for {filepath}: {io_err}"
                        )
                        raise

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    logger.error("upload_file (do_upload) called without a running event loop!")
                    raise RuntimeError("No running event loop found for file upload")
                await loop.run_in_executor(self.thread_executor, do_upload)
                return True

            except requests.HTTPError as e:
                status_code = getattr(e.response, 'status_code', None)
                error_text = getattr(e.response, 'text', str(e))
                logger.error(f"HTTP error {status_code} uploading media ({filepath}): {error_text}")
                error_context = {
                    **base_context,
                    "status_code": status_code,
                    "response_text": (error_text[:512] if isinstance(error_text, str) else None),
                }
                media_error: MediaUploadError
                # Raise specific errors based on status code
                if status_code == 403:
                    media_error = MediaUploadError(f"Permission denied (403): {error_text}")
                elif status_code == 413:
                    media_error = MediaTypeError(f"File too large (413): {error_text}")  # type: ignore[assignment]
                elif status_code == 400:
                    media_error = MediaUploadError(f"Bad request (400): {error_text}")
                elif status_code == 401:
                    media_error = MediaUploadError(f"Upload token rejected (401): {error_text}")
                elif status_code == 429:
                    media_error = MediaUploadError(f"Rate limit exceeded (429): Too many requests, will retry with backoff")
                else:
                    media_error = MediaUploadError(f"Media upload failed with HTTP {status_code}: {error_text}")

                setattr(media_error, "metadata", error_context)
                raise media_error from e
            except requests.RequestException as e:
                logger.error(f"Network error during upload ({filepath}): {str(e)}")
                error_context = {**base_context, "error": str(e), "error_type": type(e).__name__}
                media_error = MediaUploadError(f"Network error during upload: {str(e)}")
                setattr(media_error, "metadata", error_context)
                raise media_error from e
            except (IOError, MediaHashError) as e: # Catch errors from MD5 calc or do_upload prep
                logger.error(f"I/O or Hash error preparing upload for {filepath}: {str(e)}")
                error_context = {**base_context, "error": str(e), "error_type": type(e).__name__}
                media_error = MediaUploadError(f"I/O or Hash error: {str(e)}")
                setattr(media_error, "metadata", error_context)
                raise media_error from e
            except Exception as e:
                 # Catch unexpected errors
                 logger.exception(f"Unexpected error during upload_file for {filepath}: {e}")
                 error_context = {**base_context, "error": str(e), "error_type": type(e).__name__}
                 media_error = MediaUploadError(f"Unexpected upload error: {e}")
                 setattr(media_error, "metadata", error_context)
                 raise media_error from e


    @retry(max_tries=3, delay=2, backoff=3)
    async def download_file(self, url: str, destination: Union[str, Path]) -> bool:
        destination = Path(destination)
        os.makedirs(destination.parent, exist_ok=True)
        # Use uuid to ensure unique temp file per download attempt, avoiding concurrent access issues
        temp_destination = destination.with_suffix(f"{destination.suffix}.tmp.{uuid.uuid4().hex[:8]}")
        temp_file_renamed = False  # Track if temp file was successfully renamed

        await self.rate_limiter.wait_if_needed()

        base_context = {
            "stage": "download_file",
            "destination": str(destination),
            "url_host": urlparse(url).netloc if url else None,
        }

        try:
            # Check if executor is still available
            if not hasattr(self, 'thread_executor') or self.thread_executor._shutdown:
                logger.error(f"Thread executor is shut down, cannot download {url}")
                error = MediaDownloadError(f"Executor unavailable for download: {url}")
                setattr(error, "metadata", {**base_context, "reason": "executor_unavailable"})
                raise error

            # Download operation is IO bound - use thread pool
            def do_download():
                downloaded_ok = False
                try:
                    with self.session.get(url, stream=True, timeout=REQUEST_TIMEOUT * 2, verify=VERIFY_SSL) as response: # Longer timeout for download, verify SSL
                        response.raise_for_status()
                        with open(temp_destination, "wb") as f:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                f.write(chunk)
                        downloaded_ok = True # Mark as successful only if fully written
                    return downloaded_ok
                except requests.exceptions.RequestException as req_exc:
                     logger.error(f"Network error downloading {url}: {req_exc}")
                     raise req_exc
                except IOError as io_err:
                     logger.error(f"IOError writing temporary download file {temp_destination}: {io_err}")
                     raise io_err

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                 logger.error("download_file called without a running event loop!")
                 raise RuntimeError("No running event loop found for file download")

            try:
                success = await loop.run_in_executor(self.thread_executor, do_download)
            except requests.exceptions.RequestException as req_exc:
                status_code = None
                response_text = None
                response = getattr(req_exc, "response", None)
                if response is not None:
                    status_code = getattr(response, "status_code", None)
                    try:
                        response_text = response.text
                    except Exception:
                        response_text = None

                metadata = {**base_context, "error": str(req_exc), "error_type": type(req_exc).__name__}
                if status_code is not None:
                    metadata["status_code"] = status_code
                    if status_code == 404:
                        metadata.setdefault("reason", "not_found")
                if response_text:
                    metadata["response_text"] = response_text[:512]

                error = MediaDownloadError(f"Network error during download: {req_exc}")
                setattr(error, "metadata", metadata)
                raise error from req_exc
            except IOError as io_err:
                error = MediaDownloadError(f"IO error during download: {io_err}")
                setattr(error, "metadata", {**base_context, "error": str(io_err), "error_type": type(io_err).__name__})
                raise error from io_err
            except Exception as exc:
                error = MediaDownloadError(f"Unexpected error during download: {exc}")
                setattr(error, "metadata", {**base_context, "error": str(exc), "error_type": type(exc).__name__})
                raise error from exc

            if success and temp_destination.exists():
                try:
                    # Check if destination already exists (another download might have completed)
                    if destination.exists() and destination.stat().st_size > 0:
                        logger.debug(f"Destination {destination} already exists, discarding downloaded temp file")
                        temp_file_renamed = True  # Mark as handled (will skip cleanup)
                        return True
                    
                    # Attempt atomic rename if possible, otherwise normal rename
                    os.replace(temp_destination, destination) # More atomic on most OS
                    temp_file_renamed = True  # Mark as renamed
                except OSError as rename_err:
                    # Check again if file exists (race condition - another thread created it)
                    if destination.exists() and destination.stat().st_size > 0:
                        logger.debug(f"Destination {destination} created by another download during rename, discarding temp file")
                        temp_file_renamed = True  # Mark as handled
                        return True
                    try:
                        os.rename(temp_destination, destination) # Fallback
                        temp_file_renamed = True  # Mark as renamed
                    except OSError as rename_err2:
                        # Final check - maybe another download succeeded
                        if destination.exists() and destination.stat().st_size > 0:
                            logger.debug(f"Destination {destination} exists after rename failure, assuming success")
                            temp_file_renamed = True
                            return True
                        logger.error(f"Failed to rename temp file {temp_destination} to {destination}: {rename_err2}")
                        return False
                return True
            else:
                 # Download failed or temp file doesn't exist
                 logger.warning(f"Download failed or temp file missing for {url}")
                 error = MediaDownloadError("Download failed without exception")
                 setattr(error, "metadata", {**base_context, "reason": "missing_temp_file"})
                 raise error

        except Exception as e:
            logger.error(f"Unexpected error downloading file {url}: {str(e)}")
            if isinstance(e, MediaDownloadError):
                if not hasattr(e, "metadata"):
                    setattr(e, "metadata", {**base_context, "reason": "unclassified"})
                raise
            download_error = MediaDownloadError(f"Download failed: {e}")
            setattr(download_error, "metadata", {**base_context, "error": str(e), "error_type": type(e).__name__})
            raise download_error from e
        finally:
            # Clean up partial downloads robustly - only if file wasn't successfully renamed
            if not temp_file_renamed and temp_destination.exists():
                try:
                    os.unlink(temp_destination)
                except OSError as unlink_err:
                     logger.warning(f"Could not delete temporary download file {temp_destination}: {unlink_err}")


    @retry(max_tries=3, delay=1)
    async def get_media_manifest(self, user_token: str, deck_hash: str, filenames: List[str]) -> Dict:
        semaphore = await self._get_semaphore()
        async with semaphore:
            await self.rate_limiter.wait_if_needed()

            url = f"{self.api_base_url}/media/manifest"
            data = {"user_token": user_token, "deck_hash": deck_hash, "filenames": filenames}

            try:
                response = await self.async_request("post", url, json=data)
                # No need for raise_for_status here, async_request does it
                return response.json()
            except requests.HTTPError as e:
                status = e.response.status_code
                text = e.response.text
                logger.error(f"HTTP error {status} getting media manifest: {text}")
                # Raise specific errors based on status code
                base_context = {
                    "stage": "get_media_manifest",
                    "deck_hash": deck_hash,
                    "filenames_count": len(filenames),
                }
                if status == 401:
                    exc = MediaServerError(f"Authorization failed (401): {text}")
                elif status == 403:
                    exc = MediaServerError(f"Permission denied (403): {text}")
                elif status == 404:
                    exc = MediaServerError(f"Deck not found (404): {text}")
                elif status == 429:
                    exc = MediaRateLimitError(f"Rate limit exceeded (429): {text}")  # type: ignore[assignment]
                else:
                    exc = MediaServerError(f"Server error {status} getting manifest: {text}")
                setattr(exc, "metadata", {**base_context, "status_code": status, "response_text": text[:512] if isinstance(text, str) else None})
                raise exc from e
            except (requests.RequestException, ValueError, KeyError) as e: # Added ValueError/KeyError for bad JSON
                logger.error(f"Error retrieving or parsing manifest data: {str(e)}")
                exc = MediaServerError(f"Failed to get or parse server response for media manifest: {str(e)}")
                setattr(exc, "metadata", {
                    "stage": "get_media_manifest",
                    "deck_hash": deck_hash,
                    "filenames_count": len(filenames),
                    "error_type": type(e).__name__,
                })
                raise exc from e

    # --- get_media_manifest_and_download (Keep logic, ensure download_file is robust) ---
    @retry(max_tries=3, delay=2)
    async def get_media_manifest_and_download(self, user_token:str, deck_hash: str, filenames: List[str], progress_callback=None) -> Dict:
        total_files = len(filenames)
        if total_files == 0:
            return {"success": True, "message": "No files to download", "downloaded": 0, "skipped": 0, "failed": 0}

        # Deduplicate filenames to avoid downloading the same file multiple times
        unique_filenames = list(dict.fromkeys(filenames))  # Preserves order while removing duplicates
        duplicates_removed = total_files - len(unique_filenames)
        if duplicates_removed > 0:
            logger.info(f"Removed {duplicates_removed} duplicate filename(s) from download list")
        
        total_files = len(unique_filenames)
        if total_files == 0:
            return {"success": True, "message": "No files to download after deduplication", "downloaded": 0, "skipped": 0, "failed": 0}

        manifest_batch_size = 500
        download_batch_size = 10 # Keep small to avoid overwhelming network/disk
        total_downloaded = 0
        total_skipped = 0
        total_failed = 0
        processed_count = 0

        all_files_to_download = []

        # Phase 1: Get manifest for all files
        logger.info(f"Getting manifest for {total_files} files...")
        for i in range(0, total_files, manifest_batch_size):
            batch_filenames = unique_filenames[i:i+manifest_batch_size]
            try:
                manifest_data = await self.get_media_manifest(user_token, deck_hash, batch_filenames)
                if manifest_data and "files" in manifest_data:
                    files_in_batch = manifest_data.get("files", [])
                    if files_in_batch:
                         all_files_to_download.extend(files_in_batch)
                    else:
                         logger.warning(f"Manifest batch {i // manifest_batch_size + 1} returned no files.")
                else:
                    logger.warning(f"Invalid manifest format or no files found in batch {i // manifest_batch_size + 1}.")
            except MediaServerError as e:
                 logger.error(f"Failed to get manifest batch {i // manifest_batch_size + 1}: {e}")
                 # Decide whether to continue or fail all
                 return {"success": False, "message": f"Failed to get manifest: {e}", "downloaded": 0, "skipped": 0, "failed": total_files}
            except Exception as e:
                 logger.exception(f"Unexpected error getting manifest batch {i // manifest_batch_size + 1}: {e}")
                 capture_media_exception(
                     e,
                     context={
                         "stage": "get_media_manifest",
                         "deck_hash": deck_hash,
                         "batch_index": i // manifest_batch_size,
                         "requested_filenames": batch_filenames,
                     },
                 )
                 return {"success": False, "message": f"Unexpected error getting manifest: {e}", "downloaded": 0, "skipped": 0, "failed": total_files}

        # Deduplicate files from manifest (keep last occurrence to get most recent URL)
        manifest_file_count = len(all_files_to_download)
        seen_filenames = {}
        for file_info in all_files_to_download:
            filename = file_info.get("filename")
            if filename:
                seen_filenames[filename] = file_info  # Overwrites earlier entries with same filename
        
        all_files_to_download = list(seen_filenames.values())
        duplicates_in_manifest = manifest_file_count - len(all_files_to_download)
        if duplicates_in_manifest > 0:
            logger.info(f"Removed {duplicates_in_manifest} duplicate file(s) from manifest response")

        # Phase 2: Download files
        num_to_download = len(all_files_to_download)
        logger.info(f"Manifest retrieved. Attempting to download {num_to_download} files.")
        if num_to_download == 0:
             # This might mean all files existed locally or manifest was empty
             logger.info("No files marked for download based on manifest.")
             # We need to check local existence properly here
             # For now, assume manifest is correct and none needed downloading
             return {"success": True, "message": "No files needed downloading based on manifest.", "downloaded": 0, "skipped": total_files, "failed": 0}


        for i in range(0, num_to_download, download_batch_size):
            download_batch = all_files_to_download[i:i+download_batch_size]
            batch_tasks = []
            files_in_task = []
            seen_in_batch = set()  # Track filenames in current batch to avoid concurrent downloads

            for file_info in download_batch:
                filename = file_info.get("filename")
                download_url = file_info.get("download_url")

                if not filename or not download_url:
                    logger.warning(f"Invalid file info in manifest, skipping: {file_info}")
                    total_failed += 1
                    continue

                # Skip if this filename is already being downloaded in this batch
                if filename in seen_in_batch:
                    logger.warning(f"Duplicate filename {filename} within download batch, skipping duplicate")
                    total_skipped += 1
                    continue
                
                seen_in_batch.add(filename)
                destination = self.media_folder / filename

                # Skip if file already exists and seems valid (basic check)
                if destination.exists() and destination.stat().st_size > 0:
                    # Could add hash check here if manifest provided hashes
                    total_skipped += 1
                    continue

                batch_tasks.append(self.download_file(download_url, destination))
                files_in_task.append(filename) # Keep track of filenames for logging

            # Wait for this download batch to complete
            if batch_tasks:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for idx, result in enumerate(results):
                    task_filename = files_in_task[idx]
                    if isinstance(result, Exception):
                        logger.error(f"Exception downloading file {task_filename}: {result}")
                        exception_context = {
                            "stage": "download_file_batch",
                            "deck_hash": deck_hash,
                            "filename": task_filename,
                            "download_url": download_batch[idx].get("download_url") if idx < len(download_batch) else None,
                        }
                        metadata = getattr(result, "metadata", None)
                        if isinstance(metadata, dict):
                            exception_context.update(metadata)
                        capture_media_exception(result, context=exception_context)
                        total_failed += 1
                    elif not result:
                        logger.error(f"Download function returned False for file {task_filename}")
                        capture_media_message(
                            "download-returned-false",
                            level="warning",
                            context={
                                "stage": "download_file_batch",
                                "deck_hash": deck_hash,
                                "filename": task_filename,
                            },
                        )
                        total_failed += 1
                    else:
                        total_downloaded += 1 # Count successful downloads

            # Update progress after each download batch
            processed_count += len(download_batch)
            if progress_callback:
                # Ensure progress covers the full range even if skipping
                progress_value = processed_count / num_to_download if num_to_download > 0 else 1.0
                progress_callback(progress_value)

        logger.info(f"Download complete. Downloaded: {total_downloaded}, Skipped: {total_skipped}, Failed: {total_failed}")
        return {
            "success": total_failed == 0, # Consider success only if no failures
            "message": f"Downloaded {total_downloaded}, Skipped {total_skipped}, Failed {total_failed}",
            "downloaded": total_downloaded,
            "skipped": total_skipped,
            "failed": total_failed
        }

    @retry(max_tries=3, delay=1)
    async def check_media_bulk(self, user_token:str, deck_hash: str, bulk_operation_id: str, files: List[Dict]) -> Dict:
        if not files:
            return {"existing_files": [], "missing_files": [], "failed_files": [], "batch_id": None}

        semaphore = await self._get_semaphore()
        async with semaphore:
            await self.rate_limiter.wait_if_needed()

            url = f"{self.api_base_url}/media/check/bulk"
            valid_files_info = []
            for f in files:
                if all(k in f for k in ("hash", "filename", "note_guid", "file_size")):
                    valid_files_info.append(f)
                else:
                    logger.warning(f"Skipping invalid file info in check_media_bulk: {f}")

            if not valid_files_info:
                 logger.error("No valid file info provided to check_media_bulk.")
                 # Return empty structure matching expected format
                 return {"existing_files": [], "missing_files": [], "failed_files": [], "batch_id": None}


            data = {"token": user_token, "deck_hash": deck_hash, "files": valid_files_info, "bulk_operation_id": bulk_operation_id}
            logger.debug(f"Bulk Operation ID: {bulk_operation_id}")
            try:
                response = await self.async_request("post", url, json=data)
                # No need for raise_for_status here, async_request does it
                json_response = response.json()
                # Ensure expected keys exist, provide defaults
                return {
                    "existing_files": json_response.get("existing_files", []),
                    "missing_files": json_response.get("missing_files", []),
                    "failed_files": json_response.get("failed_files", []), # Add failed files key
                    "batch_id": json_response.get("batch_id")
                }
            except requests.HTTPError as e:
                status = e.response.status_code
                text = e.response.text
                logger.error(f"Check Media Bulk: HTTP error {status}: {text}")
                exc = MediaServerError(f"Server error {status} checking media: {text}")
                setattr(exc, "metadata", {
                    "stage": "check_media_bulk",
                    "deck_hash": deck_hash,
                    "bulk_operation_id": bulk_operation_id,
                    "status_code": status,
                    "file_count": len(valid_files_info),
                })
                raise exc from e
            except (requests.RequestException, ValueError, KeyError) as e:
                logger.error(f"Check Media Bulk: Network or parsing error: {str(e)}")
                exc = MediaServerError(f"Network or parsing error checking media: {str(e)}")
                setattr(exc, "metadata", {
                    "stage": "check_media_bulk",
                    "deck_hash": deck_hash,
                    "bulk_operation_id": bulk_operation_id,
                    "error_type": type(e).__name__,
                })
                raise exc from e

    async def optimize_media_for_upload(self, file_note_pairs: List[Tuple[str, str]], progress_callback=None) -> Tuple[Dict[str, str], List[Dict], Dict[str, str]]:
        # Ensure media_optimizer is available
        try:
            from . import media_optimizer
        except ImportError:
            logger.error("media_optimizer module not found. Cannot optimize media.")
            return {}, [], {}

        files_info = []
        file_paths = {}
        filename_mapping = {}

        if not file_note_pairs:
            return filename_mapping, files_info, file_paths

        base_dir = mw.col.media.dir() if mw.col else None
        if not base_dir:
             logger.error("Anki media directory not found. Cannot process media.")
             return {}, [], {}

        svg_files = []
        regular_files = []

        # Simple file categorization - all files get sanitized filenames
        logger.info(f"Starting file categorization for {len(file_note_pairs)} files...")
        if progress_callback:
            progress_callback(0.05)  # 5% - started categorization

        for filename, note_guid in file_note_pairs:
            if not filename:
                continue
            
            filepath = os.path.join(base_dir, filename)
            filepath_obj = Path(filepath)

            if not filepath_obj.exists() or not self._is_allowed_file_type(filepath):
                continue

            try:
                file_size = filepath_obj.stat().st_size
            except OSError:
                continue

            # Reject files exceeding size limit early
            if file_size > MAX_FILE_SIZE:
                logger.warning(f"File exceeds size limit ({MAX_FILE_SIZE} bytes), skipping: {filename} ({file_size} bytes)")
                continue

            # Categorize by type - all files get filename sanitization
            if filepath_obj.suffix.lower() == '.svg':
                svg_files.append((filename, filepath_obj, note_guid))
            else:
                # All image files (including WebP, JPEG, PNG, etc.) go through regular processing
                regular_files.append((filename, filepath_obj, note_guid))

        logger.info(f"Categorization complete: {len(svg_files)} SVGs, {len(regular_files)} regular files")

        # Process SVGs in batch
        if svg_files:
            logger.info(f"Processing {len(svg_files)} SVG files...")
            if progress_callback: progress_callback(0.30)
                
            svg_optimize_list = [(fname, fp) for fname, fp, _ in svg_files]
            try:
                svg_optimized = await media_optimizer.optimize_svg_files(svg_optimize_list)
            except Exception as e:
                logger.exception(f"Error during SVG optimization batch: {e}")
                capture_media_exception(
                    e,
                    context={
                        "stage": "optimize_svg_files",
                        "file_count": len(svg_optimize_list),
                    },
                )
                svg_optimized = {}

            for filename, filepath_obj, note_guid in svg_files:
                if filename in svg_optimized:
                    opt_filepath, exp_hash, was_optimized = svg_optimized[filename]
                    if was_optimized and exp_hash:
                        try:
                            file_hash, file_size = await self.compute_file_hash_and_size(opt_filepath)
                            if file_hash == exp_hash and file_size <= MAX_FILE_SIZE:
                                files_info.append({"hash": file_hash, "filename": filename, "note_guid": note_guid, "file_size": file_size})
                                file_paths[file_hash] = str(opt_filepath)
                        except Exception as e:
                            logger.error(f"Error processing optimized SVG {filename}: {e}")

        # Process regular files in parallel batches
        if regular_files:
            logger.info(f"Processing {len(regular_files)} regular files...")
            
            # Simplified batch sizing - the real bottleneck is image optimization, not our code
            cpu_count = os.cpu_count() or 2
            if cpu_count <= 4:
                batch_size = 50
            else:
                batch_size = 75
            
            # For very large sets, slightly reduce to prevent memory issues
            if len(regular_files) > 10000:
                batch_size = max(25, batch_size // 2)
            
            logger.info(f"Using batch size of {batch_size} (CPU cores: {cpu_count})")
            
            for i in range(0, len(regular_files), batch_size):
                batch = regular_files[i:i+batch_size]
                batch_num = i//batch_size + 1
                total_batches = (len(regular_files) + batch_size - 1)//batch_size
                
                # Log every 10 batches or significant milestones for large sets
                if batch_num % 10 == 1 or batch_num == total_batches:
                    logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} files)")
                else:
                    logger.debug(f"Processing batch {batch_num}/{total_batches} ({len(batch)} files)")
                
                # Update progress for this batch
                if progress_callback:
                    # Progress from 30% to 90% based on batch completion
                    batch_progress = 0.3 + (0.6 * batch_num / total_batches)
                    progress_callback(batch_progress)
                
                # Process batch in parallel
                batch_tasks = []
                for filename, filepath_obj, note_guid in batch:
                    task = self._process_regular_file(filename, filepath_obj, note_guid, media_optimizer)
                    batch_tasks.append(task)
                
                # Wait for batch to complete
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                
                # Process batch results
                for idx, result in enumerate(batch_results):
                    filename, filepath_obj, note_guid = batch[idx]
                    
                    if isinstance(result, Exception):
                        logger.error(f"Exception processing file {filename}: {result}")
                        continue
                    
                    if result is None:  # File was skipped or failed
                        continue
                    
                    # Result should be a tuple of (file_info, file_path_entry, mapping_entry)
                    if not isinstance(result, tuple) or len(result) != 3:
                        logger.error(f"Invalid result format for file {filename}: {result}")
                        continue
                    
                    file_info, file_path_entry, mapping_entry = result
                    
                    if file_info:
                        files_info.append(file_info)
                    if file_path_entry:
                        file_hash, file_path = file_path_entry
                        file_paths[file_hash] = file_path
                    if mapping_entry:
                        old_name, new_name = mapping_entry
                        filename_mapping[old_name] = new_name

        logger.info(f"Media optimization complete. Mapping: {len(filename_mapping)}, Files to check: {len(files_info)}")
        if progress_callback:
            progress_callback(1.0)  # 100% - optimization complete
        return filename_mapping, files_info, file_paths

    async def _process_regular_file(self, filename: str, filepath_obj: Path, note_guid: str, media_optimizer) -> Optional[Tuple]:
        """Process a single regular file for optimization. Returns tuple of (file_info, file_path_entry, mapping_entry) or None if failed."""
        try:
            # Check cache first to avoid re-optimization
            cache_key = str(filepath_obj)
            try:
                mtime = filepath_obj.stat().st_mtime
                file_size = filepath_obj.stat().st_size
                cached_entry = self.optimization_cache.get(cache_key)
                
                # Enhanced cache key includes file size for better cache hits
                enhanced_cache_key = f"{cache_key}:{file_size}:{mtime}"
                if cached_entry and cached_entry[0] == mtime and len(cached_entry) > 4 and cached_entry[4] == file_size:
                    # Cache hit - use cached result
                    logger.debug(f"Using cached optimization result for {filename}")
                    cached_mtime, opt_filepath, current_filename, was_optimized, cached_size = cached_entry
                    
                    # Verify cached results are valid
                    if opt_filepath is None or current_filename is None or not opt_filepath or not current_filename:
                        logger.warning(f"Invalid cached data for {filename}, re-optimizing (opt_filepath={opt_filepath}, current_filename={current_filename})")
                        # Clear bad cache entry and fall through to re-optimization
                        del self.optimization_cache[cache_key]
                    elif Path(opt_filepath).exists():
                        # Verify cached file still exists
                        mapping_entry = None
                        if filename != current_filename:
                            mapping_entry = (filename, current_filename)
                            filename = current_filename

                        file_hash, file_size = await self.compute_file_hash_and_size(opt_filepath)

                        if file_size > MAX_FILE_SIZE:
                            logger.warning(f"Cached file (optimized: {was_optimized}) exceeds size limit: {opt_filepath} ({file_size} bytes)")
                            return None

                        file_info = {"hash": file_hash, "filename": filename, "note_guid": note_guid, "file_size": file_size}
                        file_path_entry = (file_hash, str(opt_filepath))
                        
                        return (file_info, file_path_entry, mapping_entry)
                    else:
                        logger.debug(f"Cached file no longer exists for {filename}, re-optimizing")
            except OSError:
                # File might have been deleted or changed, proceed with optimization
                pass
            
            # Cache miss or file changed - perform optimization
            opt_filepath, current_filename, was_optimized = await media_optimizer.optimize_media_file(filename, filepath_obj)
            
            # Check if optimization failed
            if opt_filepath is None or current_filename is None or not opt_filepath or not current_filename:
                logger.warning(f"Optimization failed for file {filename}, skipping (opt_filepath={opt_filepath}, current_filename={current_filename})")
                return None
            
            # Cache the optimization result with enhanced data
            try:
                mtime = filepath_obj.stat().st_mtime
                file_size = filepath_obj.stat().st_size
                self.optimization_cache[cache_key] = (mtime, opt_filepath, current_filename, was_optimized, file_size)
            except OSError:
                # File might have been deleted during optimization
                pass

            mapping_entry = None
            if filename != current_filename:
                mapping_entry = (filename, current_filename)
                # Use the new filename for hash calculation etc.
                filename = current_filename

            # Use the potentially optimized filepath for hash/size
            file_hash, file_size = await self.compute_file_hash_and_size(opt_filepath)

            if file_size > MAX_FILE_SIZE:
                logger.warning(f"File (optimized: {was_optimized}) exceeds size limit: {opt_filepath} ({file_size} bytes)")
                return None

            file_info = {"hash": file_hash, "filename": filename, "note_guid": note_guid, "file_size": file_size}
            file_path_entry = (file_hash, str(opt_filepath))
            
            return (file_info, file_path_entry, mapping_entry)

        except (MediaHashError, FileNotFoundError, OSError) as e:
            logger.error(f"Error preparing file {filename}: {str(e)}")
            return None
        except Exception as e:
             logger.exception(f"Unexpected error preparing file {filename}: {e}")
             capture_media_exception(
                 e,
                 context={
                     "stage": "optimize_regular_file",
                     "filename": filename,
                     "note_guid": note_guid,
                 },
             )
             return None

    async def upload_media_bulk(self, user_token: str, files_info: List[Dict], file_paths: Dict[str, str], deck_hash: str, bulk_operation_id: str, progress_callback=None) -> Dict:
        total_initial_files = len(files_info)
        if not files_info:
            logger.warning("upload_media_bulk called with no files_info.")
            return {"success": True, "status": "no_files", "message": "No files provided for upload.", "uploaded": 0, "existing": 0, "failed": 0, "failed_filenames": []}

        existing_count = 0
        check_failed_count = 0
        missing_files_to_upload: List[Dict] = []
        batch_id: Optional[str] = None
        failed_filenames_set: set[str] = set()
        failed_hashes_set: set[str] = set()
        check_failed_hashes: set[str] = set()
        hash_to_filename = {}
        for f in files_info:
            h = f.get("hash")
            fn = f.get("filename")
            if isinstance(h, str) and isinstance(fn, str):
                hash_to_filename[h] = fn

        # Phase 1: server check
        try:
            logger.info(f"Checking {total_initial_files} files with server...")
            if progress_callback:
                progress_callback(0.1)

            bulk_check_result = await self.check_media_bulk(user_token, deck_hash, bulk_operation_id, files_info)
            existing_files = bulk_check_result.get("existing_files", []) or []
            existing_count = len(existing_files)
            check_failed_files = bulk_check_result.get("failed_files", []) or []
            for entry in check_failed_files:
                if isinstance(entry, dict):
                    hash_val = entry.get("hash")
                    if isinstance(hash_val, str):
                        check_failed_hashes.add(hash_val)
                elif isinstance(entry, str):
                    check_failed_hashes.add(entry)
            check_failed_count = len(check_failed_hashes) if check_failed_hashes else len(check_failed_files)
            if check_failed_count:
                logger.warning(f"{check_failed_count} files failed server-side check: {check_failed_files}")
                for entry in check_failed_files:
                    if isinstance(entry, dict):
                        fn = entry.get("filename") or hash_to_filename.get(entry.get("hash"))
                        hash_val = entry.get("hash")
                        if isinstance(hash_val, str):
                            failed_hashes_set.add(hash_val)
                        if fn:
                            failed_filenames_set.add(fn)
                    elif isinstance(entry, str):
                        fn = hash_to_filename.get(entry, entry)
                        failed_filenames_set.add(fn)
                        failed_hashes_set.add(entry)
            failed_hashes_set.update(check_failed_hashes)

            missing_files_to_upload = bulk_check_result.get("missing_files", []) or []
            batch_id = bulk_check_result.get("batch_id")

            logger.info(f"Check complete. Existing: {existing_count}, Failed Check: {check_failed_count}, To Upload: {len(missing_files_to_upload)}")
            if progress_callback:
                progress_callback(0.3)

            if not missing_files_to_upload:  # Nothing to upload further
                status = "all_exist_or_failed_check"
                message = f"{existing_count} files already exist, {check_failed_count} failed server check."
                success = check_failed_count == 0
                return {"success": success, "status": status, "message": message, "uploaded": 0, "existing": existing_count, "failed": check_failed_count, "failed_filenames": sorted(failed_filenames_set)}

            if not batch_id:
                logger.error("Server did not provide batch_id for upload.")
                for f in missing_files_to_upload:  # treat as failed
                    fn = f.get("filename")
                    h = f.get("hash")
                    if isinstance(h, str):
                        failed_hashes_set.add(h)
                    if isinstance(fn, str):
                        failed_filenames_set.add(fn)
                return {"success": False, "status": "no_batch_id", "message": "Server rejected upload batch.", "uploaded": 0, "existing": existing_count, "failed": len(failed_hashes_set), "failed_filenames": sorted(failed_filenames_set)}
        except MediaServerError as e:
            logger.error(f"Server error during media check: {e}")
            capture_media_exception(
                e,
                context={
                    "stage": "check_media_bulk",
                    "deck_hash": deck_hash,
                    "bulk_operation_id": bulk_operation_id,
                    "file_count": total_initial_files,
                },
            )
            return {"success": False, "status": "check_failed", "message": f"Server error checking files: {e}", "uploaded": 0, "existing": 0, "failed": total_initial_files, "failed_filenames": list(hash_to_filename.values())}
        except Exception as e:
            logger.exception(f"Unexpected error during media check: {e}")
            capture_media_exception(
                e,
                context={
                    "stage": "check_media_bulk",
                    "deck_hash": deck_hash,
                    "bulk_operation_id": bulk_operation_id,
                    "file_count": total_initial_files,
                },
            )
            return {"success": False, "status": "check_failed_unexpected", "message": f"Unexpected error checking files: {e}", "uploaded": 0, "existing": 0, "failed": total_initial_files, "failed_filenames": list(hash_to_filename.values())}

        # Phase 2: Upload missing
        uploaded_hashes: List[str] = []
        upload_failed_hashes: set[str] = set()
        num_to_upload = len(missing_files_to_upload)
        upload_batch_size = 10
        logger.info(f"Starting upload for {num_to_upload} files...")
        for i in range(0, num_to_upload, upload_batch_size):
            batch = missing_files_to_upload[i:i+upload_batch_size]
            upload_tasks = []
            hashes_in_batch: List[str] = []
            files_for_tasks: List[str] = []
            for file_info in batch:
                file_hash = file_info.get("hash")
                upload_url = file_info.get("upload_url") or file_info.get("presigned_url")
                if not isinstance(file_hash, str) or not isinstance(upload_url, str):
                    logger.warning(f"Missing hash or upload URL in file info, skipping upload: {file_info}")
                    upload_failed_hashes.add((file_hash or "unknown"))
                    if isinstance(file_hash, str):
                        failed_hashes_set.add(file_hash)
                    continue
                filepath_str = file_paths.get(file_hash)
                if not filepath_str or not Path(filepath_str).exists():
                    logger.error(f"Local file path missing or file not found for hash {file_hash}, skipping upload.")
                    if isinstance(file_hash, str):
                        upload_failed_hashes.add(file_hash)
                        failed_hashes_set.add(file_hash)
                    continue
                hashes_in_batch.append(file_hash)
                files_for_tasks.append(file_hash)
                upload_tasks.append(
                    self.upload_file(upload_url, Path(filepath_str), file_hash=file_hash)
                )
            if upload_tasks:
                logger.debug(f"Attempting S3 upload sub-batch {i//upload_batch_size + 1}. Hashes: {hashes_in_batch}")
                try:
                    batch_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
                    logger.debug(f"Completed S3 upload sub-batch for hashes: {hashes_in_batch}")
                    for idx, result in enumerate(batch_results):
                        current_hash = files_for_tasks[idx]
                        if isinstance(result, Exception):
                            logger.error(f"Failed to upload file {current_hash}: {type(result).__name__}: {result}")
                            exception_context = {
                                "stage": "upload_media_file",
                                "file_hash": current_hash,
                                "filename": hash_to_filename.get(current_hash),
                                "deck_hash": deck_hash,
                                "bulk_operation_id": bulk_operation_id,
                            }
                            metadata = getattr(result, "metadata", None)
                            if isinstance(metadata, dict):
                                exception_context.update(metadata)
                            capture_media_exception(result, context=exception_context)
                            upload_failed_hashes.add(current_hash)
                            failed_hashes_set.add(current_hash)
                        elif not result:
                            logger.error(f"Upload function returned False for file {current_hash}")
                            capture_media_message(
                                "upload-returned-false",
                                level="warning",
                                context={
                                    "stage": "upload_media_file",
                                    "file_hash": current_hash,
                                    "filename": hash_to_filename.get(current_hash),
                                    "deck_hash": deck_hash,
                                    "bulk_operation_id": bulk_operation_id,
                                },
                            )
                            upload_failed_hashes.add(current_hash)
                            failed_hashes_set.add(current_hash)
                        else:
                            uploaded_hashes.append(current_hash)
                except Exception as e:
                    logger.exception(f"Unexpected error during asyncio.gather for S3 uploads (hashes {hashes_in_batch}): {e}")
                    capture_media_exception(
                        e,
                        context={
                            "stage": "upload_media_batch",
                            "hashes": hashes_in_batch,
                            "deck_hash": deck_hash,
                            "bulk_operation_id": bulk_operation_id,
                        },
                    )
                    for h in hashes_in_batch:
                        upload_failed_hashes.add(h)
                        failed_hashes_set.add(h)
            if progress_callback:
                progress_value = 0.3 + (0.6 * min(i + upload_batch_size, num_to_upload) / num_to_upload)
                progress_callback(progress_value)

        uploaded_hashes_unique = list(dict.fromkeys(uploaded_hashes))
        final_uploaded_count = len(uploaded_hashes_unique)
        final_upload_failed_count = len(upload_failed_hashes)
        failed_hashes_set.update(upload_failed_hashes)
        total_failed_count = len(failed_hashes_set)
        logger.info(f"Upload phase complete. Succeeded: {final_uploaded_count}, Failed during upload: {final_upload_failed_count}")
        for fh in upload_failed_hashes:
            fn = hash_to_filename.get(fh)
            if fn:
                failed_filenames_set.add(fn)

        if uploaded_hashes_unique:
            try:
                logger.info(f"Confirming {final_uploaded_count} uploaded files with server (Batch ID: {batch_id})...")
                if progress_callback:
                    progress_callback(0.95)
                await self.confirm_media_bulk_upload(batch_id, bulk_operation_id, uploaded_hashes_unique)
                logger.info("Bulk upload confirmed successfully.")
                if progress_callback:
                    progress_callback(1.0)
                failed_list = sorted(failed_filenames_set)
                summary_context = {
                    "stage": "upload_media_bulk",
                    "deck_hash": deck_hash,
                    "bulk_operation_id": bulk_operation_id,
                    "uploaded": final_uploaded_count,
                    "existing": existing_count,
                    "failed": total_failed_count,
                    "failed_filenames_sample": failed_list[:50],
                    "failed_filenames_total": len(failed_list),
                }
                if total_failed_count:
                    capture_media_message(
                        "media-upload-partial-failures",
                        level="warning",
                        context=summary_context,
                    )
                return {"success": total_failed_count == 0, "status": "uploaded_confirmed", "message": f"Uploaded {final_uploaded_count} files ({existing_count} existing, {total_failed_count} failed).", "uploaded": final_uploaded_count, "existing": existing_count, "failed": total_failed_count, "failed_filenames": sorted(failed_filenames_set)}
            except MediaServerError as e:
                logger.error(f"Server error confirming bulk upload: {e}")
                failed_hashes_set.update(uploaded_hashes_unique)
                total_failed_count = len(failed_hashes_set)
                for fh in uploaded_hashes:
                    fn = hash_to_filename.get(fh)
                    if fn:
                        failed_filenames_set.add(fn)
                hashes_sample = uploaded_hashes[:50]
                capture_media_exception(
                    e,
                    context={
                        "stage": "confirm_media_bulk_upload",
                        "batch_id": batch_id,
                        "bulk_operation_id": bulk_operation_id,
                        "uploaded_hashes_sample": hashes_sample,
                        "uploaded_hashes_total": len(uploaded_hashes),
                        "deck_hash": deck_hash,
                    },
                )
                return {"success": False, "status": "confirmation_failed", "message": f"Upload confirmation failed: {e}", "uploaded": 0, "existing": existing_count, "failed": total_failed_count, "error": str(e), "failed_filenames": sorted(failed_filenames_set)}
            except Exception as e:
                logger.exception(f"Unexpected error confirming bulk upload: {e}")
                capture_media_exception(
                    e,
                    context={
                        "stage": "confirm_media_bulk_upload",
                        "batch_id": batch_id,
                        "bulk_operation_id": bulk_operation_id,
                        "uploaded_hashes_sample": uploaded_hashes[:50],
                        "uploaded_hashes_total": len(uploaded_hashes),
                        "deck_hash": deck_hash,
                    },
                )
                failed_hashes_set.update(uploaded_hashes_unique)
                total_failed_count = len(failed_hashes_set)
                for fh in uploaded_hashes:
                    fn = hash_to_filename.get(fh)
                    if fn:
                        failed_filenames_set.add(fn)
                return {"success": False, "status": "confirmation_failed_unexpected", "message": f"Unexpected error confirming upload: {e}", "uploaded": 0, "existing": existing_count, "failed": total_failed_count, "error": str(e), "failed_filenames": sorted(failed_filenames_set)}
        else:
            if progress_callback:
                progress_callback(1.0)
            logger.warning("No files were successfully uploaded.")
            failed_list = sorted(failed_filenames_set)
            capture_media_message(
                "media-upload-all-failed",
                level="error",
                context={
                    "stage": "upload_media_bulk",
                    "deck_hash": deck_hash,
                    "bulk_operation_id": bulk_operation_id,
                    "existing": existing_count,
                    "failed": total_failed_count,
                    "failed_filenames_sample": failed_list[:50],
                    "failed_filenames_total": len(failed_list),
                },
            )
            return {"success": total_failed_count == 0, "status": "upload_failed_all", "message": f"No files uploaded ({existing_count} existing, {total_failed_count} failed).", "uploaded": 0, "existing": existing_count, "failed": total_failed_count, "failed_filenames": sorted(failed_filenames_set)}


    @retry(max_tries=3, delay=1)
    async def confirm_media_bulk_upload(self, batch_id: str, bulk_operation_id: str, confirmed_files: List[str]) -> None: # Return None on success
        if not confirmed_files:
             logger.warning("confirm_media_bulk_upload called with no files to confirm.")
             return # Nothing to do

        semaphore = await self._get_semaphore()
        async with semaphore:
            await self.rate_limiter.wait_if_needed()

            url = f"{self.api_base_url}/media/confirm/bulk"
            data = {"batch_id": batch_id, "confirmed_files": confirmed_files, "bulk_operation_id": bulk_operation_id}

            try:
                response = await self.async_request("post", url, json=data)
                # async_request already raises for bad status codes
                logger.info(f"Successfully confirmed {len(confirmed_files)} files for batch {batch_id}")
                return # Explicitly return None on success

            except requests.HTTPError as e:
                status = e.response.status_code
                text = e.response.text
                logger.error(f"Confirm Media Bulk: HTTP error {status}: {text}")
                exc = MediaServerError(f"Server error {status} confirming upload: {text}")
                setattr(exc, "metadata", {
                    "stage": "confirm_media_bulk_upload",
                    "batch_id": batch_id,
                    "bulk_operation_id": bulk_operation_id,
                    "status_code": status,
                    "confirmed_count": len(confirmed_files),
                })
                raise exc from e
            except requests.RequestException as e:
                logger.error(f"Confirm Media Bulk: Network error: {str(e)}")
                exc = MediaServerError(f"Network error confirming upload: {str(e)}")
                setattr(exc, "metadata", {
                    "stage": "confirm_media_bulk_upload",
                    "batch_id": batch_id,
                    "bulk_operation_id": bulk_operation_id,
                    "error_type": type(e).__name__,
                    "confirmed_count": len(confirmed_files),
                })
                raise exc from e
            except Exception as e:
                 logger.exception(f"Unexpected error during confirm_media_bulk_upload: {e}")
                 capture_media_exception(
                     e,
                     context={
                         "stage": "confirm_media_bulk_upload",
                         "batch_id": batch_id,
                         "bulk_operation_id": bulk_operation_id,
                         "confirmed_count": len(confirmed_files),
                     },
                 )
                 raise MediaServerError(f"Unexpected error confirming upload: {e}") from e