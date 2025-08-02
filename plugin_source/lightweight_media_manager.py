"""
Lightweight Media Manager optimized for Raspberry Pi backend hosting.
Reduces memory usage, concurrent operations, and implements resource-aware processing.
"""

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
from functools import wraps, lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union, cast
from concurrent.futures import ThreadPoolExecutor
from asyncio import Lock as AsyncLock, get_running_loop

# Optional psutil import for memory monitoring
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# Import Pi-specific configuration
try:
    from .pi_config import *
except ImportError:
    # Fallback to original values if pi_config not available
    RASPBERRY_PI_MODE = False
    MAX_WORKERS_PI = 4
    MAX_REQUESTS_PER_MINUTE_PI = 30
    REQUEST_TIMEOUT_PI = 45
    HASH_CACHE_LIMIT = 200
    EXISTS_CACHE_LIMIT = 400
    DOWNLOAD_CACHE_LIMIT = 100

logger = logging.getLogger("ankicollab")

# Pi-optimized constants
if RASPBERRY_PI_MODE:
    MAX_REQUESTS_PER_MINUTE = MAX_REQUESTS_PER_MINUTE_PI
    REQUEST_TIMEOUT = REQUEST_TIMEOUT_PI
    MAX_FILE_SIZE = MAX_FILE_SIZE_PI
    CHUNK_SIZE = CHUNK_SIZE_PI
else:
    # Original values for non-Pi deployments (full performance)
    MAX_REQUESTS_PER_MINUTE = 100  # Higher for regular servers
    REQUEST_TIMEOUT = 30
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB for regular servers
    CHUNK_SIZE = 16384

REQUEST_TRACKING_WINDOW = 60
VERIFY_SSL = True

ALLOWED_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"},
    "audio": {".mp3", ".ogg"},
}
ALL_ALLOWED_EXTENSIONS = set().union(*ALLOWED_EXTENSIONS.values())

class MemoryAwareLRUCache:
    """LRU Cache with memory usage monitoring."""
    
    def __init__(self, max_size: int, memory_threshold_mb: int = 100):
        self.max_size = max_size
        self.memory_threshold_mb = memory_threshold_mb
        self.cache = {}
        self.access_order = []
    
    def get(self, key):
        if key in self.cache:
            # Move to end (most recently used)
            self.access_order.remove(key)
            self.access_order.append(key)
            return self.cache[key]
        return None
    
    def set(self, key, value):
        # Check memory usage before adding
        if self._check_memory_usage():
            self._cleanup_cache()
        
        if key in self.cache:
            self.access_order.remove(key)
        elif len(self.cache) >= self.max_size:
            # Remove least recently used
            oldest = self.access_order.pop(0)
            del self.cache[oldest]
        
        self.cache[key] = value
        self.access_order.append(key)
    
    def _check_memory_usage(self) -> bool:
        """Check if memory usage is above threshold."""
        if not HAS_PSUTIL:
            # Fallback: assume memory pressure after certain cache size
            return len(self.cache) > self.max_size * 0.8
        
        try:
            memory_usage = psutil.Process().memory_info().rss / 1024 / 1024  # MB
            return memory_usage > self.memory_threshold_mb
        except:
            return False
    
    def _cleanup_cache(self):
        """Aggressive cache cleanup when memory is high."""
        cleanup_count = max(1, len(self.cache) // 4)  # Remove 25% of cache
        for _ in range(cleanup_count):
            if self.access_order:
                oldest = self.access_order.pop(0)
                del self.cache[oldest]
        logger.info(f"Cleaned up {cleanup_count} cache entries due to memory pressure")
    
    def clear(self):
        self.cache.clear()
        self.access_order.clear()

class LightweightMediaManager:
    """Raspberry Pi optimized Media Manager with reduced resource usage."""
    
    def __init__(self, api_base_url: str, media_folder: str):
        self.api_base_url = api_base_url.rstrip("/")
        self.media_folder = Path(media_folder)
        
        if not self.media_folder.exists():
            raise ValueError(f"Media folder not found at: {media_folder}")
        
        # Pi-optimized settings
        if RASPBERRY_PI_MODE:
            max_workers = min(MAX_WORKERS_PI, (os.cpu_count() or 1))
            cache_memory_limit = 50  # MB
        else:
            # Full performance for regular servers (supports 32+ users)
            max_workers = min(32, (os.cpu_count() or 1) + 2)  # Restore original performance
            cache_memory_limit = 200  # MB - higher for regular servers
        
        # Initialize caches with memory awareness
        if RASPBERRY_PI_MODE:
            self.hash_cache = MemoryAwareLRUCache(HASH_CACHE_LIMIT, cache_memory_limit)
            self.exists_cache = MemoryAwareLRUCache(EXISTS_CACHE_LIMIT, cache_memory_limit)
            self.download_cache = MemoryAwareLRUCache(DOWNLOAD_CACHE_LIMIT, cache_memory_limit)
        else:
            # Use larger caches for regular servers
            self.hash_cache = MemoryAwareLRUCache(1000, cache_memory_limit)  # 5x larger
            self.exists_cache = MemoryAwareLRUCache(2000, cache_memory_limit)  # 5x larger
            self.download_cache = MemoryAwareLRUCache(500, cache_memory_limit)  # 5x larger
        
        # Rate limiter with Pi-optimized settings
        self.rate_limiter = RateLimiter(MAX_REQUESTS_PER_MINUTE, REQUEST_TRACKING_WINDOW)
        
        # Reduced thread pool
        self.thread_executor = ThreadPoolExecutor(max_workers=max_workers)
        logger.info(f"Initialized lightweight media manager with {max_workers} workers")
        
        # Session configuration
        self.session = requests.Session()
        self.session.verify = VERIFY_SSL
        
        # Async semaphore for Pi
        self.semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_lock = AsyncLock()
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Memory monitoring
        self.last_memory_check = 0
        self.memory_check_interval = 30  # seconds
        
    async def _get_semaphore(self) -> asyncio.Semaphore:
        """Get semaphore with Pi-optimized concurrency limit."""
        try:
            current_loop = get_running_loop()
        except RuntimeError:
            logger.error("_get_semaphore called without a running event loop!")
            raise RuntimeError("Cannot get semaphore without a running event loop")
        
        if self.semaphore is None or self._semaphore_loop is not current_loop:
            async with self._semaphore_lock:
                if self.semaphore is None or self._semaphore_loop is not current_loop:
                    # Adaptive concurrency based on system type
                    if RASPBERRY_PI_MODE:
                        concurrency_limit = 2  # Conservative for Pi
                    else:
                        concurrency_limit = 10  # Higher for regular servers
                    logger.debug(f"Initializing semaphore with limit {concurrency_limit}")
                    self.semaphore = asyncio.Semaphore(concurrency_limit)
                    self._semaphore_loop = current_loop
        
        return cast(asyncio.Semaphore, self.semaphore)
    
    def _check_memory_pressure(self) -> bool:
        """Check for memory pressure and trigger cleanup if needed."""
        now = time.time()
        if now - self.last_memory_check < self.memory_check_interval:
            return False
        
        self.last_memory_check = now
        
        if not HAS_PSUTIL:
            # Fallback: use cache size heuristics
            total_cache_items = (len(self.hash_cache.cache) + 
                               len(self.exists_cache.cache) + 
                               len(self.download_cache.cache))
            threshold = HASH_CACHE_LIMIT + EXISTS_CACHE_LIMIT + DOWNLOAD_CACHE_LIMIT
            
            if total_cache_items > threshold * 0.8:
                logger.warning(f"High cache usage: {total_cache_items} items, triggering cleanup")
                self.emergency_cleanup()
                return True
            return False
        
        try:
            memory_info = psutil.Process().memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            
            # Pi-specific memory thresholds
            if RASPBERRY_PI_MODE:
                memory_threshold = 150  # MB for Pi
            else:
                memory_threshold = 500  # MB for regular systems (higher threshold)
            
            if memory_mb > memory_threshold:
                logger.warning(f"Memory usage high: {memory_mb:.1f}MB, triggering cleanup")
                self.emergency_cleanup()
                return True
                
        except Exception as e:
            logger.debug(f"Memory check failed: {e}")
        
        return False
    
    def emergency_cleanup(self):
        """Emergency cleanup when memory pressure is detected."""
        logger.info("Performing emergency memory cleanup")
        
        # Clear all caches
        self.hash_cache.clear()
        self.exists_cache.clear()
        self.download_cache.clear()
        
        # Force garbage collection
        import gc
        gc.collect()
        
        logger.info("Emergency cleanup completed")
    
    async def async_request(self, method, url, **kwargs):
        """Pi-optimized async request with memory monitoring."""
        # Check memory pressure before making requests
        if self._check_memory_pressure():
            # Add small delay to let cleanup take effect
            await asyncio.sleep(0.1)
        
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("async_request called without a running event loop!")
            raise RuntimeError("No running event loop found for async_request")
        
        if "timeout" not in kwargs:
            kwargs["timeout"] = REQUEST_TIMEOUT
        
        # Use semaphore to limit concurrent requests
        semaphore = await self._get_semaphore()
        
        async with semaphore:
            # Rate limiting
            await self.rate_limiter.wait_if_needed()
            
            def do_request():
                try:
                    response = self.session.request(method, url, **kwargs)
                    response.raise_for_status()
                    return response
                except requests.exceptions.RequestException as req_exc:
                    logger.error(f"Network request failed: {method.upper()} {url} -> {req_exc}")
                    raise
            
            return await loop.run_in_executor(self.thread_executor, do_request)
    
    def clear_caches(self):
        """Clear all caches to free memory."""
        self.hash_cache.clear()
        self.exists_cache.clear()
        self.download_cache.clear()
        logger.info("All lightweight media manager caches cleared")
    
    def get_memory_stats(self) -> Dict[str, Union[int, float]]:
        """Get current memory usage statistics."""
        stats = {
            "hash_cache_size": len(self.hash_cache.cache),
            "exists_cache_size": len(self.exists_cache.cache),
            "download_cache_size": len(self.download_cache.cache),
            "pi_mode": RASPBERRY_PI_MODE,
            "max_workers": self.thread_executor._max_workers,
            "has_psutil": HAS_PSUTIL,
        }
        
        if HAS_PSUTIL:
            try:
                process = psutil.Process()
                memory_info = process.memory_info()
                stats["memory_mb"] = memory_info.rss / 1024 / 1024
            except Exception as e:
                stats["memory_error"] = str(e)
        else:
            stats["memory_mb"] = "unavailable (no psutil)"
        
        return stats

class RateLimiter:
    """Rate limiter with Pi-aware settings."""
    
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
                # Longer waits for Pi to prevent overwhelming
                if RASPBERRY_PI_MODE:
                    wait_time *= 1.5
                logger.debug(f"Rate limit reached. Waiting for {wait_time:.2f} seconds")
                await asyncio.sleep(wait_time)
        
        self.calls.append(now)

# Exception classes (kept minimal for Pi)
class MediaError(Exception): pass
class MediaServerError(MediaError): pass
class MediaRateLimitError(MediaError): pass
class MediaTypeError(MediaError): pass
class MediaHashError(MediaError): pass
class MediaUploadError(MediaError): pass
class MediaDownloadError(MediaError): pass
