"""
Auto-detection and configuration adapter for Raspberry Pi optimization.
Automatically detects Pi hardware and adjusts settings accordingly.
"""

import os
import platform
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("ankicollab")

def detect_raspberry_pi() -> bool:
    """
    Detect if running on Raspberry Pi hardware.
    Returns True if Pi is detected, False otherwise.
    """
    try:
        # Method 1: Check /proc/cpuinfo for BCM processor
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r") as f:
                cpuinfo = f.read().lower()
                if "bcm" in cpuinfo or "raspberry" in cpuinfo:
                    return True
        
        # Method 2: Check for Pi-specific files
        pi_files = [
            "/sys/firmware/devicetree/base/model",
            "/proc/device-tree/model"
        ]
        
        for pi_file in pi_files:
            if os.path.exists(pi_file):
                try:
                    with open(pi_file, "r") as f:
                        content = f.read().lower()
                        if "raspberry" in content:
                            return True
                except:
                    continue
        
        # Method 3: Check platform architecture
        machine = platform.machine().lower()
        if machine in ["armv6l", "armv7l", "aarch64"] and platform.system() == "Linux":
            # Likely ARM-based Linux, could be Pi
            return True
            
    except Exception as e:
        logger.debug(f"Pi detection failed: {e}")
    
    return False

def get_system_resources() -> Dict[str, Any]:
    """
    Get system resource information for optimization decisions.
    """
    resources = {
        "cpu_count": os.cpu_count() or 1,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "system": platform.system(),
        "is_pi": detect_raspberry_pi(),
    }
    
    # Try to get memory information
    try:
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", "r") as f:
                meminfo = f.read()
                for line in meminfo.split("\n"):
                    if line.startswith("MemTotal:"):
                        # Extract memory in KB and convert to MB
                        mem_kb = int(line.split()[1])
                        resources["memory_mb"] = mem_kb // 1024
                        break
    except Exception as e:
        logger.debug(f"Failed to get memory info: {e}")
        resources["memory_mb"] = None
    
    return resources

def get_optimized_config(force_pi_mode: Optional[bool] = None) -> Dict[str, Any]:
    """
    Get optimized configuration based on system detection.
    
    Args:
        force_pi_mode: Override auto-detection (True/False/None)
    
    Returns:
        Dictionary with optimized configuration values
    """
    system_info = get_system_resources()
    
    # Determine if we should use Pi mode
    if force_pi_mode is not None:
        pi_mode = force_pi_mode
    else:
        pi_mode = system_info["is_pi"]
        
        # Also enable Pi mode for low-memory systems
        if system_info.get("memory_mb"):
            if system_info["memory_mb"] < 2048:  # Less than 2GB RAM
                pi_mode = True
    
    logger.info(f"System detected - Pi mode: {pi_mode}, CPUs: {system_info['cpu_count']}, "
                f"Memory: {system_info.get('memory_mb', 'unknown')}MB")
    
    if pi_mode:
        config = {
            # Core settings
            "RASPBERRY_PI_MODE": True,
            "MAX_WORKERS": min(2, system_info["cpu_count"]),
            "MAX_REQUESTS_PER_MINUTE": 15,
            "REQUEST_TIMEOUT": 60,
            
            # Memory management
            "HASH_CACHE_LIMIT": 50,
            "EXISTS_CACHE_LIMIT": 100,
            "DOWNLOAD_CACHE_LIMIT": 25,
            "MEMORY_THRESHOLD_MB": 50,
            
            # File processing
            "MAX_FILE_SIZE": 1 * 1024 * 1024,  # 1MB
            "CHUNK_SIZE": 4096,
            "CONCURRENCY_LIMIT": 1,
            
            # Background processing
            "ENABLE_ASYNC_PROCESSING": True,
            "BACKGROUND_QUEUE_SIZE": 5,
            "CLEANUP_INTERVAL": 180,  # 3 minutes
            
            # Error handling
            "ENABLE_GRACEFUL_DEGRADATION": True,
            "AUTO_RETRY_DELAY": 2.0,
            "MAX_RETRIES": 2,
        }
    else:
        # Standard configuration for more powerful systems (supports 32+ users)
        config = {
            "RASPBERRY_PI_MODE": False,
            "MAX_WORKERS": min(32, (system_info["cpu_count"] or 1) + 2),  # Full performance
            "MAX_REQUESTS_PER_MINUTE": 100,  # Higher limit for regular servers
            "REQUEST_TIMEOUT": 30,
            
            "HASH_CACHE_LIMIT": 1000,  # Larger caches for better performance
            "EXISTS_CACHE_LIMIT": 2000,
            "DOWNLOAD_CACHE_LIMIT": 500,
            "MEMORY_THRESHOLD_MB": 500,  # Higher memory threshold
            
            "MAX_FILE_SIZE": 5 * 1024 * 1024,  # 5MB for regular servers
            "CHUNK_SIZE": 16384,
            "CONCURRENCY_LIMIT": 10,  # Higher concurrency
            
            "ENABLE_ASYNC_PROCESSING": True,
            "BACKGROUND_QUEUE_SIZE": 50,  # Larger queue
            "CLEANUP_INTERVAL": 600,  # 10 minutes (less aggressive)
            
            "ENABLE_GRACEFUL_DEGRADATION": False,
            "AUTO_RETRY_DELAY": 1.0,
            "MAX_RETRIES": 3,
        }
    
    # Add system info for reference
    config["SYSTEM_INFO"] = system_info
    
    return config

def apply_runtime_optimizations(config: Dict[str, Any]) -> None:
    """
    Apply runtime optimizations based on configuration.
    """
    if config.get("RASPBERRY_PI_MODE"):
        logger.info("Applying Raspberry Pi optimizations...")
        
        # Set environment variables for Python optimizations
        os.environ.setdefault("PYTHONOPTIMIZE", "1")
        os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        
        # Suggest garbage collection tuning
        import gc
        gc.set_threshold(700, 10, 10)  # More aggressive GC for Pi
        
        logger.info("Pi optimizations applied")

def get_deployment_recommendations(config: Dict[str, Any]) -> Dict[str, str]:
    """
    Get deployment recommendations based on system configuration.
    """
    recommendations = {}
    
    if config.get("RASPBERRY_PI_MODE"):
        recommendations.update({
            "database": "Use SQLite instead of PostgreSQL for lower memory usage",
            "storage": "Consider local filesystem storage instead of S3 for better performance",
            "caching": "Enable aggressive caching and compression",
            "monitoring": "Set up memory and CPU monitoring with alerts",
            "backups": "Implement automated backup to external storage",
            "updates": "Schedule updates during low-usage periods",
            "networking": "Use wired connection for better stability",
            "power": "Ensure stable power supply with UPS if possible"
        })
    else:
        recommendations.update({
            "database": "PostgreSQL is suitable for this system",
            "storage": "S3 or similar cloud storage recommended for scalability",
            "caching": "Standard caching configuration is appropriate",
            "monitoring": "Standard monitoring setup recommended",
            "backups": "Cloud backup solutions recommended",
            "updates": "Regular update schedule is fine",
            "networking": "Standard network configuration",
            "power": "Standard power management"
        })
    
    return recommendations

# Auto-configuration on import
AUTO_CONFIG = get_optimized_config()

# Export commonly used values
RASPBERRY_PI_MODE = AUTO_CONFIG["RASPBERRY_PI_MODE"]
MAX_WORKERS_OPTIMIZED = AUTO_CONFIG["MAX_WORKERS"]
MAX_REQUESTS_PER_MINUTE_OPTIMIZED = AUTO_CONFIG["MAX_REQUESTS_PER_MINUTE"]
REQUEST_TIMEOUT_OPTIMIZED = AUTO_CONFIG["REQUEST_TIMEOUT"]

# Apply optimizations
apply_runtime_optimizations(AUTO_CONFIG)
