# Raspberry Pi Optimized Configuration
# These settings reduce resource usage ONLY for Pi-hosted backends
# Regular servers maintain full performance (32 workers, 100 req/min, etc.)

# Auto-detection: Only applies optimizations when Pi is detected
RASPBERRY_PI_MODE = True  # This will be overridden by auto-detection

# Reduced concurrent operations for Pi only
MAX_WORKERS_PI = 2  # Regular servers: 32 (unchanged)
MAX_REQUESTS_PER_MINUTE_PI = 20  # Regular servers: 100 (unchanged)
REQUEST_TIMEOUT_PI = 60  # Regular servers: 30 (unchanged)

# Smaller cache sizes to preserve Pi memory (regular servers use larger caches)
HASH_CACHE_LIMIT = 100  # Regular servers: 1000
EXISTS_CACHE_LIMIT = 200  # Regular servers: 2000
DOWNLOAD_CACHE_LIMIT = 50  # Regular servers: 500

# Media processing optimizations for Pi
MAX_FILE_SIZE_PI = 1 * 1024 * 1024  # 1MB for Pi (Regular: 5MB)
CHUNK_SIZE_PI = 8192  # Smaller chunks for Pi (Regular: 16384)
ENABLE_PROGRESSIVE_LOADING = True

# Database optimizations
USE_SQLITE = True
DB_CONNECTION_POOL_SIZE = 2
DB_QUERY_TIMEOUT = 30

# Background processing
ENABLE_ASYNC_MEDIA_PROCESSING = True
BACKGROUND_QUEUE_SIZE = 10
CLEANUP_INTERVAL = 300  # 5 minutes

# Error handling for resource constraints
ENABLE_GRACEFUL_DEGRADATION = True
AUTO_RETRY_ON_MEMORY_ERROR = True
FALLBACK_TO_SYNC_ON_ASYNC_FAILURE = True
