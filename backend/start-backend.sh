#!/bin/bash
# Meeting-Ops Backend Startup Script

echo "Starting Meeting-Ops Backend..."

# Ensure PipeWire audio is running (needed for shared mic access)
if ! systemctl --user is-active pipewire &>/dev/null; then
    echo "Starting PipeWire audio services..."
    systemctl --user start pipewire pipewire-pulse wireplumber 2>/dev/null || true
    sleep 1
fi
if systemctl --user is-active pipewire &>/dev/null; then
    echo "PipeWire audio: running"
else
    echo "PipeWire audio: not available (recording may use direct ALSA)"
fi

# Set mic gain if script exists
[ -x "../scripts/set-mic-gain.sh" ] && ../scripts/set-mic-gain.sh &

# Load environment variables from .env file
if [ -f ../.env ]; then
    echo "Loading configuration from parent .env file..."
    export $(cat ../.env | grep -v '^#' | xargs)
elif [ -f .env ]; then
    echo "Loading configuration from .env file..."
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "No .env file found, using defaults..."
    export DATABASE_URL="postgresql://meetingops:meetingops123@localhost:5434/meeting_sessions"
    export REDIS_URL="redis://localhost:6381"
    export HOST="0.0.0.0"
    export PORT="9050"
fi

# Check if NPU is available
if [ -e /dev/accel/accel0 ]; then
    echo "NPU device detected: /dev/accel/accel0"
else
    echo "NPU device not found - check permissions"
fi

# Start the backend
echo "Starting backend on ${HOST}:${PORT}..."
echo "Database: ${DATABASE_URL}"
echo "Redis: ${REDIS_URL}"
echo ""

# Check if Docker services are running
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Checking Docker services status..."

if ! docker ps | grep -q meetingops-postgres; then
    echo "  PostgreSQL not running. Start with: docker compose -f docker-compose-full-stack.yml up -d"
fi

if ! docker ps | grep -q meetingops-redis; then
    echo "  Redis not running. Start with: docker compose -f docker-compose-full-stack.yml up -d"
fi

if ! docker ps | grep -q meetingops-llama-gpu; then
    echo "  Granite LLM not running. Start with: docker compose -f docker-compose-full-stack.yml up -d"
fi

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
for i in {1..15}; do
    if docker exec meetingops-postgres pg_isready -U meetingops &>/dev/null; then
        echo "PostgreSQL ready"
        break
    fi
    echo -n "."
    sleep 1
done

# Fail if PostgreSQL is not reachable
if ! docker exec meetingops-postgres pg_isready -U meetingops &>/dev/null; then
    echo ""
    echo "ERROR: PostgreSQL is not reachable after 15 seconds."
    echo "Meeting-Ops requires PostgreSQL to run."
    echo ""
    echo "To start it:  docker compose -f $PROJECT_DIR/docker-compose-full-stack.yml up -d"
    echo ""
    exit 1
fi

cd "$SCRIPT_DIR"

# Kill any existing processes that might conflict
pkill -f "uvicorn main:app" 2>/dev/null || true
sleep 1

# Start the backend
echo "Starting uvicorn..."
python3 -m uvicorn main:app --host ${HOST} --port ${PORT}
