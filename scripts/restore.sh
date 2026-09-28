#!/bin/bash
#
# Meeting-Ops Restore Script
# Restores from backups created by backup.sh
#
# Usage:
#   ./scripts/restore.sh <backup_file.tar.gz>
#   ./scripts/restore.sh latest
#   ./scripts/restore.sh                       # list available backups
#
# Requires PostgreSQL container (meetingops-postgres) to be running.
# Does NOT require root.

set -e

# Resolve project root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

BACKUP_DIR="$PROJECT_DIR/backups"
RECORDINGS_DIR="$PROJECT_DIR/recordings"

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

# List available backups
list_backups() {
    echo ""
    echo -e "${BLUE}Available backups:${NC}"
    if [ ! -d "$BACKUP_DIR" ]; then
        print_error "Backup directory not found: $BACKUP_DIR"
        exit 1
    fi

    ls -lhtr "$BACKUP_DIR"/meetingops_backup_*.tar.gz 2>/dev/null || {
        print_error "No backup files found in $BACKUP_DIR"
        exit 1
    }
}

# Restore from a backup archive
restore_backup() {
    local backup_file="$1"

    if [ ! -f "$backup_file" ]; then
        print_error "Backup file not found: $backup_file"
        exit 1
    fi

    echo ""
    echo -e "${BLUE}Meeting-Ops Restore${NC}"
    echo "==================="
    echo ""
    print_status "Restoring from: $(basename "$backup_file")"

    # Extract to temp dir
    TEMP_DIR=$(mktemp -d)
    trap 'rm -rf "$TEMP_DIR"' EXIT

    print_status "Extracting archive..."
    tar -xzf "$backup_file" -C "$TEMP_DIR"

    # Find the backup content directory
    BACKUP_CONTENT=$(find "$TEMP_DIR" -maxdepth 1 -name "meetingops_backup_*" -type d | head -1)
    if [ -z "$BACKUP_CONTENT" ]; then
        print_error "Invalid backup structure (no meetingops_backup_* directory found)"
        exit 1
    fi

    # Show metadata
    if [ -f "$BACKUP_CONTENT/backup_metadata.json" ]; then
        echo ""
        print_status "Backup metadata:"
        python3 -m json.tool "$BACKUP_CONTENT/backup_metadata.json" 2>/dev/null || \
            cat "$BACKUP_CONTENT/backup_metadata.json"
        echo ""
    fi

    # Confirm
    echo -e "${YELLOW}This will overwrite the current database. Continue? (y/N)${NC}"
    read -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        print_status "Restore cancelled."
        exit 0
    fi

    # ---- Safety backup of current database ----
    if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
        SAFETY_FILE="$BACKUP_DIR/pre_restore_$(date +%Y%m%d_%H%M%S).sql"
        print_status "Creating safety backup of current database..."
        docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
            > "$SAFETY_FILE" 2>/dev/null && \
            print_success "Safety backup: $SAFETY_FILE" || \
            print_warning "Could not create safety backup (new install?)"
    fi

    # ---- Restore PostgreSQL ----
    if [ -f "$BACKUP_CONTENT/meeting_sessions.pgdump" ]; then
        print_status "Restoring database (custom format)..."
        if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
            # Drop and recreate the database
            docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres \
                -c "DROP DATABASE IF EXISTS ${PG_DB};" \
                -c "CREATE DATABASE ${PG_DB} OWNER ${PG_USER};" 2>/dev/null
            # Restore from custom dump
            docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner \
                < "$BACKUP_CONTENT/meeting_sessions.pgdump" 2>/dev/null && \
                print_success "Database restored" || \
                print_warning "pg_restore reported warnings (usually harmless)"
        else
            print_error "Container $PG_CONTAINER is not running. Start with: make up"
            exit 1
        fi
    elif [ -f "$BACKUP_CONTENT/meeting_sessions.sql" ]; then
        print_status "Restoring database (SQL format)..."
        if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
            docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres \
                -c "DROP DATABASE IF EXISTS ${PG_DB};" \
                -c "CREATE DATABASE ${PG_DB} OWNER ${PG_USER};" 2>/dev/null
            docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" \
                < "$BACKUP_CONTENT/meeting_sessions.sql" 2>/dev/null && \
                print_success "Database restored" || \
                print_warning "psql reported warnings (usually harmless)"
        else
            print_error "Container $PG_CONTAINER is not running. Start with: make up"
            exit 1
        fi
    else
        print_warning "No database dump found in backup"
    fi

    # ---- Restore audio recordings ----
    if [ -d "$BACKUP_CONTENT/recordings" ]; then
        print_status "Restoring audio recordings..."
        mkdir -p "$RECORDINGS_DIR"
        cp -rn "$BACKUP_CONTENT/recordings/"* "$RECORDINGS_DIR/" 2>/dev/null || true
        RESTORED_COUNT=$(find "$BACKUP_CONTENT/recordings" -type f 2>/dev/null | wc -l)
        print_success "Restored $RESTORED_COUNT audio file(s) (existing files kept)"
    fi

    # ---- Restore .env ----
    if [ -f "$BACKUP_CONTENT/.env" ]; then
        if [ -f "$PROJECT_DIR/.env" ]; then
            print_warning ".env already exists -- skipping (backup copy in archive)"
        else
            cp "$BACKUP_CONTENT/.env" "$PROJECT_DIR/.env"
            print_success "Restored .env"
        fi
    fi

    # ---- Verify ----
    echo ""
    print_status "Verifying restore..."
    if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
        ROW_COUNT=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
            "SELECT count(*) FROM recording_sessions;" 2>/dev/null || echo "?")
        print_success "Database has $ROW_COUNT session(s)"
    fi

    echo ""
    print_success "Restore complete."
    if [ -n "$SAFETY_FILE" ] && [ -f "$SAFETY_FILE" ]; then
        echo -e "${BLUE}Safety backup of previous data:${NC} $SAFETY_FILE"
    fi
}

# ---- Main ----
if [ $# -eq 0 ]; then
    list_backups
    echo ""
    echo -e "${BLUE}Usage:${NC}"
    echo "  $0 <backup_file.tar.gz>"
    echo "  $0 latest"
    exit 0
fi

if [ "$1" = "latest" ]; then
    LATEST=$(ls -t "$BACKUP_DIR"/meetingops_backup_*.tar.gz 2>/dev/null | head -1)
    if [ -z "$LATEST" ]; then
        print_error "No backups found in $BACKUP_DIR"
        exit 1
    fi
    restore_backup "$LATEST"
else
    if [[ "$1" = /* ]]; then
        restore_backup "$1"
    else
        restore_backup "$BACKUP_DIR/$1"
    fi
fi
