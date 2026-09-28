#!/bin/bash
#
# Meeting-Ops Backup Script
# Backs up PostgreSQL database, recent audio files, and configuration
#
# Usage: ./scripts/backup.sh
#
# Backups are stored in ./backups/ as compressed tarballs.
# No service downtime -- pg_dump runs online against the Docker container.

set -e

# Resolve project root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

BACKUP_DIR="$PROJECT_DIR/backups"
RECORDINGS_DIR="$PROJECT_DIR/recordings"
MODELS_DIR="$PROJECT_DIR/models"
RETENTION_DAYS=30
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="meetingops_backup_$TIMESTAMP"

# Database connection (matches docker-compose-full-stack.yml)
PG_CONTAINER="meetingops-postgres"
PG_USER="meetingops"
PG_DB="meeting_sessions"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo ""
echo -e "${BLUE}Meeting-Ops Backup${NC}"
echo "==================="
echo ""

# Create backup staging directory
mkdir -p "$BACKUP_DIR"
CURRENT_BACKUP="$BACKUP_DIR/$BACKUP_NAME"
mkdir -p "$CURRENT_BACKUP"

# ---- PostgreSQL dump (online, no downtime) ----
print_status "Backing up PostgreSQL database..."
if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" --format=custom \
        > "$CURRENT_BACKUP/meeting_sessions.pgdump" 2>/dev/null && \
        print_success "Database dumped (custom format)" || {
            print_warning "Custom dump failed, trying plain SQL..."
            docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
                > "$CURRENT_BACKUP/meeting_sessions.sql" 2>/dev/null && \
                print_success "Database dumped (SQL)" || \
                print_error "Database dump failed. Is PostgreSQL running?"
        }
else
    print_error "Container $PG_CONTAINER is not running. Skipping database backup."
fi

# ---- Recent audio recordings (last 7 days) ----
print_status "Backing up recent audio files (last 7 days)..."
if [ -d "$RECORDINGS_DIR" ]; then
    AUDIO_COUNT=0
    mkdir -p "$CURRENT_BACKUP/recordings"
    while IFS= read -r -d '' file; do
        rel_path="${file#"$RECORDINGS_DIR"/}"
        mkdir -p "$CURRENT_BACKUP/recordings/$(dirname "$rel_path")"
        cp "$file" "$CURRENT_BACKUP/recordings/$rel_path"
        AUDIO_COUNT=$((AUDIO_COUNT + 1))
    done < <(find "$RECORDINGS_DIR" -type f \( -name "*.wav" -o -name "*.mp3" -o -name "*.webm" \) -mtime -7 -print0 2>/dev/null)
    print_success "Backed up $AUDIO_COUNT audio file(s)"
else
    print_warning "No recordings/ directory found"
fi

# ---- .env file ----
if [ -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env" "$CURRENT_BACKUP/.env"
    print_success "Backed up .env"
else
    print_status "No .env file (using defaults)"
fi

# ---- Model inventory (list only, models are too large to copy) ----
print_status "Recording model inventory..."
if [ -d "$MODELS_DIR" ]; then
    ls -lhS "$MODELS_DIR"/*.gguf 2>/dev/null > "$CURRENT_BACKUP/model_inventory.txt" || \
        echo "No .gguf models found" > "$CURRENT_BACKUP/model_inventory.txt"
    print_success "Model inventory saved (files not copied -- too large)"
fi

# ---- System info snapshot ----
cat > "$CURRENT_BACKUP/system_info.txt" << EOF
Meeting-Ops Backup Information
==============================
Backup Date: $(date)
Hostname: $(hostname)
OS: $(lsb_release -d 2>/dev/null | cut -f2- || uname -a)
Python: $(python3 --version 2>/dev/null || echo "not found")
Node: $(node --version 2>/dev/null || echo "not found")
Docker: $(docker --version 2>/dev/null || echo "not found")

Docker Services:
$(docker ps --filter "name=meetingops-" --format "  {{.Names}}: {{.Status}}" 2>/dev/null || echo "  Docker not running")

Disk Usage:
$(df -h "$PROJECT_DIR" 2>/dev/null || echo "  unknown")
EOF

# ---- Metadata ----
cat > "$CURRENT_BACKUP/backup_metadata.json" << EOF
{
  "backup_name": "$BACKUP_NAME",
  "timestamp": "$TIMESTAMP",
  "date": "$(date -Iseconds)",
  "hostname": "$(hostname)",
  "version": "4.0.0",
  "components": {
    "database": $([ -f "$CURRENT_BACKUP/meeting_sessions.pgdump" ] || [ -f "$CURRENT_BACKUP/meeting_sessions.sql" ] && echo "true" || echo "false"),
    "recordings": $([ -d "$CURRENT_BACKUP/recordings" ] && echo "true" || echo "false"),
    "env_file": $([ -f "$CURRENT_BACKUP/.env" ] && echo "true" || echo "false"),
    "model_inventory": $([ -f "$CURRENT_BACKUP/model_inventory.txt" ] && echo "true" || echo "false")
  }
}
EOF

# ---- Compress ----
print_status "Compressing backup..."
cd "$BACKUP_DIR"
tar -czf "${BACKUP_NAME}.tar.gz" "$BACKUP_NAME"
rm -rf "$BACKUP_NAME"

BACKUP_SIZE=$(du -h "${BACKUP_NAME}.tar.gz" | cut -f1)
print_success "Backup complete: ${BACKUP_NAME}.tar.gz ($BACKUP_SIZE)"

# ---- Cleanup old backups ----
OLD_COUNT=$(find "$BACKUP_DIR" -name "meetingops_backup_*.tar.gz" -mtime +$RETENTION_DAYS -delete -print 2>/dev/null | wc -l)
if [ "$OLD_COUNT" -gt 0 ]; then
    print_status "Cleaned up $OLD_COUNT backup(s) older than $RETENTION_DAYS days"
fi

TOTAL_BACKUPS=$(find "$BACKUP_DIR" -name "meetingops_backup_*.tar.gz" 2>/dev/null | wc -l)
echo ""
print_success "Done. $TOTAL_BACKUPS backup(s) in $BACKUP_DIR/"
echo -e "${BLUE}Restore with:${NC} bash scripts/restore.sh $BACKUP_DIR/${BACKUP_NAME}.tar.gz"
