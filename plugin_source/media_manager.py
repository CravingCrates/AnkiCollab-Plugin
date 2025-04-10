import asyncio
import hashlib
import logging
import mimetypes
import os
import time
import requests
import base64
import binascii
import io
import zipfile
import tempfile
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union
from concurrent.futures import ThreadPoolExecutor

import anki
import aqt
from aqt import mw

logger = logging.getLogger("ankicollab")

MAX_REQUESTS_PER_MINUTE = 50
REQUEST_TRACKING_WINDOW = 60  # seconds
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB max file size
CHUNK_SIZE = 16384  # 16 KB for file operations
REQUEST_TIMEOUT = 30  # Seconds
VERIFY_SSL = True

# File type definitions
ALLOWED_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"},
    ##"audio": {".mp3", ".ogg"},
}
ALL_ALLOWED_EXTENSIONS = set().union(*ALLOWED_EXTENSIONS.values())

class MediaError(Exception):
    """Base class for media errors"""
    pass

class MediaServerError(MediaError):
    """Error communicating with media server"""
    pass

class MediaRateLimitError(MediaError):
    """Rate limit exceeded"""
    pass

class MediaTypeError(MediaError):
    """Invalid media type"""
    pass

class MediaHashError(MediaError):
    """Error calculating file hash"""
    pass

class MediaUploadError(MediaError):
    """Error during file upload"""
    pass

class MediaDownloadError(MediaError):
    """Error during file download"""
    pass
    
class RateLimiter:    
    def __init__(self, max_calls: int, period: int):
        """
        Initialize rate limiter
        
        Args:
            max_calls: Maximum number of calls allowed in the period
            period: Time period in seconds
        """
        self.max_calls = max_calls
        self.period = period
        self.calls = []
    
    async def wait_if_needed(self) -> None:
        """
        Wait if the rate limit has been reached
        """
        now = time.time()
        
        # Remove timestamps older than the period
        self.calls = [t for t in self.calls if now - t < self.period]
        
        if len(self.calls) >= self.max_calls:
            # Calculate wait time to satisfy rate limit
            oldest_call = self.calls[0]
            wait_time = self.period - (now - oldest_call)
            if wait_time > 0:
                print(f"Rate limit reached. Waiting for {wait_time:.2f} seconds")
                await asyncio.sleep(wait_time)
                return await self.wait_if_needed()
        
        self.calls.append(now)
        return


def retry(max_tries=3, delay=1, backoff=2, exceptions=(requests.RequestException, TimeoutError)):
    """
    Retry decorator with exponential backoff for async functions
    
    Args:
        max_tries: Maximum number of attempts
        delay: Initial delay between attempts in seconds
        backoff: Factor to increase delay after each attempt
        exceptions: Exceptions that trigger a retry
        
    Returns:
        Decorated function
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            mtries, mdelay = max_tries, delay
            while mtries > 0:
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    # Detect rate limiting (HTTP 429) and increase delay
                    if isinstance(e, requests.HTTPError) and e.response.status_code == 429:
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            mdelay = max(mdelay, int(retry_after))                    
                    logger.warning(f"Request failed: {str(e)} ({e.response.text}). Retrying in {mdelay}s... ({mtries-1} tries left)")
                    await asyncio.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            
            # give up
            logger.error(f"Maximum retries reached for {func.__name__}")
            raise MediaServerError(f"Failed after {max_tries} tries")
        return wrapper
    return decorator

class MediaManager:    
    def __init__(self, api_base_url: str, media_folder: str):
        """
        Initialize the MediaManager.
        
        Args:
            api_base_url: The base URL for the AnkiCollab API
            media_folder: Path to Anki's media folder
        """
        self.api_base_url = api_base_url.rstrip("/")
        self.media_folder = Path(media_folder)
        self.rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE, REQUEST_TRACKING_WINDOW)
        
        if not self.media_folder.exists():
            raise ValueError(f"Media folder not found at: {media_folder}")
        
        self.optimize_images = True
        
        # Create tmp caches to improve performance
        self.hash_cache = {}  # Cache file hashes to avoid redundant calculations
        self.exists_cache = {}  # Cache server-side existence checks
        self.download_cache = {}  # Cache recently downloaded files
        
        self.session = requests.Session()
        self.session.verify = VERIFY_SSL
        
        # Create semaphore to limit concurrent API calls
        self.semaphore = asyncio.Semaphore(15)
        self.thread_executor = ThreadPoolExecutor() # we try default max workers for now and migh need to adjust later
        
        mimetypes.add_type('image/webp', '.webp')  # how the fuck is this not a default mimetype
    
    async def async_request(self, method, url, **kwargs):
        """Use persistent session for all requests"""
        loop = asyncio.get_event_loop()
        
        if "timeout" not in kwargs:
            kwargs["timeout"] = REQUEST_TIMEOUT
        
        def do_request():
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        
        return await loop.run_in_executor(self.thread_executor, do_request)
        
    def set_media_folder(self, media_folder: str):
        """
        Set the media folder.

        Args:
            media_folder: Path to Anki's media folder
        """
        self.media_folder = Path(media_folder)
        if not self.media_folder.exists():
            raise ValueError(f"Media folder not found at: {media_folder}")
        
    async def close(self):
        """Clean up resources"""
        if hasattr(self, 'thread_executor'):
            self.thread_executor.shutdown()
        
        if hasattr(self, 'session'):
            self.session.close()
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
    
    def _is_allowed_file_type(self, filename: str) -> bool:
        """
        Check if the file type is allowed for upload.
        
        Args:
            filename: Filename to check
            
        Returns:
            True if the file type is allowed, False otherwise
        """
        ext = Path(filename).suffix.lower()
        return ext in ALL_ALLOWED_EXTENSIONS
    
    def _get_media_type(self, filename: str) -> Optional[str]:
        """
        Determine the media type based on file extension.
        
        Args:
            filename: Filename to check
            
        Returns:
            Media type (image, audio, video, document) or None if not recognized
        """
        ext = Path(filename).suffix.lower()
        for media_type, extensions in ALLOWED_EXTENSIONS.items():
            if ext in extensions:
                return media_type
        return None
    
    async def compute_file_hash_and_size(self, filepath: Union[str, Path]) -> Tuple[str, int]:
        """
        Compute SHA-256 hash and size of a file with caching.
        
        Args:
            filepath: Path to the file
            
        Returns:
            Tuple of (SHA-256 hash as a hex string, file size in bytes)
            
        Raises:
            MediaHashError: If hash computation fails
        """
        filepath = Path(filepath)
        if not filepath.exists() or not filepath.is_file():
            raise FileNotFoundError(f"Invalid File: {filepath}")
        
        file_size = filepath.stat().st_size
        
        cache_key = str(filepath)
        try:
            mtime = os.path.getmtime(filepath)
            if cache_key in self.hash_cache and self.hash_cache[cache_key][0] == mtime:
                return (self.hash_cache[cache_key][1], file_size)
        except (OSError, IOError):
            raise MediaHashError(f"Failed to get file {filepath}")
            
        try:
            # Hash calculation could be CPU intensive, so run in thread pool
            loop = asyncio.get_event_loop()
            
            def calculate_hash():
                md5_hash = hashlib.md5()
                with open(filepath, "rb") as f:
                    for byte_block in iter(lambda: f.read(CHUNK_SIZE), b""):
                        md5_hash.update(byte_block)
                return md5_hash.hexdigest()
            
            file_hash = await loop.run_in_executor(self.thread_executor, calculate_hash)
            
            # Store in cache with modification time
            try:
                mtime = os.path.getmtime(filepath)
                self.hash_cache[cache_key] = (mtime, file_hash)
            except (OSError, IOError):
                # File might have been deleted
                pass
            return (file_hash, file_size)
            
        except Exception as e:
            logger.error(f"Error computing hash for {filepath}: {str(e)}")
            raise MediaHashError(f"Failed to compute file hash: {str(e)}")

    def validate_file_basic(self, filepath: Path) -> bool:
        """
        Perform basic file validation before upload.
        
        Args:
            filepath: Path to the file
            
        Returns:
            True if file passes basic validation
        """
        try:
            if not filepath.exists():
                return False
                
            if filepath.stat().st_size > MAX_FILE_SIZE:
                logger.warning(f"File exceeds size limit ({MAX_FILE_SIZE} bytes): {filepath}")
                return False
                
            if not self._is_allowed_file_type(filepath):
                logger.warning(f"File type not allowed: {filepath.suffix}")
                return False
                
            with open(filepath, "rb") as f:
                header = f.read(16)
                
                # Basic signature checks for common formats
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
                elif ext == '.webp' and not header.startswith(b'RIFF'): # should be the default for almost all converted files
                    logger.warning(f"Invalid WebP signature for file: {filepath}")
                    return False
                
                
            return True
            
        except Exception as e:
            logger.error(f"Error validating file {filepath}: {str(e)}")
            return False
        
    @retry(max_tries=3, delay=2, backoff=3)
    async def upload_file(self, presigned_url: str, filepath: Union[str, Path], file_hash: str = None) -> bool:
        """
        Upload a file to the presigned S3 URL with enhanced error handling.
        
        Args:
            presigned_url: The presigned URL for upload
            filepath: Path to the file to upload
            file_hash: MD5 hash (hex) of the file if already calculated, otherwise will calculate
                
        Returns:
            True if upload succeeded
        """
        filepath = Path(filepath)
        
        if not filepath.exists() or not filepath.is_file():
            raise FileNotFoundError(f"Missing file for upload: {filepath}")
        
        if filepath.stat().st_size > MAX_FILE_SIZE:
            raise MediaTypeError(f"File exceeds size limit: {filepath.stat().st_size} bytes")
        
        if not self.validate_file_basic(filepath):
            raise MediaTypeError(f"File failed basic validation: {filepath}")
        
        async with self.semaphore:
            try:
                file_ext = Path(filepath).suffix.lower()                
                # Explicit mapping bc i dont trust the mimetypes module
                content_type_map = {
                    '.webp': 'image/webp',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.png': 'image/png',
                    '.gif': 'image/gif',
                    '.svg': 'image/svg+xml',
                    '.bmp': 'image/bmp',
                    '.tif': 'image/tiff',
                    '.tiff': 'image/tiff'
                }
                
                # Use our mapping first, fall back to mimetypes module if not found
                if file_ext in content_type_map:
                    content_type = content_type_map[file_ext]
                else:
                    content_type = mimetypes.guess_type(str(filepath))[0] or "application/octet-stream"
                
                # Calculate md5 if necessary. the fact that s3 doesnt support sha256 but only md5 as default is embarassing
                if file_hash is None:
                    def calculate_md5():
                        import hashlib
                        md5_hash = hashlib.md5()
                        with open(filepath, "rb") as f:
                            for chunk in iter(lambda: f.read(8192), b""):
                                md5_hash.update(chunk)
                        return md5_hash.hexdigest()
                    
                    loop = asyncio.get_event_loop()
                    file_hash = await loop.run_in_executor(self.thread_executor, calculate_md5)
                
                binary_hash = binascii.unhexlify(file_hash)
                base64_md5 = base64.b64encode(binary_hash).decode('ascii')
                    
                headers = {
                    "Content-Type": content_type,
                    "Content-MD5": base64_md5,
                }
                
                # File upload is I/O intensive - use thread pool
                def do_upload():
                    with open(filepath, "rb") as f:
                        response = self.session.put(
                            presigned_url, 
                            data=f.read(),
                            headers=headers,
                            timeout=REQUEST_TIMEOUT
                        )
                        response.raise_for_status()
                        return response
                        
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(self.thread_executor, do_upload)
                
                return True
                
            except requests.HTTPError as e:
                status_code = getattr(e.response, 'status_code', None)
                
                # Handle specific error codes
                if status_code == 403:
                    logger.error(f"Permission denied uploading to S3: {str(e)}")
                    raise MediaUploadError(f"Permission denied: {str(e)}")
                elif status_code == 413:
                    logger.error(f"File too large for S3: {str(e)}")
                    raise MediaTypeError(f"File too large: {str(e)}")
                elif status_code == 400:
                    logger.error(f"Bad request to S3: {str(e)}")
                    raise MediaUploadError(f"Bad request: {str(e)}")
                elif status_code == 500:
                    logger.error(f"Internal server error from S3: {str(e)}")                    
                    raise MediaUploadError(f"Internal server error: {str(e)}")
                else:
                    logger.error(f"HTTP error uploading to S3: {str(e)}")
                    raise MediaUploadError(f"S3 upload failed: {str(e)}")
                    
            except requests.RequestException as e:
                logger.error(f"Network error during upload: {str(e)}")
                raise MediaUploadError(f"Network error during upload: {str(e)}")
            
            except IOError as e:
                logger.error(f"I/O error reading file for upload: {str(e)}")
                raise MediaUploadError(f"I/O error: {str(e)}")
            
    @retry(max_tries=3, delay=1)
    async def download_file(self, url: str, destination: Union[str, Path]) -> bool:
        """
        Download a file from a URL to a destination path.
        
        Args:
            url: URL to download from
            destination: Path to save the file to
            
        Returns:
            True if download succeeded
        """
        destination = Path(destination)
        
        os.makedirs(destination.parent, exist_ok=True)
        
        try:
            temp_destination = destination.with_suffix(f"{destination.suffix}.tmp")
            
            # Download operation is IO bound - use thread pool
            def do_download():
                with self.session.get(url, stream=True) as response:
                    response.raise_for_status()
                    with open(temp_destination, "wb") as f:
                        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                            f.write(chunk)
                return True
                
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self.thread_executor, do_download)
            
            if temp_destination.exists():
                temp_destination.rename(destination)
                return True
                
            return False
            
        except Exception as e:
            logger.error(f"Error downloading file: {str(e)}")
            
            # Clean up partial downloads
            if 'temp_destination' in locals() and temp_destination.exists():
                try:
                    os.unlink(temp_destination)
                except:
                    pass
                    
            return False
    
    @retry(max_tries=3, delay=1)
    async def get_media_manifest(self, user_token: str, deck_hash: str, filenames) -> Dict:
        """
        Get a manifest of all media files for a deck
        
        Args:
            deck_hash: ID of the deck
            filenames: List of filenames to check
            
        Returns:
            Dictionary with manifest info and file list
        """        
        async with self.semaphore:
            await self.rate_limiter.wait_if_needed()
            
            url = f"{self.api_base_url}/media/manifest"
            data = {
                "user_token": user_token,
                "deck_hash": deck_hash,
                "filenames": filenames
            }
            
            try:
                response = await self.async_request(
                    "post", 
                    url, 
                    json=data,
                )
                
                response.raise_for_status()
                return response.json()
                
            except requests.HTTPError as e:
                if e.response.status_code == 403:
                    logger.error(f"Permission denied getting media manifest: {str(e)}")
                    raise MediaServerError(f"Permission denied")
                if e.response.status_code == 404:
                    logger.error(f"Deck not found: {str(e)}")
                    raise MediaServerError(f"404 not found")
                if e.response.status_code == 429:
                    logger.error(f"Rate limit exceeded: {str(e)}")
                    raise MediaServerError(f"Woah this is a lot of requests. Please wait a bit.")
                if e.response.status_code == 401:
                    logger.error(f"Unauthorized: {str(e)}")
                    raise MediaServerError(f"Your Login expired. Please renew it.")
                logger.error(f"Error getting media manifest: {str(e)}")
                raise MediaServerError(f"Failed to get server response for media: {str(e)}")
                
            except (requests.RequestException, KeyError) as e:
                logger.error(f"Error retrieving manifest data: {str(e)}")
                raise MediaServerError(f"Failed to get server response for media: {str(e)}")
    
    @retry(max_tries=3, delay=2)
    async def get_media_manifest_and_download(self, user_token:str, deck_hash: str, filenames, progress_callback=None) -> Dict:
        """
        Get a manifest of files and download missing ones in batches.
        
        Args:
            deck_hash: hash of the deck
            filenames: vec of filenames to download
            progress_callback: function to track progress
            
        Returns:
            Dictionary with download results
        """
        total_files = len(filenames)
        if total_files == 0:
            return {"success": True, "message": "No files to download", "downloaded": 0, "skipped": 0}
        
        manifest_batch_size = 500
        download_batch_size = 10
        total_downloaded = 0
        total_skipped = 0
        processed_count = 0
        
        for i in range(0, total_files, manifest_batch_size):
            # Get a batch of filenames
            batch_filenames = filenames[i:i+manifest_batch_size]
            
            # Get manifest for this batch only
            manifest_data = await self.get_media_manifest(user_token, deck_hash, batch_filenames)
            
            if not manifest_data or "files" not in manifest_data or manifest_data.get("file_count", 0) == 0:
                print("Invalid manifest format")
                print(manifest_data)
                continue
            
            files = manifest_data["files"]
            if not files:
                continue
                            
            for j in range(0, len(files), download_batch_size):
                download_batch = files[j:j+download_batch_size]
                batch_tasks = []
                
                for file_info in download_batch:
                    if not file_info.get("download_url") or not file_info.get("filename"):
                        logger.warning(f"Invalid file info in manifest: {file_info}")
                        continue
                    
                    filename = file_info["filename"]
                    destination = self.media_folder / filename
                    
                    # Skip if file already exists and is readable
                    if destination.exists():
                        try:
                            with open(destination, "rb") as f:
                                f.read(16)
                            total_skipped += 1
                            continue
                        except Exception:
                            # If reading fails, redownload the file
                            pass
                    
                    batch_tasks.append(self.download_file(file_info["download_url"], destination))
                
                # Wait for this download batch to complete
                if batch_tasks:
                    results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                    total_downloaded += sum(1 for r in results if r is True)
                    
                    # Log errors but continue with remaining files
                    for idx, result in enumerate(results):
                        if isinstance(result, Exception) or not result:
                            logger.error(f"Error downloading file: {result}")
                
                # Update progress after each download batch
                processed_count += len(download_batch)
                if progress_callback:
                    progress_callback(processed_count / total_files)
        
        return {
            "success": True, 
            "message": f"Downloaded {total_downloaded} files, skipped {total_skipped} existing files",
            "downloaded": total_downloaded,
            "skipped": total_skipped
        }

    @retry(max_tries=3, delay=1)
    async def check_media_bulk(self, user_token:str, deck_hash: str, files: List[Dict]) -> Dict:
        """
        Check multiple media files at once.
        
        Args:
            deck_hash: Hash of the deck
            files: List of file info dictionaries (hash, filename, note_guid)
            
        Returns:
            Dictionary with results for existing and missing files
        """
        if not files:
            return {"existing_files": [], "missing_files": []}
            
        if len(files) > 100:
            raise MediaServerError("Too many files to check at once")
        
        async with self.semaphore:
            await self.rate_limiter.wait_if_needed()
            
            url = f"{self.api_base_url}/media/check/bulk"
            data = {"token": user_token, "deck_hash": deck_hash, "files": files}
            
            try:
                response = await self.async_request(
                    "post", 
                    url, 
                    json=data,
                )
                return response.json()
                
            except requests.HTTPError as e:
                message = f"HTTP error {e.response.status_code}: {e.response.text}"
                logger.error(message)
                raise MediaServerError(e.response.text)
                
            except requests.RequestException as e:
                logger.error(f"Network error: {str(e)}")
                raise MediaServerError(f"Network error: {str(e)}")
    
    async def optimize_media_for_upload(self, file_note_pairs):
        from . import media_optimizer
        
        files_info = []
        file_paths = {}
        filename_mapping = {}
        
        if not file_note_pairs:
            return filename_mapping, files_info, file_paths
                                        
        base_dir = mw.col.media.dir()
        
        svg_files = []
        regular_files = []
        
        for filename, note_guid in file_note_pairs:
            filepath = os.path.join(base_dir, filename)
            filepath_obj = Path(filepath)
            
            if not filepath_obj.exists():
                logger.warning(f"File not found: {filepath}")
                continue
                
            if not self._is_allowed_file_type(filepath):
                logger.warning(f"File type not allowed: {filepath_obj.suffix}")
                continue
            
            # Sort into SVG and non-SVG files since they get optimized differently
            if filepath_obj.suffix.lower() == '.svg':
                # we skip large svg files here already, because they all need to get uploaded for sanitization. webp transformed files could still pass the limit after optimization
                if filepath_obj.stat().st_size > MAX_FILE_SIZE:
                    logger.warning(f"SVG File too big: {filepath}")
                    continue
                svg_files.append((filename, filepath_obj, note_guid))
            else:
                regular_files.append((filename, filepath_obj, note_guid))
        
        # Process files and build file info list
        
        if svg_files:                
            # Batch optimize SVGs without note ids bc who cares
            svg_optimize_list = [(filename, filepath_obj) for filename, filepath_obj, _ in svg_files]
            
            svg_optimized = await media_optimizer.optimize_svg_files(svg_optimize_list)
            
            for filename, filepath_obj, note_guid in svg_files:
                if filename in svg_optimized:
                    filepath, exp_hash, was_optimized = svg_optimized[filename]
                    
                    if not was_optimized or not exp_hash: # we dont want unoptimized files
                        continue
                    
                    try:
                        # Calculate hash and get file size
                        file_hash, file_size = await self.compute_file_hash_and_size(filepath)
                        
                        if file_hash != exp_hash:
                            logger.error(f"Hash mismatch for optimized SVG {filename}")
                            continue
                        
                        if file_size > MAX_FILE_SIZE: # impossible to hit here tbh
                            logger.warning(f"File exceeds size limit: {filepath} ({file_size} bytes)")
                            continue
                        
                        files_info.append({
                            "hash": file_hash,
                            "filename": filename,
                            "note_guid": note_guid,
                            "file_size": file_size
                        })
                        
                        file_paths[file_hash] = filepath
                        
                    except Exception as e:
                        logger.error(f"Error processing optimized SVG {filename}: {str(e)}")
        
        # Process regular files individually
        for i, (filename, filepath_obj, note_guid) in enumerate(regular_files):            
            try:
                # Optimize file if possible (images -> WebP)
                filepath, current_filename, was_optimized = await media_optimizer.optimize_media_file(filename, filepath_obj)
                
                # Track the mapping if file was optimized, but allow attempted upload if pillow is unavailable for example
                if was_optimized:
                    filename_mapping[filename] = current_filename
                    filename = current_filename
                    
                file_hash, file_size = await self.compute_file_hash_and_size(filepath)
                
                if file_size > MAX_FILE_SIZE:
                    logger.warning(f"File exceeds size limit: {filepath} ({file_size} bytes)")
                    continue
                
                files_info.append({
                    "hash": file_hash,
                    "filename": filename,
                    "note_guid": note_guid,
                    "file_size": file_size
                })
                
                file_paths[file_hash] = filepath
                
            except Exception as e:
                logger.error(f"Error preparing file {filename}: {str(e)}")
                
        return filename_mapping, files_info, file_paths

    async def upload_media_bulk(self, user_token, files_info, file_paths, deck_hash: str, progress_callback=None) -> Dict:        
        if not files_info:
            return {
                "success": True,
                "status": "all_wrong",
                "message": "All files failed. Please try again.",
                "uploaded": 0,
                "existing": 0,
                "failed": 0,
            }
            
        # Check which files need to be uploaded
        try:
            bulk_check_result = await self.check_media_bulk(user_token, deck_hash, files_info)
                        
            # Process existing files (they don't need uploading)
            existing_files = bulk_check_result.get("existing_files", [])
            existing_count = len(existing_files)
            
            check_failed_files = bulk_check_result.get("failed_files", [])
            check_failed_count = len(check_failed_files)
                
            # Process missing files that need uploading
            missing_files = bulk_check_result.get("missing_files", [])
            if not missing_files:
                return {
                    "success": True, 
                    "status": "all_exist", 
                    "message": f"{existing_count} files already exist on server, {check_failed_count} failed",
                    "uploaded": 0,
                    "existing": existing_count,
                    "failed": check_failed_count,
                }
            
            # Extract batch ID for confirmation later
            batch_id = bulk_check_result.get("batch_id")
            if not batch_id:
                return {
                    "success": False, 
                    "status": "no_batch_id", 
                    "message": "Server doesn't want your files",
                    }
        except Exception as e:
            logger.error(f"Error checking media files: {str(e)}")
            return {
                "success": False, 
                "status": "check_failed", 
                "message": str(e),
                }

        # Upload each missing file using its presigned URL
        uploaded_files = []
        failed_files = []

        batch_size = 10
        
        for i in range(0, len(missing_files), batch_size):
            batch = missing_files[i:i+batch_size]
            upload_tasks = []
            
            # Create tasks for this batch
            for j, file_info in enumerate(batch):
                # Update progress (second phase - 30% to 50% of total progress)
                if progress_callback:
                    progress_value = 0.3 + (0.2 * (i+j) / len(missing_files))
                    progress_callback(progress_value)
                    
                file_hash = file_info["hash"]
                presigned_url = file_info.get("presigned_url")
                
                if not presigned_url or file_hash not in file_paths:
                    failed_files.append(file_hash)
                    continue
                
                filepath = file_paths[file_hash]
                task = self.upload_file(presigned_url, filepath, file_hash)
                upload_tasks.append((file_hash, task))
            
            # Process this batch
            if upload_tasks:
                batch_results = await asyncio.gather(*[task for _, task in upload_tasks], return_exceptions=True)
                
                for i, result in enumerate(batch_results):
                    file_hash, _ = upload_tasks[i]
                    if isinstance(result, Exception) or not result:
                        logger.error(f"Failed to upload file {file_hash}: {result}")
                        failed_files.append(file_hash)
                    else:
                        uploaded_files.append(file_hash)
                
            # Update progress (50% - 80% of total progress)
            if progress_callback:
                progress_value = 0.5 + (0.3 * (i+len(batch)) / len(missing_files))
                progress_callback(progress_value)
                        
        # If any files were uploaded successfully, confirm with the server
        if uploaded_files:
            try:
                # Update progress (final phase - 90% to 100%)
                if progress_callback:
                    progress_callback(0.9)
                    
                await self.confirm_media_bulk_upload(batch_id, uploaded_files)
                success_count = len(uploaded_files)
                failed_count = len(failed_files)
                
                if progress_callback:
                    progress_callback(1.0)
                    
                return {
                    "success": True,
                    "status": "uploaded",
                    "message": f"Uploaded {success_count} files ({existing_count} existing, {failed_count + check_failed_count} failed)",
                    "uploaded": success_count,
                    "existing": existing_count,
                    "failed": failed_count + check_failed_count,
                    "details": "",
                }
            except Exception as e:
                logger.error(f"Error confirming bulk upload: {str(e)}")
                return {
                    "success": False,
                    "status": "confirmation_failed", 
                    "message": f"Failed to upload files (error 2)",
                    "uploaded": len(uploaded_files),
                    "existing": existing_count,
                    "failed": len(check_failed_count),
                    "error": str(e),
                }
        else:
            return {
                "success": False,
                "status": "upload_failed",
                "message": f"No files were uploaded ({existing_count} already existed)",
                "uploaded": 0, 
                "existing": existing_count,
                "failed": len(failed_files),
            }

    @retry(max_tries=3, delay=1)
    async def confirm_media_bulk_upload(self, batch_id: str, confirmed_files: List[str]) -> Dict:
        """
        Confirm successful bulk upload with the server.
        
        Args:
            batch_id: ID of the batch returned by check_media_bulk
            confirmed_files: List of file hashes that were successfully uploaded
            
        Returns:
            Server response with confirmation status
        """
        async with self.semaphore:
            await self.rate_limiter.wait_if_needed()
            
            url = f"{self.api_base_url}/media/confirm/bulk"
            data = {
                "batch_id": batch_id,
                "confirmed_files": confirmed_files
            }
            
            try:
                response = await self.async_request(
                    "post", 
                    url, 
                    json=data
                )
                if response.status_code == 200:
                    return
                else:
                    message = f"Server returned an error: {response.text}"
                    logger.error(message)
                    raise MediaServerError(message)

            except requests.exceptions.Timeout:
                logger.error("Timeout during Media Upload")
                
            except requests.HTTPError as e:
                message = f"HTTP error {e.response.status_code} during bulk confirm: {e.response.text}"
                logger.error(message)
                raise MediaServerError(f"Error communicating with the server")
                
            except requests.RequestException as e:
                logger.error(f"Network error during bulk confirmation: {str(e)}")
                raise MediaServerError(f"Network error: {str(e)}")
