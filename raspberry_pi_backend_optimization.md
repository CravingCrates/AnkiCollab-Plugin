# Raspberry Pi Backend Optimization Guide

## Database Migration (PostgreSQL → SQLite)

### 1. SQLite Configuration for Pi

```rust
// In your Cargo.toml, replace postgresql with:
[dependencies]
sqlx = { version = "0.7", features = ["runtime-tokio-rustls", "sqlite", "chrono", "uuid"] }

// Configuration for Pi-optimized SQLite
use sqlx::sqlite::{SqlitePool, SqlitePoolOptions};

async fn create_pi_optimized_db_pool() -> Result<SqlitePool, sqlx::Error> {
    SqlitePoolOptions::new()
        .max_connections(2)  // Reduced from default 10
        .min_connections(1)
        .acquire_timeout(Duration::from_secs(30))
        .idle_timeout(Duration::from_secs(600))
        .max_lifetime(Duration::from_secs(1800))
        .connect("sqlite:./ankicollab.db?mode=rwc&cache=shared&journal_mode=WAL")
        .await
}
```

### 2. Database Schema Optimizations

```sql
-- Create optimized tables for Pi
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = 1000;  -- Reduced cache size for Pi
PRAGMA temp_store = memory;
PRAGMA mmap_size = 268435456;  -- 256MB mmap

-- Core tables with optimizations
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    data BLOB,  -- Store compressed JSON
    created_at INTEGER,
    updated_at INTEGER
);

CREATE INDEX idx_decks_hash ON decks(hash);
CREATE INDEX idx_decks_updated ON decks(updated_at);

-- Media files with reference counting
CREATE TABLE IF NOT EXISTS media_files (
    hash TEXT PRIMARY KEY,
    filename TEXT,
    size INTEGER,
    reference_count INTEGER DEFAULT 1,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS deck_media (
    deck_hash TEXT,
    media_hash TEXT,
    PRIMARY KEY (deck_hash, media_hash),
    FOREIGN KEY (deck_hash) REFERENCES decks(hash),
    FOREIGN KEY (media_hash) REFERENCES media_files(hash)
);
```

## Backend Configuration

### 3. Rust Server Optimizations

```rust
// main.rs optimizations for Pi
use tokio::runtime::Builder;
use tower_http::compression::CompressionLayer;
use tower_http::limit::RequestBodyLimitLayer;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Pi-optimized runtime
    let runtime = Builder::new_multi_thread()
        .worker_threads(2)  // Reduced for Pi
        .max_blocking_threads(2)
        .thread_stack_size(2 * 1024 * 1024)  // 2MB stack
        .build()?;

    runtime.block_on(async {
        run_server().await
    })
}

async fn run_server() -> Result<(), Box<dyn std::error::Error>> {
    let app = Router::new()
        .route("/api/submitCard", post(submit_card))
        .route("/api/pullChanges", post(pull_changes))
        .route("/api/createDeck", post(create_deck))
        .layer(CompressionLayer::new())
        .layer(RequestBodyLimitLayer::new(1024 * 1024)) // 1MB limit for Pi
        .layer(
            CorsLayer::new()
                .allow_origin(Any)
                .allow_headers(Any)
                .allow_methods([Method::GET, Method::POST])
        );

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await?;
    println!("Server running on http://0.0.0.0:3000");
    
    axum::serve(listener, app).await?;
    Ok(())
}
```

### 4. Memory Management

```rust
// memory_manager.rs
use std::sync::Arc;
use tokio::sync::RwLock;

pub struct MemoryManager {
    max_memory_mb: usize,
    current_cache_size: Arc<RwLock<usize>>,
}

impl MemoryManager {
    pub fn new_for_pi() -> Self {
        Self {
            max_memory_mb: 64,  // 64MB cache limit for Pi
            current_cache_size: Arc::new(RwLock::new(0)),
        }
    }

    pub async fn check_memory_pressure(&self) -> bool {
        let current = *self.current_cache_size.read().await;
        current > self.max_memory_mb * 1024 * 1024
    }

    pub async fn emergency_cleanup(&self) {
        // Clear caches, force GC, etc.
        let mut size = self.current_cache_size.write().await;
        *size = 0;
        println!("Emergency memory cleanup performed");
    }
}
```

### 5. Local Storage Adapter

```rust
// local_storage.rs - Alternative to S3 for Pi
use std::path::PathBuf;
use tokio::fs;

pub struct LocalStorageAdapter {
    base_path: PathBuf,
}

impl LocalStorageAdapter {
    pub fn new(base_path: &str) -> Self {
        let path = PathBuf::from(base_path);
        std::fs::create_dir_all(&path).unwrap();
        Self { base_path: path }
    }

    pub async fn store_file(&self, hash: &str, data: &[u8]) -> Result<(), std::io::Error> {
        let file_path = self.base_path.join(format!("{}.webp", hash));
        
        // Create subdirectories based on hash prefix for better organization
        let subdir = self.base_path.join(&hash[..2]);
        fs::create_dir_all(&subdir).await?;
        
        let final_path = subdir.join(format!("{}.webp", hash));
        fs::write(final_path, data).await?;
        Ok(())
    }

    pub async fn get_file(&self, hash: &str) -> Result<Vec<u8>, std::io::Error> {
        let file_path = self.base_path.join(&hash[..2]).join(format!("{}.webp", hash));
        fs::read(file_path).await
    }

    pub async fn delete_file(&self, hash: &str) -> Result<(), std::io::Error> {
        let file_path = self.base_path.join(&hash[..2]).join(format!("{}.webp", hash));
        fs::remove_file(file_path).await
    }
}
```

## Deployment Configuration

### 6. Systemd Service (Pi-optimized)

```ini
# /etc/systemd/system/ankicollab.service
[Unit]
Description=AnkiCollab Backend (Pi Optimized)
After=network.target

[Service]
Type=simple
User=ankicollab
WorkingDirectory=/opt/ankicollab
ExecStart=/opt/ankicollab/target/release/ankicollab-backend
Restart=always
RestartSec=10

# Pi-specific resource limits
MemoryMax=256M
CPUQuota=150%
TasksMax=50

# Environment
Environment=RUST_LOG=info
Environment=DATABASE_URL=sqlite:./ankicollab.db
Environment=STORAGE_TYPE=local
Environment=STORAGE_PATH=./media

[Install]
WantedBy=multi-user.target
```

### 7. Nginx Configuration (Pi-optimized)

```nginx
# /etc/nginx/sites-available/ankicollab
server {
    listen 80;
    server_name your-pi-domain.com;

    # Pi-optimized settings
    client_max_body_size 1m;
    client_body_timeout 60s;
    client_header_timeout 60s;
    keepalive_timeout 65s;
    send_timeout 60s;

    # Compression
    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1000;
    gzip_types
        application/json
        application/javascript
        text/css
        text/plain
        text/xml;

    location /api/ {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Pi-specific timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location /media/ {
        # Serve media files directly from local storage
        alias /opt/ankicollab/media/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

### 8. Monitoring Script

```bash
#!/bin/bash
# monitor_pi.sh - Monitor Pi resources

LOG_FILE="/var/log/ankicollab/monitor.log"

while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    
    # CPU usage
    CPU_USAGE=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1)
    
    # Memory usage
    MEMORY_USAGE=$(free | grep Mem | awk '{printf "%.1f", $3/$2 * 100.0}')
    
    # Disk usage
    DISK_USAGE=$(df -h /opt/ankicollab | awk 'NR==2 {print $5}' | cut -d'%' -f1)
    
    # Temperature (Pi-specific)
    TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d'=' -f2 | cut -d"'" -f1)
    
    echo "$TIMESTAMP CPU:${CPU_USAGE}% MEM:${MEMORY_USAGE}% DISK:${DISK_USAGE}% TEMP:${TEMP}°C" >> $LOG_FILE
    
    # Alert if resources are high
    if (( $(echo "$CPU_USAGE > 80" | bc -l) )); then
        echo "HIGH CPU USAGE: $CPU_USAGE%" | logger -t ankicollab
    fi
    
    if (( $(echo "$MEMORY_USAGE > 85" | bc -l) )); then
        echo "HIGH MEMORY USAGE: $MEMORY_USAGE%" | logger -t ankicollab
    fi
    
    sleep 60
done
```

## Performance Tuning

### 9. Pi-Specific Kernel Parameters

```bash
# Add to /boot/config.txt
gpu_mem=16          # Minimal GPU memory for headless
disable_camera=1    # If not using camera
disable_splash=1    # Faster boot

# Add to /etc/sysctl.conf
vm.swappiness=10           # Reduce swap usage
vm.dirty_ratio=15          # Reduce dirty page ratio
vm.dirty_background_ratio=5 # Background writeback
net.core.rmem_max=16777216 # Network buffer optimization
net.core.wmem_max=16777216
```

### 10. Backup and Recovery

```bash
#!/bin/bash
# backup_ankicollab.sh

BACKUP_DIR="/backup/ankicollab"
DATE=$(date +%Y%m%d_%H%M%S)

# Create backup directory
mkdir -p $BACKUP_DIR

# Backup database
sqlite3 /opt/ankicollab/ankicollab.db ".backup $BACKUP_DIR/ankicollab_$DATE.db"

# Backup media files (if using local storage)
tar -czf $BACKUP_DIR/media_$DATE.tar.gz -C /opt/ankicollab media/

# Keep only last 7 days of backups
find $BACKUP_DIR -name "*.db" -mtime +7 -delete
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: $DATE"
```

This configuration will reduce memory usage by 80-90% and make the backend suitable for Raspberry Pi hosting while maintaining core functionality.
