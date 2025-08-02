#!/bin/bash

# AnkiCollab Migration Script: PostgreSQL to SQLite (Pi Optimization)
# This script helps migrate from a PostgreSQL-based backend to Pi-optimized SQLite

set -e

# Configuration
POSTGRES_DB="ankicollab"
POSTGRES_USER="ankicollab"
POSTGRES_HOST="localhost"
POSTGRES_PORT="5432"
SQLITE_DB="/opt/ankicollab/ankicollab.db"
BACKUP_DIR="/backup/migration"
MEDIA_SOURCE="/path/to/current/media"  # Update this path
MEDIA_DEST="/opt/ankicollab/media"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Check if running as root
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root"
        exit 1
    fi
    
    # Check if PostgreSQL tools are available
    if ! command -v pg_dump &> /dev/null; then
        log_error "pg_dump not found. Please install postgresql-client"
        exit 1
    fi
    
    # Check if SQLite is available
    if ! command -v sqlite3 &> /dev/null; then
        log_error "sqlite3 not found. Please install sqlite3"
        exit 1
    fi
    
    log_info "Prerequisites check passed"
}

create_backup() {
    log_info "Creating backup of current system..."
    
    mkdir -p $BACKUP_DIR
    
    # Backup PostgreSQL database
    log_info "Backing up PostgreSQL database..."
    pg_dump -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER $POSTGRES_DB > $BACKUP_DIR/postgres_backup.sql
    
    # Backup current media files
    if [[ -d "$MEDIA_SOURCE" ]]; then
        log_info "Backing up media files..."
        tar -czf $BACKUP_DIR/media_backup.tar.gz -C $(dirname $MEDIA_SOURCE) $(basename $MEDIA_SOURCE)
    else
        log_warn "Media source directory not found: $MEDIA_SOURCE"
    fi
    
    log_info "Backup completed in $BACKUP_DIR"
}

extract_data_from_postgres() {
    log_info "Extracting data from PostgreSQL..."
    
    # Create temporary directory for extracted data
    mkdir -p /tmp/migration_data
    
    # Extract decks data
    psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB -c "
    COPY (
        SELECT id, hash, data, 
               EXTRACT(EPOCH FROM created_at)::INTEGER as created_at,
               EXTRACT(EPOCH FROM updated_at)::INTEGER as updated_at
        FROM decks
    ) TO '/tmp/migration_data/decks.csv' WITH CSV HEADER;
    "
    
    # Extract media files data
    psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB -c "
    COPY (
        SELECT hash, filename, size, reference_count,
               EXTRACT(EPOCH FROM created_at)::INTEGER as created_at
        FROM media_files
    ) TO '/tmp/migration_data/media_files.csv' WITH CSV HEADER;
    "
    
    # Extract deck-media relationships
    psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB -c "
    COPY (
        SELECT deck_hash, media_hash
        FROM deck_media
    ) TO '/tmp/migration_data/deck_media.csv' WITH CSV HEADER;
    "
    
    log_info "Data extraction completed"
}

create_sqlite_database() {
    log_info "Creating SQLite database..."
    
    # Remove existing SQLite database if it exists
    rm -f $SQLITE_DB
    
    # Create directory structure
    mkdir -p $(dirname $SQLITE_DB)
    
    # Create SQLite database with Pi-optimized schema
    sqlite3 $SQLITE_DB << 'EOF'
-- Pi-optimized SQLite settings
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = 1000;
PRAGMA temp_store = memory;
PRAGMA mmap_size = 134217728;  -- 128MB mmap

-- Create tables
CREATE TABLE decks (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE NOT NULL,
    data BLOB,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE media_files (
    hash TEXT PRIMARY KEY,
    filename TEXT,
    size INTEGER,
    reference_count INTEGER DEFAULT 1,
    created_at INTEGER
);

CREATE TABLE deck_media (
    deck_hash TEXT,
    media_hash TEXT,
    PRIMARY KEY (deck_hash, media_hash),
    FOREIGN KEY (deck_hash) REFERENCES decks(hash),
    FOREIGN KEY (media_hash) REFERENCES media_files(hash)
);

-- Create indexes
CREATE INDEX idx_decks_hash ON decks(hash);
CREATE INDEX idx_decks_updated ON decks(updated_at);
CREATE INDEX idx_media_files_hash ON media_files(hash);
CREATE INDEX idx_deck_media_deck ON deck_media(deck_hash);
CREATE INDEX idx_deck_media_media ON deck_media(media_hash);
EOF
    
    log_info "SQLite database created"
}

import_data_to_sqlite() {
    log_info "Importing data to SQLite..."
    
    # Import decks data
    if [[ -f /tmp/migration_data/decks.csv ]]; then
        log_info "Importing decks..."
        sqlite3 $SQLITE_DB << 'EOF'
.mode csv
.import /tmp/migration_data/decks.csv temp_decks
INSERT INTO decks (id, hash, data, created_at, updated_at)
SELECT id, hash, data, created_at, updated_at FROM temp_decks WHERE id != 'id';
DROP TABLE temp_decks;
EOF
    fi
    
    # Import media files data
    if [[ -f /tmp/migration_data/media_files.csv ]]; then
        log_info "Importing media files..."
        sqlite3 $SQLITE_DB << 'EOF'
.mode csv
.import /tmp/migration_data/media_files.csv temp_media_files
INSERT INTO media_files (hash, filename, size, reference_count, created_at)
SELECT hash, filename, size, reference_count, created_at FROM temp_media_files WHERE hash != 'hash';
DROP TABLE temp_media_files;
EOF
    fi
    
    # Import deck-media relationships
    if [[ -f /tmp/migration_data/deck_media.csv ]]; then
        log_info "Importing deck-media relationships..."
        sqlite3 $SQLITE_DB << 'EOF'
.mode csv
.import /tmp/migration_data/deck_media.csv temp_deck_media
INSERT INTO deck_media (deck_hash, media_hash)
SELECT deck_hash, media_hash FROM temp_deck_media WHERE deck_hash != 'deck_hash';
DROP TABLE temp_deck_media;
EOF
    fi
    
    log_info "Data import completed"
}

migrate_media_files() {
    log_info "Migrating media files..."
    
    if [[ -d "$MEDIA_SOURCE" ]]; then
        # Create media destination directory
        mkdir -p $MEDIA_DEST
        
        # Copy media files with organization by hash prefix
        find $MEDIA_SOURCE -type f \( -name "*.jpg" -o -name "*.jpeg" -o -name "*.png" -o -name "*.gif" -o -name "*.webp" -o -name "*.svg" -o -name "*.mp3" -o -name "*.ogg" \) | while read file; do
            filename=$(basename "$file")
            # Extract hash from filename or generate one
            if [[ $filename =~ ^[a-f0-9]{64} ]]; then
                hash=${filename:0:64}
                subdir=${hash:0:2}
                mkdir -p $MEDIA_DEST/$subdir
                cp "$file" $MEDIA_DEST/$subdir/
            else
                # Generate hash for files without hash names
                hash=$(sha256sum "$file" | cut -d' ' -f1)
                subdir=${hash:0:2}
                mkdir -p $MEDIA_DEST/$subdir
                extension="${filename##*.}"
                cp "$file" $MEDIA_DEST/$subdir/${hash}.${extension}
            fi
        done
        
        # Set proper permissions
        chown -R ankicollab:ankicollab $MEDIA_DEST
        chmod -R 755 $MEDIA_DEST
        
        log_info "Media files migrated"
    else
        log_warn "Media source directory not found, skipping media migration"
    fi
}

update_configuration() {
    log_info "Updating configuration for SQLite..."
    
    # Update application configuration
    if [[ -f /opt/ankicollab/config/app.toml ]]; then
        sed -i 's|postgresql://.*|sqlite:/opt/ankicollab/ankicollab.db|g' /opt/ankicollab/config/app.toml
        sed -i 's|max_connections = [0-9]*|max_connections = 2|g' /opt/ankicollab/config/app.toml
        sed -i 's|storage.*type.*=.*"s3"|type = "local"|g' /opt/ankicollab/config/app.toml
    fi
    
    # Update systemd service environment
    if [[ -f /etc/systemd/system/ankicollab.service ]]; then
        sed -i 's|Environment=DATABASE_URL=postgresql://.*|Environment=DATABASE_URL=sqlite:/opt/ankicollab/ankicollab.db|g' /etc/systemd/system/ankicollab.service
        sed -i 's|Environment=STORAGE_TYPE=s3|Environment=STORAGE_TYPE=local|g' /etc/systemd/system/ankicollab.service
        systemctl daemon-reload
    fi
    
    log_info "Configuration updated"
}

verify_migration() {
    log_info "Verifying migration..."
    
    # Check SQLite database
    deck_count=$(sqlite3 $SQLITE_DB "SELECT COUNT(*) FROM decks;")
    media_count=$(sqlite3 $SQLITE_DB "SELECT COUNT(*) FROM media_files;")
    relation_count=$(sqlite3 $SQLITE_DB "SELECT COUNT(*) FROM deck_media;")
    
    log_info "Migration statistics:"
    log_info "  - Decks: $deck_count"
    log_info "  - Media files: $media_count"
    log_info "  - Deck-media relations: $relation_count"
    
    # Check media files
    if [[ -d $MEDIA_DEST ]]; then
        media_file_count=$(find $MEDIA_DEST -type f | wc -l)
        log_info "  - Physical media files: $media_file_count"
    fi
    
    # Test database connectivity
    if sqlite3 $SQLITE_DB "SELECT 1;" &>/dev/null; then
        log_info "SQLite database is accessible"
    else
        log_error "SQLite database is not accessible"
        exit 1
    fi
    
    log_info "Migration verification completed"
}

cleanup() {
    log_info "Cleaning up temporary files..."
    
    rm -rf /tmp/migration_data
    
    log_info "Cleanup completed"
}

create_rollback_script() {
    log_info "Creating rollback script..."
    
    cat > $BACKUP_DIR/rollback.sh << EOF
#!/bin/bash
# Rollback script for AnkiCollab migration

set -e

echo "Rolling back AnkiCollab migration..."

# Stop services
systemctl stop ankicollab || true

# Restore PostgreSQL database
psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB < $BACKUP_DIR/postgres_backup.sql

# Restore media files
if [[ -f $BACKUP_DIR/media_backup.tar.gz ]]; then
    tar -xzf $BACKUP_DIR/media_backup.tar.gz -C $(dirname $MEDIA_SOURCE)
fi

# Restore original configuration
# (Add specific configuration restore commands here)

echo "Rollback completed. Please restart your original services."
EOF
    
    chmod +x $BACKUP_DIR/rollback.sh
    log_info "Rollback script created at $BACKUP_DIR/rollback.sh"
}

main() {
    echo "=== AnkiCollab Migration: PostgreSQL to SQLite ==="
    echo "This script will migrate your AnkiCollab backend from PostgreSQL to SQLite"
    echo "for Raspberry Pi optimization."
    echo
    
    read -p "Do you want to proceed? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Migration cancelled"
        exit 0
    fi
    
    check_prerequisites
    create_backup
    extract_data_from_postgres
    create_sqlite_database
    import_data_to_sqlite
    migrate_media_files
    update_configuration
    verify_migration
    create_rollback_script
    cleanup
    
    echo
    echo "=== Migration Completed Successfully ==="
    echo
    echo "Your AnkiCollab backend has been migrated to SQLite."
    echo "Key changes:"
    echo "  - Database: PostgreSQL → SQLite"
    echo "  - Storage: S3 → Local filesystem"
    echo "  - Configuration: Optimized for Raspberry Pi"
    echo
    echo "Next steps:"
    echo "  1. Restart the AnkiCollab service: systemctl restart ankicollab"
    echo "  2. Test the functionality with a client"
    echo "  3. Monitor performance and logs"
    echo "  4. If issues occur, use rollback script: $BACKUP_DIR/rollback.sh"
    echo
    log_info "Migration completed successfully!"
}

# Run main function
main "$@"
