#!/bin/bash

# AnkiCollab Raspberry Pi Deployment Script
# This script sets up a lightweight AnkiCollab backend optimized for Raspberry Pi

set -e

echo "=== AnkiCollab Raspberry Pi Deployment ==="
echo "This script will set up a lightweight AnkiCollab backend"
echo "optimized for Raspberry Pi hosting."
echo

# Configuration
APP_USER="ankicollab"
APP_DIR="/opt/ankicollab"
DB_FILE="$APP_DIR/ankicollab.db"
MEDIA_DIR="$APP_DIR/media"
LOG_DIR="/var/log/ankicollab"
BACKUP_DIR="/backup/ankicollab"
SERVICE_NAME="ankicollab"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_pi() {
    log_info "Checking if running on Raspberry Pi..."
    
    if [[ -f /proc/cpuinfo ]] && grep -q "BCM" /proc/cpuinfo; then
        log_info "Raspberry Pi detected!"
        return 0
    elif [[ -f /sys/firmware/devicetree/base/model ]] && grep -qi "raspberry" /sys/firmware/devicetree/base/model; then
        log_info "Raspberry Pi detected!"
        return 0
    else
        log_warn "Not running on Raspberry Pi, but continuing with Pi optimizations..."
        return 0
    fi
}

check_memory() {
    local mem_total=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    local mem_gb=$((mem_total / 1024 / 1024))
    
    log_info "Detected ${mem_gb}GB of RAM"
    
    if [[ $mem_gb -lt 1 ]]; then
        log_warn "Less than 1GB RAM detected. Performance may be limited."
    elif [[ $mem_gb -le 2 ]]; then
        log_info "Low memory system detected. Using aggressive optimizations."
    fi
}

install_dependencies() {
    log_info "Installing system dependencies..."
    
    # Update package list
    apt update
    
    # Install required packages
    apt install -y \
        curl \
        wget \
        git \
        build-essential \
        pkg-config \
        libssl-dev \
        sqlite3 \
        nginx \
        logrotate \
        bc \
        htop
    
    # Install Rust for Pi
    if ! command -v rustc &> /dev/null; then
        log_info "Installing Rust..."
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
        source $HOME/.cargo/env
        
        # Pi-specific Rust optimizations
        rustup target add armv7-unknown-linux-gnueabihf
    fi
    
    log_info "Dependencies installed successfully"
}

create_user() {
    log_info "Creating application user..."
    
    if ! id "$APP_USER" &>/dev/null; then
        useradd --system --shell /bin/bash --home-dir $APP_DIR --create-home $APP_USER
        log_info "User $APP_USER created"
    else
        log_info "User $APP_USER already exists"
    fi
}

setup_directories() {
    log_info "Setting up directory structure..."
    
    # Create directories
    mkdir -p $APP_DIR
    mkdir -p $MEDIA_DIR
    mkdir -p $LOG_DIR
    mkdir -p $BACKUP_DIR
    mkdir -p $APP_DIR/config
    
    # Set permissions
    chown -R $APP_USER:$APP_USER $APP_DIR
    chown -R $APP_USER:$APP_USER $LOG_DIR
    chown -R $APP_USER:$APP_USER $BACKUP_DIR
    
    chmod 755 $APP_DIR
    chmod 755 $MEDIA_DIR
    chmod 755 $LOG_DIR
    chmod 755 $BACKUP_DIR
    
    log_info "Directory structure created"
}

setup_database() {
    log_info "Setting up SQLite database..."
    
    # Create database with Pi-optimized settings
    sudo -u $APP_USER sqlite3 $DB_FILE << 'EOF'
-- Pi-optimized SQLite settings
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = 1000;
PRAGMA temp_store = memory;
PRAGMA mmap_size = 134217728;  -- 128MB mmap for Pi

-- Core tables
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    data BLOB,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS media_files (
    hash TEXT PRIMARY KEY,
    filename TEXT,
    size INTEGER,
    reference_count INTEGER DEFAULT 1,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS deck_media (
    deck_hash TEXT,
    media_hash TEXT,
    PRIMARY KEY (deck_hash, media_hash),
    FOREIGN KEY (deck_hash) REFERENCES decks(hash),
    FOREIGN KEY (media_hash) REFERENCES media_files(hash)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_decks_hash ON decks(hash);
CREATE INDEX IF NOT EXISTS idx_decks_updated ON decks(updated_at);
CREATE INDEX IF NOT EXISTS idx_media_files_hash ON media_files(hash);
CREATE INDEX IF NOT EXISTS idx_deck_media_deck ON deck_media(deck_hash);
CREATE INDEX IF NOT EXISTS idx_deck_media_media ON deck_media(media_hash);

-- Insert initial data if needed
INSERT OR IGNORE INTO decks (hash, data) VALUES ('system', '{}');
EOF
    
    chown $APP_USER:$APP_USER $DB_FILE
    log_info "Database setup completed"
}

build_backend() {
    log_info "Building AnkiCollab backend..."
    
    # Clone or update repository
    if [[ ! -d $APP_DIR/source ]]; then
        sudo -u $APP_USER git clone https://github.com/Leadernelson/AnkiCollab-Backend.git $APP_DIR/source
    else
        cd $APP_DIR/source
        sudo -u $APP_USER git pull
    fi
    
    cd $APP_DIR/source
    
    # Create Pi-optimized Cargo config
    sudo -u $APP_USER mkdir -p .cargo
    sudo -u $APP_USER cat > .cargo/config.toml << EOF
[build]
target-dir = "$APP_DIR/target"

[target.armv7-unknown-linux-gnueabihf]
linker = "arm-linux-gnueabihf-gcc"

[env]
SQLITE_MAX_CONNECTIONS = "2"
SQLITE_CACHE_SIZE = "1000"
EOF
    
    # Build with Pi optimizations
    sudo -u $APP_USER cargo build --release --target armv7-unknown-linux-gnueabihf
    
    # Copy binary to app directory
    cp target/armv7-unknown-linux-gnueabihf/release/ankicollab-backend $APP_DIR/
    chown $APP_USER:$APP_USER $APP_DIR/ankicollab-backend
    chmod +x $APP_DIR/ankicollab-backend
    
    log_info "Backend build completed"
}

create_config() {
    log_info "Creating configuration files..."
    
    # Application config
    sudo -u $APP_USER cat > $APP_DIR/config/app.toml << EOF
[server]
host = "0.0.0.0"
port = 3000
workers = 2

[database]
url = "sqlite:$DB_FILE"
max_connections = 2
min_connections = 1
acquire_timeout = 30
idle_timeout = 600

[storage]
type = "local"
path = "$MEDIA_DIR"
max_file_size = 1048576  # 1MB
cleanup_interval = 180   # 3 minutes

[cache]
max_memory_mb = 64
hash_cache_size = 50
exists_cache_size = 100
download_cache_size = 25

[monitoring]
enable_metrics = true
log_level = "info"
memory_check_interval = 30
EOF
    
    chown $APP_USER:$APP_USER $APP_DIR/config/app.toml
    log_info "Configuration files created"
}

setup_systemd() {
    log_info "Setting up systemd service..."
    
    cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=AnkiCollab Backend (Pi Optimized)
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/ankicollab-backend
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Pi-specific resource limits
MemoryMax=256M
CPUQuota=150%
TasksMax=50
OOMScoreAdjust=100

# Environment
Environment=RUST_LOG=info
Environment=CONFIG_FILE=$APP_DIR/config/app.toml
Environment=DATABASE_URL=sqlite:$DB_FILE
Environment=STORAGE_PATH=$MEDIA_DIR

[Install]
WantedBy=multi-user.target
EOF
    
    systemctl daemon-reload
    systemctl enable $SERVICE_NAME
    log_info "Systemd service configured"
}

setup_nginx() {
    log_info "Configuring Nginx..."
    
    cat > /etc/nginx/sites-available/ankicollab << 'EOF'
server {
    listen 80;
    server_name _;

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

    # Security headers
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header X-XSS-Protection "1; mode=block";

    # API endpoints
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
        
        # Rate limiting
        limit_req zone=api burst=20 nodelay;
    }

    # Media files
    location /media/ {
        alias /opt/ankicollab/media/;
        expires 1y;
        add_header Cache-Control "public, immutable";
        
        # Security
        location ~* \.(php|pl|py|jsp|asp|sh|cgi)$ {
            deny all;
        }
    }

    # Health check
    location /health {
        access_log off;
        return 200 "healthy\n";
        add_header Content-Type text/plain;
    }
}

# Rate limiting
http {
    limit_req_zone $binary_remote_addr zone=api:10m rate=10r/m;
}
EOF
    
    # Enable site
    ln -sf /etc/nginx/sites-available/ankicollab /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    
    # Test configuration
    nginx -t
    systemctl enable nginx
    systemctl restart nginx
    
    log_info "Nginx configured and started"
}

setup_monitoring() {
    log_info "Setting up monitoring..."
    
    # Create monitoring script
    cat > $APP_DIR/monitor.sh << 'EOF'
#!/bin/bash

LOG_FILE="/var/log/ankicollab/monitor.log"
ALERT_EMAIL=""  # Set email for alerts

while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    
    # System metrics
    CPU_USAGE=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1)
    MEMORY_USAGE=$(free | grep Mem | awk '{printf "%.1f", $3/$2 * 100.0}')
    DISK_USAGE=$(df -h /opt/ankicollab | awk 'NR==2 {print $5}' | cut -d'%' -f1)
    
    # Pi temperature (if available)
    if command -v vcgencmd &> /dev/null; then
        TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d'=' -f2 | cut -d"'" -f1)
    else
        TEMP="N/A"
    fi
    
    # Service status
    if systemctl is-active --quiet ankicollab; then
        SERVICE_STATUS="running"
    else
        SERVICE_STATUS="stopped"
    fi
    
    # Log metrics
    echo "$TIMESTAMP CPU:${CPU_USAGE}% MEM:${MEMORY_USAGE}% DISK:${DISK_USAGE}% TEMP:${TEMP}°C SERVICE:${SERVICE_STATUS}" >> $LOG_FILE
    
    # Alerts
    if (( $(echo "$CPU_USAGE > 80" | bc -l) )); then
        echo "HIGH CPU USAGE: $CPU_USAGE%" | logger -t ankicollab-monitor
    fi
    
    if (( $(echo "$MEMORY_USAGE > 85" | bc -l) )); then
        echo "HIGH MEMORY USAGE: $MEMORY_USAGE%" | logger -t ankicollab-monitor
    fi
    
    if [[ "$SERVICE_STATUS" == "stopped" ]]; then
        echo "SERVICE DOWN: Attempting restart" | logger -t ankicollab-monitor
        systemctl restart ankicollab
    fi
    
    sleep 60
done
EOF
    
    chmod +x $APP_DIR/monitor.sh
    chown $APP_USER:$APP_USER $APP_DIR/monitor.sh
    
    # Create monitoring service
    cat > /etc/systemd/system/ankicollab-monitor.service << EOF
[Unit]
Description=AnkiCollab Monitor
After=ankicollab.service

[Service]
Type=simple
User=root
ExecStart=$APP_DIR/monitor.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
    
    systemctl daemon-reload
    systemctl enable ankicollab-monitor
    
    log_info "Monitoring setup completed"
}

setup_backup() {
    log_info "Setting up backup system..."
    
    cat > $APP_DIR/backup.sh << 'EOF'
#!/bin/bash

BACKUP_DIR="/backup/ankicollab"
DATE=$(date +%Y%m%d_%H%M%S)

# Create backup directory
mkdir -p $BACKUP_DIR

# Backup database
sqlite3 /opt/ankicollab/ankicollab.db ".backup $BACKUP_DIR/ankicollab_$DATE.db"

# Backup media files
tar -czf $BACKUP_DIR/media_$DATE.tar.gz -C /opt/ankicollab media/

# Backup configuration
tar -czf $BACKUP_DIR/config_$DATE.tar.gz -C /opt/ankicollab config/

# Keep only last 7 days of backups
find $BACKUP_DIR -name "*.db" -mtime +7 -delete
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete

echo "Backup completed: $DATE"
EOF
    
    chmod +x $APP_DIR/backup.sh
    chown $APP_USER:$APP_USER $APP_DIR/backup.sh
    
    # Add to crontab
    (crontab -u $APP_USER -l 2>/dev/null; echo "0 2 * * * $APP_DIR/backup.sh") | crontab -u $APP_USER -
    
    log_info "Backup system configured"
}

setup_logrotate() {
    log_info "Setting up log rotation..."
    
    cat > /etc/logrotate.d/ankicollab << EOF
$LOG_DIR/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 644 $APP_USER $APP_USER
    postrotate
        systemctl reload ankicollab || true
    endscript
}
EOF
    
    log_info "Log rotation configured"
}

optimize_system() {
    log_info "Applying Pi-specific system optimizations..."
    
    # Kernel parameters for Pi
    cat >> /etc/sysctl.conf << EOF

# AnkiCollab Pi optimizations
vm.swappiness=10
vm.dirty_ratio=15
vm.dirty_background_ratio=5
net.core.rmem_max=16777216
net.core.wmem_max=16777216
net.ipv4.tcp_rmem=4096 16384 16777216
net.ipv4.tcp_wmem=4096 16384 16777216
EOF
    
    sysctl -p
    
    # GPU memory split for headless Pi
    if [[ -f /boot/config.txt ]]; then
        grep -q "gpu_mem" /boot/config.txt || echo "gpu_mem=16" >> /boot/config.txt
        grep -q "disable_splash" /boot/config.txt || echo "disable_splash=1" >> /boot/config.txt
    fi
    
    log_info "System optimizations applied"
}

start_services() {
    log_info "Starting services..."
    
    systemctl start $SERVICE_NAME
    systemctl start ankicollab-monitor
    systemctl restart nginx
    
    # Wait for service to start
    sleep 5
    
    # Check service status
    if systemctl is-active --quiet $SERVICE_NAME; then
        log_info "AnkiCollab service started successfully"
    else
        log_error "Failed to start AnkiCollab service"
        systemctl status $SERVICE_NAME
        exit 1
    fi
}

show_status() {
    echo
    echo "=== AnkiCollab Pi Deployment Complete ==="
    echo
    echo "Service Status:"
    systemctl status ankicollab --no-pager -l
    echo
    echo "Memory Usage:"
    free -h
    echo
    echo "Disk Usage:"
    df -h $APP_DIR
    echo
    echo "Configuration:"
    echo "  - Application: $APP_DIR"
    echo "  - Database: $DB_FILE"
    echo "  - Media: $MEDIA_DIR"
    echo "  - Logs: $LOG_DIR"
    echo "  - Backups: $BACKUP_DIR"
    echo
    echo "Next Steps:"
    echo "  1. Configure your domain/IP in client applications"
    echo "  2. Set up SSL/HTTPS with Let's Encrypt (recommended)"
    echo "  3. Configure firewall rules"
    echo "  4. Monitor logs: journalctl -u ankicollab -f"
    echo
    log_info "Deployment completed successfully!"
}

# Main execution
main() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root"
        exit 1
    fi
    
    check_pi
    check_memory
    install_dependencies
    create_user
    setup_directories
    setup_database
    build_backend
    create_config
    setup_systemd
    setup_nginx
    setup_monitoring
    setup_backup
    setup_logrotate
    optimize_system
    start_services
    show_status
}

# Run main function
main "$@"
