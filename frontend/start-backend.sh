#\!/bin/bash
# Meeting-Ops Backend Startup Script

echo "🚀 Starting Meeting-Ops Backend with NPU..."

# Load environment variables from .env file
if [ -f .env ]; then
    echo "📄 Loading configuration from .env file..."
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "⚠️ No .env file found, using defaults..."
    export DATABASE_URL="postgresql://meetingops:meetingops123@localhost:5432/meeting_sessions"
    export REDIS_URL="redis://localhost:6379"
    export HOST="0.0.0.0"
    export PORT="9050"
fi

# Start the NPU-optimized backend
echo "🎯 Starting NPU backend on ${HOST}:${PORT}..."
./venv/bin/python -m uvicorn npu_main:app --host ${HOST} --port ${PORT} --reload
EOF < /dev/null
