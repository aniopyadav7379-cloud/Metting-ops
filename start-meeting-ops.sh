#!/bin/bash
# Meeting-Ops Full System Startup Script

echo "🦄 Starting Meeting-Ops Complete System..."
echo "======================================="

# Function to kill background processes on exit
cleanup() {
    echo ""
    echo "🛑 Shutting down Meeting-Ops..."
    if [ ! -z "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null
        echo "✅ Backend stopped"
    fi
    if [ ! -z "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null
        echo "✅ Frontend stopped"
    fi
    exit 0
}

# Set up signal handling
trap cleanup SIGINT SIGTERM

# Check if ports are free
if nc -z localhost 9050 2>/dev/null; then
    echo "⚠️ Port 9050 is already in use (backend)"
    echo "💡 Kill existing process or use different port"
    exit 1
fi

if nc -z localhost 7777 2>/dev/null; then
    echo "⚠️ Port 7777 is already in use (frontend)"
    echo "💡 Kill existing process or use different port"
    exit 1
fi

# Start backend
echo "🚀 Starting backend..."
cd backend
./start-backend.sh &
BACKEND_PID=$!
cd ..

# Wait for backend to start
echo "⏳ Waiting for backend to initialize..."
sleep 5

# Check if backend started successfully
if ! nc -z localhost 9050 2>/dev/null; then
    echo "❌ Backend failed to start"
    cleanup
    exit 1
fi

echo "✅ Backend running on port 9050"

# Start frontend
echo "🎨 Starting frontend..."
cd frontend
./start-frontend.sh &
FRONTEND_PID=$!
cd ..

# Wait for frontend to start
echo "⏳ Waiting for frontend to initialize..."
sleep 3

echo ""
echo "🎉 Meeting-Ops is running!"
echo "======================================="
echo "🌐 Frontend: http://localhost:7777"
echo "🔧 Backend API: http://localhost:9050/docs"
echo "👥 Login: admin/admin123 or user/user123"
echo "🎤 USB Mic: Ready for 44.1kHz transcription"
echo "⚡ NPU: AMD Phoenix 16 TOPS INT8"
echo ""
echo "Press Ctrl+C to stop both services..."

# Wait for user interrupt
wait