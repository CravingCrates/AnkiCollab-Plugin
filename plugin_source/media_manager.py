import asyncio
import hashlib
import logging
import mimetypes
import os
import time
import requests
import base64
import binascii
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, cast
from concurrent.futures import ThreadPoolExecutor
from asyncio import Lock as AsyncLock, get_running_loop # Use asyncio Lock

from aqt import mw

logger = logging.getLogger("ankicollab")

# --- Add File Logging ---
# log_file_path = os.path.join(os.path.dirname(__file__), 'ankicollab_media_errors.log')
# file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
# file_handler.setLevel(logging.DEBUG)
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# file_handler.setFormatter(formatter)
# if not any(isinstance(h, logging.FileHandler) and h.baseFilename == file_handler.baseFilename for h in logger.handlers):
#     logger.addHandler(file_handler)
# --- End File Logging ---


MAX_REQUESTS_PER_MINUTE = 50
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
def retry(max_tries=3, delay=1, backoff=2, exceptions=(requests.RequestException, TimeoutError, MediaServerError)):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            mtries, mdelay = max_tries, delay
            last_exception = None
            while mtries > 0:
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    # Detect rate limiting (HTTP 429) and increase delay
                    if isinstance(e, requests.HTTPError) and e.response.status_code == 429:
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            mdelay = max(mdelay, int(retry_after))
                        logger.warning(f"Rate limited (429). Retrying in {mdelay}s...")
                    elif isinstance(e, MediaServerError):
                         # Don't retry on all server errors, maybe specific ones? For now, retry.
                         logger.warning(f"Server error encountered: {str(e)}. Retrying in {mdelay}s... ({mtries-1} tries left)")
                    else:
                         logger.warning(f"Request failed: {str(e)}. Retrying in {mdelay}s... ({mtries-1} tries left)")

                    await asyncio.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff

            logger.error(f"Maximum retries reached for {func.__name__}. Last error: {last_exception}")
            # Re-raise the last exception encountered
            if last_exception:
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


        if "timeout" not in kwargs:
            kwargs["timeout"] = REQUEST_TIMEOUT

        self.session.verify = VERIFY_SSL

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
            if filepath.stat().st_size > MAX_FILE_SIZE:
                logger.warning(f"File exceeds size limit ({MAX_FILE_SIZE} bytes): {filepath}")
                return False
            if not self._is_allowed_file_type(filepath):
                logger.warning(f"File type not allowed: {filepath.suffix}")
                return False
            
            with open(filepath, "rb") as f:
                header = f.read(16)
                ext = filepath.suffix.lower()
                if ext in ['.jpg', '.jpeg'] and not header.startswith(b'\xFF\xD8\xFF'):
                    logger.warning(f"Invalid JPEG signature for file: {filepath}")
                    return False
                elif ext == '.png' and not header.startswith(b'\x89PNG\r\n\x1A\n'):
                    logger.warning(f"Invalid PNG signature for file: {filepath}")
                    return False
                elif ext == '.gif' and not (header.startswith(b'GIF87a') or header.startswith(b'GIF89a')):
                    logger.warning(f"Invalid GIF signature for file: {filepath}")
                    return False
                elif ext == '.webp' and not header.startswith(b'RIFF'):
                    logger.warning(f"Invalid WebP signature for file: {filepath}")
                    return False
                elif ext == '.mp3' and not header.startswith(b'ID3') and not header.startswith(b'\xFF\xFB') and not header.startswith(b'\xFF\xF3') and not header.startswith(b'\xFF\xF2'):
                    logger.warning(f"Invalid MP3 signature for file: {filepath}")
                    return False
                elif ext == '.ogg' and not header.startswith(b'OggS'):
                    logger.warning(f"Invalid OGG signature for file: {filepath}")
                    return False
            return True
        except Exception as e:
            logger.error(f"Error validating file {filepath}: {str(e)}")
            return False

    @retry(max_tries=3, delay=2, backoff=3)
    async def upload_file(self, presigned_url: str, filepath: Union[str, Path], file_hash: Optional[str] = None) -> bool:
        filepath = Path(filepath)

        if not filepath.exists() or not filepath.is_file():
            raise FileNotFoundError(f"Missing file for upload: {filepath}")
        if filepath.stat().st_size > MAX_FILE_SIZE:
            raise MediaTypeError(f"File exceeds size limit: {filepath.stat().st_size} bytes")
        if not self.validate_file_basic(filepath):
            raise MediaTypeError(f"File failed basic validation: {filepath}")

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

                binary_hash = binascii.unhexlify(file_hash)
                base64_md5 = base64.b64encode(binary_hash).decode('ascii')
                headers = {"Content-Type": content_type, "Content-MD5": base64_md5}

                # File upload is I/O intensive - use thread pool
                def do_upload():
                    try:
                        with open(filepath, "rb") as f:
                            # Read file content once
                            file_content = f.read()
                            if len(file_content) != filepath.stat().st_size:
                                 # Basic check if file changed during read
                                 raise IOError(f"File size changed during read: {filepath}")

                        response = self.session.put(
                            presigned_url,
                            data=file_content, # Use content read previously
                            headers=headers,
                            timeout=REQUEST_TIMEOUT * 2 # Increase timeout for upload?
                        )
                        response.raise_for_status()
                        return response
                    except requests.exceptions.RequestException as req_exc:
                         logger.error(f"Network error during S3 PUT for {filepath}: {req_exc}")
                         raise # Re-raise
                    except IOError as io_err:
                         logger.error(f"IOError during S3 PUT preparation for {filepath}: {io_err}")
                         raise # Re-raise to be caught by outer try/except

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
                logger.error(f"HTTP error {status_code} uploading to S3 ({filepath}): {error_text}")
                # Raise specific errors based on status code
                if status_code == 403: raise MediaUploadError(f"Permission denied (403): {error_text}") from e
                if status_code == 413: raise MediaTypeError(f"File too large (413): {error_text}") from e
                if status_code == 400: raise MediaUploadError(f"Bad request (400): {error_text}") from e
                
                raise MediaUploadError(f"S3 upload failed with HTTP {status_code}: {error_text}") from e
            except requests.RequestException as e:
                logger.error(f"Network error during upload ({filepath}): {str(e)}")
                raise MediaUploadError(f"Network error during upload: {str(e)}") from e
            except (IOError, MediaHashError) as e: # Catch errors from MD5 calc or do_upload prep
                logger.error(f"I/O or Hash error preparing upload for {filepath}: {str(e)}")
                raise MediaUploadError(f"I/O or Hash error: {str(e)}") from e
            except Exception as e:
                 # Catch unexpected errors
                 logger.exception(f"Unexpected error during upload_file for {filepath}: {e}")
                 raise MediaUploadError(f"Unexpected upload error: {e}") from e


    @retry(max_tries=3, delay=1)
    async def download_file(self, url: str, destination: Union[str, Path]) -> bool:
        destination = Path(destination)
        os.makedirs(destination.parent, exist_ok=True)
        temp_destination = destination.with_suffix(f"{destination.suffix}.tmp.{os.getpid()}") # Make temp name more unique

        try:
            # Download operation is IO bound - use thread pool
            def do_download():
                downloaded_ok = False
                try:
                    with self.session.get(url, stream=True, timeout=REQUEST_TIMEOUT * 2) as response: # Longer timeout for download
                        response.raise_for_status()
                        with open(temp_destination, "wb") as f:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                f.write(chunk)
                        downloaded_ok = True # Mark as successful only if fully written
                    return downloaded_ok
                except requests.exceptions.RequestException as req_exc:
                     logger.error(f"Network error downloading {url}: {req_exc}")
                     return False # Indicate failure
                except IOError as io_err:
                     logger.error(f"IOError writing temporary download file {temp_destination}: {io_err}")
                     return False # Indicate failure

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                 logger.error("download_file called without a running event loop!")
                 raise RuntimeError("No running event loop found for file download")

            success = await loop.run_in_executor(self.thread_executor, do_download)

            if success and temp_destination.exists():
                try:
                    # Attempt atomic rename if possible, otherwise normal rename
                    os.replace(temp_destination, destination) # More atomic on most OS
                except OSError:
                    os.rename(temp_destination, destination) # Fallback
                return True
            else:
                 # Download failed or temp file doesn't exist
                 logger.warning(f"Download failed or temp file missing for {url}")
                 return False

        except Exception as e:
            logger.error(f"Unexpected error downloading file {url}: {str(e)}")
            return False # Indicate failure
        finally:
            # Clean up partial downloads robustly
            if temp_destination.exists():
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
                if status == 401: raise MediaServerError(f"Authorization failed (401): {text}") from e
                if status == 403: raise MediaServerError(f"Permission denied (403): {text}") from e
                if status == 404: raise MediaServerError(f"Deck not found (404): {text}") from e
                if status == 429: raise MediaRateLimitError(f"Rate limit exceeded (429): {text}") from e # Use specific exception
                raise MediaServerError(f"Server error {status} getting manifest: {text}") from e
            except (requests.RequestException, ValueError, KeyError) as e: # Added ValueError/KeyError for bad JSON
                logger.error(f"Error retrieving or parsing manifest data: {str(e)}")
                raise MediaServerError(f"Failed to get or parse server response for media manifest: {str(e)}") from e

    # --- get_media_manifest_and_download (Keep logic, ensure download_file is robust) ---
    @retry(max_tries=3, delay=2)
    async def get_media_manifest_and_download(self, user_token:str, deck_hash: str, filenames: List[str], progress_callback=None) -> Dict:
        total_files = len(filenames)
        if total_files == 0:
            return {"success": True, "message": "No files to download", "downloaded": 0, "skipped": 0, "failed": 0}

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
            batch_filenames = filenames[i:i+manifest_batch_size]
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
                 return {"success": False, "message": f"Unexpected error getting manifest: {e}", "downloaded": 0, "skipped": 0, "failed": total_files}

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

            for file_info in download_batch:
                filename = file_info.get("filename")
                download_url = file_info.get("download_url")

                if not filename or not download_url:
                    logger.warning(f"Invalid file info in manifest, skipping: {file_info}")
                    total_failed += 1
                    continue

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
                        total_failed += 1
                    elif not result:
                        logger.error(f"Download function returned False for file {task_filename}")
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
            print(f"Bulk Operation ID: {bulk_operation_id}")
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
                # Propagate error clearly
                raise MediaServerError(f"Server error {status} checking media: {text}") from e
            except (requests.RequestException, ValueError, KeyError) as e:
                logger.error(f"Check Media Bulk: Network or parsing error: {str(e)}")
                raise MediaServerError(f"Network or parsing error checking media: {str(e)}") from e

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
        small_files_to_hash = []  # Files to hash directly without optimization

        # Simple file categorization - the previous benchmarks show this is already fast
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

            # Handle small files directly without optimization
            if filepath_obj.suffix.lower() == '.webp' and file_size < 200 * 1024:
                small_files_to_hash.append((filename, filepath_obj, note_guid, file_size))
            elif filepath_obj.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff'] and file_size < 100 * 1024:
                small_files_to_hash.append((filename, filepath_obj, note_guid, file_size))
            elif filepath_obj.suffix.lower() == '.svg':
                if file_size <= MAX_FILE_SIZE:
                    svg_files.append((filename, filepath_obj, note_guid))
            else:
                regular_files.append((filename, filepath_obj, note_guid))

        logger.info(f"Categorization complete: {len(svg_files)} SVGs, {len(regular_files)} regular files, {len(small_files_to_hash)} small files")

        # Simple parallel hash computation for small files
        if small_files_to_hash:
            logger.info(f"Processing {len(small_files_to_hash)} small files...")
            
            async def hash_small_file(file_info):
                filename, filepath_obj, note_guid, file_size = file_info
                try:
                    file_hash, _ = await self.compute_file_hash_and_size(filepath_obj)
                    return {
                        "hash": file_hash, 
                        "filename": filename, 
                        "note_guid": note_guid, 
                        "file_size": file_size
                    }, (file_hash, str(filepath_obj))
                except Exception as e:
                    logger.warning(f"Error processing small file {filename}: {e}")
                    return None, None

            # Process in reasonable batches
            batch_size = 100
            for i in range(0, len(small_files_to_hash), batch_size):
                batch = small_files_to_hash[i:i+batch_size]
                hash_tasks = [hash_small_file(file_info) for file_info in batch]
                hash_results = await asyncio.gather(*hash_tasks, return_exceptions=True)
                
                for result in hash_results:
                    if isinstance(result, Exception) or not isinstance(result, tuple) or len(result) != 2:
                        continue
                    file_info_result, file_path_result = result
                    if file_info_result and file_path_result:
                        files_info.append(file_info_result)
                        file_hash, file_path = file_path_result
                        file_paths[file_hash] = file_path

        # Process SVGs in batch
        if svg_files:
            logger.info(f"Processing {len(svg_files)} SVG files...")
            if progress_callback: progress_callback(0.30)
                
            svg_optimize_list = [(fname, fp) for fname, fp, _ in svg_files]
            try:
                svg_optimized = await media_optimizer.optimize_svg_files(svg_optimize_list)
            except Exception as e:
                logger.exception(f"Error during SVG optimization batch: {e}")
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
             return None

    async def upload_media_bulk(self, user_token: str, files_info: List[Dict], file_paths: Dict[str, str], deck_hash: str, bulk_operation_id: str, progress_callback=None) -> Dict:
        total_initial_files = len(files_info)
        if not files_info:
            logger.warning("upload_media_bulk called with no files_info.")
            return {"success": True, "status": "no_files", "message": "No files provided for upload.", "uploaded": 0, "existing": 0, "failed": 0}

        # Phase 1: Check files with server
        existing_count = 0
        check_failed_count = 0
        missing_files_to_upload = []
        batch_id = None

        try:
            logger.info(f"Checking {total_initial_files} files with server...")
            if progress_callback: progress_callback(0.1) # Progress: Checking started

            bulk_check_result = await self.check_media_bulk(user_token, deck_hash, bulk_operation_id, files_info)

            existing_files = bulk_check_result.get("existing_files", [])
            existing_count = len(existing_files)

            check_failed_files = bulk_check_result.get("failed_files", [])
            check_failed_count = len(check_failed_files)
            if check_failed_count > 0:
                 logger.warning(f"{check_failed_count} files failed server-side check: {check_failed_files}")


            missing_files_to_upload = bulk_check_result.get("missing_files", [])
            batch_id = bulk_check_result.get("batch_id")

            logger.info(f"Check complete. Existing: {existing_count}, Failed Check: {check_failed_count}, To Upload: {len(missing_files_to_upload)}")
            if progress_callback: progress_callback(0.3) # Progress: Checking done

            if not missing_files_to_upload:
                status = "all_exist_or_failed_check"
                message = f"{existing_count} files already exist, {check_failed_count} failed server check."
                success = check_failed_count == 0 # Only success if no check failures
                return {"success": success, "status": status, "message": message, "uploaded": 0, "existing": existing_count, "failed": check_failed_count}

            if not batch_id:
                logger.error("Server did not provide batch_id for upload.")
                # Treat all missing as failed upload attempt
                return {"success": False, "status": "no_batch_id", "message": "Server rejected upload batch.", "uploaded": 0, "existing": existing_count, "failed": check_failed_count + len(missing_files_to_upload)}

        except MediaServerError as e:
            logger.error(f"Server error during media check: {e}")
            return {"success": False, "status": "check_failed", "message": f"Server error checking files: {e}", "uploaded": 0, "existing": 0, "failed": total_initial_files}
        except Exception as e:
            logger.exception(f"Unexpected error during media check: {e}")
            return {"success": False, "status": "check_failed_unexpected", "message": f"Unexpected error checking files: {e}", "uploaded": 0, "existing": 0, "failed": total_initial_files}


        # Phase 2: Upload missing files
        uploaded_hashes = []
        upload_failed_hashes = []
        num_to_upload = len(missing_files_to_upload)
        upload_batch_size = 10 # Keep S3 uploads in smaller batches

        logger.info(f"Starting upload for {num_to_upload} files...")

        for i in range(0, num_to_upload, upload_batch_size):
            batch = missing_files_to_upload[i:i+upload_batch_size]
            upload_tasks = []
            hashes_in_batch = []
            files_for_tasks = []

            # Create tasks for this batch
            for file_info in batch:
                file_hash = file_info.get("hash")
                presigned_url = file_info.get("presigned_url")

                if not file_hash or not presigned_url:
                    logger.warning(f"Missing hash or URL in file info, skipping upload: {file_info}")
                    upload_failed_hashes.append(file_hash or "unknown")
                    continue

                filepath_str = file_paths.get(file_hash)
                if not filepath_str or not Path(filepath_str).exists():
                    logger.error(f"Local file path missing or file not found for hash {file_hash}, skipping upload.")
                    upload_failed_hashes.append(file_hash)
                    continue

                hashes_in_batch.append(file_hash)
                files_for_tasks.append(file_hash) # Track hash for result mapping
                # Pass hash to upload_file for MD5 optimization
                task = self.upload_file(presigned_url, Path(filepath_str), file_hash=file_hash)
                upload_tasks.append(task)

            # Process this batch
            if upload_tasks:
                logger.debug(f"Attempting S3 upload sub-batch {i//upload_batch_size + 1}. Hashes: {hashes_in_batch}")
                try:
                    batch_results = await asyncio.gather(*upload_tasks, return_exceptions=True)
                    logger.debug(f"Completed S3 upload sub-batch for hashes: {hashes_in_batch}")

                    for idx, result in enumerate(batch_results):
                        current_hash = files_for_tasks[idx]
                        if isinstance(result, Exception):
                            # Log specific exception type if possible
                            logger.error(f"Failed to upload file {current_hash}: {type(result).__name__}: {result}")
                            upload_failed_hashes.append(current_hash)
                        elif not result: # Should not happen if upload_file raises exceptions
                            logger.error(f"Upload function returned False for file {current_hash}")
                            upload_failed_hashes.append(current_hash)
                        else:
                            # logger.debug(f"Successfully uploaded file {current_hash}")
                            uploaded_hashes.append(current_hash)
                except Exception as e:
                    # This catches errors in asyncio.gather itself, less likely
                    logger.exception(f"Unexpected error during asyncio.gather for S3 uploads (hashes {hashes_in_batch}): {e}")
                    # Mark all in this gather as failed
                    upload_failed_hashes.extend(h for h in hashes_in_batch if h not in uploaded_hashes)

            # Update progress (scaled between 30% and 90%)
            if progress_callback:
                progress_value = 0.3 + (0.6 * min(i + upload_batch_size, num_to_upload) / num_to_upload)
                progress_callback(progress_value)

        # Phase 3: Confirm successful uploads
        final_uploaded_count = len(uploaded_hashes)
        final_upload_failed_count = len(upload_failed_hashes)
        total_failed_count = check_failed_count + final_upload_failed_count

        logger.info(f"Upload phase complete. Succeeded: {final_uploaded_count}, Failed during upload: {final_upload_failed_count}")

        if uploaded_hashes:
            logger.info(f"Confirming {final_uploaded_count} uploaded files with server (Batch ID: {batch_id})...")
            try:
                if progress_callback: progress_callback(0.95) # Progress: Confirming

                await self.confirm_media_bulk_upload(batch_id, bulk_operation_id, uploaded_hashes)

                logger.info("Bulk upload confirmed successfully.")
                if progress_callback: progress_callback(1.0) # Progress: Done

                return {
                    "success": total_failed_count == 0, # Success only if no failures at any stage
                    "status": "uploaded_confirmed",
                    "message": f"Uploaded {final_uploaded_count} files ({existing_count} existing, {total_failed_count} failed).",
                    "uploaded": final_uploaded_count,
                    "existing": existing_count,
                    "failed": total_failed_count,
                }
            except MediaServerError as e:
                logger.error(f"Server error confirming bulk upload: {e}")
                # Confirmation failed, treat uploaded files as failed for summary
                total_failed_count += final_uploaded_count
                return {
                    "success": False,
                    "status": "confirmation_failed",
                    "message": f"Upload confirmation failed: {e}",
                    "uploaded": 0, # None were successfully confirmed
                    "existing": existing_count,
                    "failed": total_failed_count,
                    "error": str(e),
                }
            except Exception as e:
                logger.exception(f"Unexpected error confirming bulk upload: {e}")
                total_failed_count += final_uploaded_count
                return {
                    "success": False,
                    "status": "confirmation_failed_unexpected",
                    "message": f"Unexpected error confirming upload: {e}",
                    "uploaded": 0,
                    "existing": existing_count,
                    "failed": total_failed_count,
                    "error": str(e),
                }
        else:
            # No files were successfully uploaded in Phase 2
            logger.warning("No files were successfully uploaded.")
            if progress_callback: progress_callback(1.0) # Progress: Done (but nothing happened)
            return {
                "success": total_failed_count == 0, # Success only if check failures were 0
                "status": "upload_failed_all",
                "message": f"No files uploaded ({existing_count} existing, {total_failed_count} failed).",
                "uploaded": 0,
                "existing": existing_count,
                "failed": total_failed_count,
            }


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
                raise MediaServerError(f"Server error {status} confirming upload: {text}") from e
            except requests.RequestException as e:
                logger.error(f"Confirm Media Bulk: Network error: {str(e)}")
                raise MediaServerError(f"Network error confirming upload: {str(e)}") from e
            except Exception as e:
                 logger.exception(f"Unexpected error during confirm_media_bulk_upload: {e}")
                 raise MediaServerError(f"Unexpected error confirming upload: {e}") from e